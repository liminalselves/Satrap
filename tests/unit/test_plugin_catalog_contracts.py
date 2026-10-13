"""业务链路场景与集中边界契约"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import asyncio
import time
import pytest
from satrap.core.utils.async_worker import BoundedAsyncWorker, WorkerBusyError
from satrap.core.utils.snapshot_cache import SnapshotCache
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.core.utils import snapshot_cache
from typing import Any, cast
import yaml
from satrap.edictum.plugin_runtime import PluginRuntimeState, apply_plugin_capabilities_async
from satrap.edictum.plugin_runtime import preview_plugin_reconciliation, reconcile_plugin_states_async
from satrap.edictum.plugin_spec import PluginSpec, parse_plugin_specs, plugin_specs_fingerprint
from satrap.edictum.plugin_compatibility import PluginEnvironment, check_plugin_compatibility
from unittest.mock import Mock
import io
import logging
from satrap.edictum.plugin_config import parse_config_schema, schema_to_payload
from satrap.core.log import Logger
from satrap.core.log.stream import standard_log_stream

def write_meta(base: Path, name: str, version: str = '1') -> Path:
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'meta.yaml'
    path.write_text(f'name: {name}\nversion: "{version}"\nconfig_schema:\n  note:\n    type: string\n    default: original\n', encoding='utf-8')
    return path

def test_warm_catalog_index_copies_and_external_invalidation(tmp_path, monkeypatch):
    builtin, user = tmp_path / 'builtin', tmp_path / 'user'
    path = write_meta(builtin, 'probe')
    write_meta(user, 'probe', 'shadow')
    calls = []
    load = PluginCatalog._load_entry
    monkeypatch.setattr(PluginCatalog, '_load_entry', staticmethod(lambda path: (calls.append(path), load(path))[1]))
    catalog = PluginCatalog(builtin, user)
    assert catalog.get('probe').version == '1'
    fixed = catalog.snapshot()
    changed = catalog.get('probe')
    changed.config_schema['note'].default = 'changed'
    changed.capabilities['tools']['fake'] = 'changed'
    assert PluginCatalog(builtin, user).get('probe').config_schema['note'].default == 'original'
    assert 'fake' not in catalog.get('probe').capabilities['tools']
    assert len(calls) == 2
    replacement = path.with_suffix('.new')
    replacement.write_text('name: probe\nversion: "2"\n', encoding='utf-8')
    replacement.replace(path)
    assert catalog.get('probe').version == '2'
    assert fixed.get('probe').version == '1'
    write_meta(user, 'added')
    assert catalog.get('added') is not None
    path.unlink()
    assert catalog.get('probe').version == 'shadow'

def test_broken_metadata_recovers_and_preserves_errors(tmp_path):
    path = write_meta(tmp_path, 'probe')
    catalog = PluginCatalog(tmp_path, tmp_path / 'missing')
    path.write_text('name: [invalid', encoding='utf-8')
    assert catalog.scan() == []
    assert catalog.scan_errors[0]['name'] == 'probe'
    catalog.scan_errors.clear()
    catalog.scan()
    assert catalog.scan_errors
    write_meta(tmp_path, 'probe', 'fixed')
    assert catalog.get('probe').version == 'fixed'
    assert catalog.scan_errors == []

def test_concurrent_catalog_builds_once(tmp_path, monkeypatch):
    write_meta(tmp_path, 'probe')
    calls = []
    load = PluginCatalog._load_entry

    def slow(path):
        calls.append(path)
        time.sleep(.03)
        return load(path)

    monkeypatch.setattr(PluginCatalog, '_load_entry', staticmethod(slow))
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: PluginCatalog(tmp_path, tmp_path / 'missing').get('probe'), range(8)))
    assert len(calls) == 1
    assert all(entry.version == '1' for entry in results)

def test_late_snapshot_cannot_replace_new_generation():
    cache = SnapshotCache()
    entered, release = threading.Event(), threading.Event()
    revision = [1]

    def load():
        value = revision[0]
        if value == 1:
            entered.set()
            assert release.wait(2)
        return value

    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(cache.get, 'resource', lambda: revision[0], load)
        assert entered.wait(2)
        revision[0] = 2
        assert cache.get('resource', lambda: revision[0], load) == 2
        release.set()
        assert old.result(timeout=2) == 2
        assert cache.get('resource', lambda: revision[0], load) == 2

@pytest.mark.asyncio
async def test_async_waiters_do_not_fill_pool_and_cancellation_isolated():
    cache = SnapshotCache()
    worker = BoundedAsyncWorker('test-snapshot', workers=1, capacity=1)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def load():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return 42

    try:
        first = asyncio.create_task(cache.get_async('key', lambda: 1, load, worker))
        await asyncio.to_thread(entered.wait, 2)
        followers = [asyncio.create_task(cache.get_async('key', lambda: 1, load, worker)) for _ in range(12)]
        await asyncio.sleep(.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await asyncio.gather(*followers) == [42] * 12
        assert calls == [1]
    finally:
        release.set()
        worker.close()

@pytest.mark.asyncio
async def test_queue_full_and_parse_failure_allow_retry():
    cache = SnapshotCache()
    worker = BoundedAsyncWorker('test-snapshot', workers=1, capacity=1)
    release, entered = threading.Event(), threading.Event()
    occupied = asyncio.create_task(worker.run(lambda: (entered.set(), release.wait(2))))
    await asyncio.to_thread(entered.wait, 2)
    try:
        with pytest.raises(WorkerBusyError):
            await cache.get_async('key', lambda: 1, lambda: 42, worker)
        release.set()
        await occupied
        with pytest.raises(ValueError):
            await cache.get_async('key', lambda: 1, lambda: int('bad'), worker)
        assert await cache.get_async('key', lambda: 1, lambda: 42, worker) == 42
    finally:
        release.set()
        await occupied
        worker.close()

@pytest.mark.asyncio
async def test_failure_notifications_follow_cleanup_and_worker_release(monkeypatch):
    cache = SnapshotCache()
    worker = BoundedAsyncWorker('test-snapshot', workers=1, capacity=1)
    original = snapshot_cache.Future

    class CheckedFuture(original):
        def set_exception(self, error):
            assert not cache._pending
            assert worker._slots.acquire(blocking=False)
            worker._slots.release()
            super().set_exception(error)

    monkeypatch.setattr(snapshot_cache, 'Future', CheckedFuture)
    try:
        with pytest.raises(ValueError):
            await cache.get_async('resource', lambda: 1, lambda: int('bad'), worker)
        assert await cache.get_async('resource', lambda: 1, lambda: 7, worker) == 7
    finally:
        worker.close()

@pytest.mark.asyncio
async def test_slow_source_probe_does_not_block_event_loop():
    cache = SnapshotCache()
    worker = BoundedAsyncWorker('test-snapshot', workers=1, capacity=1)
    entered, release = threading.Event(), threading.Event()

    def fingerprint():
        entered.set()
        assert release.wait(2)
        return 1

    request = asyncio.create_task(cache.get_async('key', fingerprint, lambda: 42, worker))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        started = time.perf_counter()
        await asyncio.sleep(.01)
        assert time.perf_counter() - started < .1
        assert not request.done()
        release.set()
        assert await request == 42
    finally:
        release.set()
        await request
        worker.close()

@pytest.mark.asyncio
async def test_incompatible_upgrade_is_blocked_before_uninstall():
    from satrap.edictum.plugin_compatibility import CompatibilityResult

    old = PluginSpec(name="demo", version="1", path="original")
    handle = object()
    states = [PluginRuntimeState(desired_spec=old, applied_spec=old, handle=handle)]
    target = PluginSpec(name="demo", version="2", path="replacement",
                        availability=CompatibilityResult(False, "session_type_not_supported", "仅平台可用"))
    calls = []

    async def install(path, config):
        calls.append("install")

    async def uninstall(name):
        calls.append("uninstall")
        return True

    preview = preview_plugin_reconciliation(states, [target])
    assert preview[0]["action"] == "blocked"
    result = await reconcile_plugin_states_async(states, [target], install, uninstall)
    assert not result["ok"]
    assert calls == []
    assert states[0].handle is handle
    assert states[0].applied_spec is old

def _write_plugin(root: Path) -> None:
    """写入最小插件元数据"""
    plugin_dir = root / "demo"
    plugin_dir.mkdir(parents=True)
    payload: dict[str, Any] = {
        "name": "demo",
        "version": "1.0.0",
        "tools": {"search": "搜索"},
        "commands": {"about": "关于"},
    }
    dumped = cast(Any, yaml).safe_dump(payload, allow_unicode=True)
    (plugin_dir / "meta.yaml").write_text(str(dumped), encoding="utf-8")

def test_plugin_spec_normalizes_legacy_and_capability_defaults(tmp_path: Path) -> None:
    """共享规格应兼容字符串插件项并补全能力默认状态"""
    preset = tmp_path / "preset"
    _write_plugin(preset)
    catalog = PluginCatalog(preset_dir=preset, user_dir=tmp_path / "user")

    legacy = parse_plugin_specs(["demo"], catalog, require_available=True)[0]
    configured_value: list[object] = [
        {"name": "demo", "capabilities": {"tools": {"search": False}}}
    ]
    configured = parse_plugin_specs(
        configured_value,
        catalog,
        require_available=True,
    )[0]

    assert legacy.capabilities["tools"]["search"] is True
    assert legacy.capabilities["commands"]["about"] is True
    assert configured.capabilities["tools"]["search"] is False
    assert plugin_specs_fingerprint([legacy]) != plugin_specs_fingerprint([configured])

def test_plugin_spec_rejects_unknown_declared_capability(tmp_path: Path) -> None:
    """已找到插件时不能静默接受未知子能力"""
    preset = tmp_path / "preset"
    _write_plugin(preset)
    catalog = PluginCatalog(preset_dir=preset, user_dir=tmp_path / "user")

    unknown_capability: list[object] = [
        {"name": "demo", "capabilities": {"tools": {"missing": False}}}
    ]
    with pytest.raises(ValueError, match="未声明能力"):
        parse_plugin_specs(
            unknown_capability,
            catalog,
            require_available=True,
        )

    unknown_config: list[object] = [{"name": "demo", "config": {"missing": "value"}}]
    with pytest.raises(ValueError, match="未声明配置项"):
        parse_plugin_specs(
            unknown_config,
            catalog,
            require_available=True,
        )

@pytest.mark.asyncio
async def test_runtime_applies_all_five_capability_kinds_through_plugin_handle() -> None:
    """共享运行协调器应通过插件句柄覆盖五类子能力"""
    calls: list[tuple[str, str]] = []

    class _PluginHandle:
        """记录能力停用调用的插件句柄"""

        def disable_tool(self, name: str) -> bool:
            calls.append(("tools", name))
            return True

        async def disable_skill(self, name: str) -> bool:
            calls.append(("skills", name))
            return True

        def disable_mcp(self, name: str) -> bool:
            calls.append(("mcp", name))
            return True

        def disable_handler(self, name: str) -> bool:
            calls.append(("handlers", name))
            return True

        def disable_command(self, name: str) -> bool:
            calls.append(("commands", name))
            return True

    desired = PluginSpec(
        name="demo",
        capabilities={
            "tools": {"tool": False},
            "skills": {"skill": False},
            "mcp": {"server": False},
            "handlers": {"handler": False},
            "commands": {"command": False},
        },
    )

    changes = await apply_plugin_capabilities_async(_PluginHandle(), desired)

    assert calls == [
        ("tools", "tool"),
        ("skills", "skill"),
        ("handlers", "handler"),
        ("commands", "command"),
        ("mcp", "server"),
    ]
    assert len(changes) == 5

@pytest.mark.asyncio
async def test_runtime_reinstalls_changed_config_and_removes_plugin() -> None:
    """共享协调器应覆盖配置重装和插件删除"""
    installed: list[dict[str, Any]] = []
    uninstalled: list[str] = []

    async def install(_path: str, config: dict[str, Any] | None) -> object:
        installed.append(dict(config or {}))
        return object()

    async def uninstall(name: str) -> bool:
        uninstalled.append(name)
        return True

    initial = PluginSpec(name="demo", path="demo", config={"value": 1})
    states: list[PluginRuntimeState] = []
    first = await reconcile_plugin_states_async(states, [initial], install, uninstall)
    unchanged = await reconcile_plugin_states_async(states, [initial], install, uninstall)
    changed = PluginSpec(name="demo", path="demo", config={"value": 2})
    second = await reconcile_plugin_states_async(states, [changed], install, uninstall)
    third = await reconcile_plugin_states_async(states, [], install, uninstall)

    assert first["ok"] is True
    assert first['timings_ms']['install_ms'] >= 0
    assert unchanged['ok'] is True
    assert 'install_ms' not in unchanged['timings_ms']
    assert 'uninstall_ms' not in unchanged['timings_ms']
    assert second["plugins"][0]["action"] == "reinstall"
    assert second["plugins"][0]["status"] == "applied"
    assert third["plugins"][0]["action"] == "remove"
    assert second['timings_ms']['install_ms'] >= 0
    assert second['timings_ms']['uninstall_ms'] >= 0
    assert third['timings_ms']['uninstall_ms'] >= 0
    assert states == []
    assert installed == [{"value": 1}, {"value": 2}]
    assert uninstalled == ["demo", "demo"]

@pytest.mark.asyncio
async def test_runtime_rolls_back_when_reinstall_fails() -> None:
    """新配置安装失败时应恢复旧插件并保留可重试漂移"""
    installs: list[int] = []

    async def install(_path: str, config: dict[str, Any] | None) -> object:
        value = int((config or {}).get("value", 0))
        installs.append(value)
        if value == 2:
            raise RuntimeError("新配置无效")
        return object()

    async def uninstall(_name: str) -> bool:
        return True

    previous = PluginSpec(name="demo", path="demo", config={"value": 1})
    desired = PluginSpec(name="demo", path="demo", config={"value": 2})
    states = [
        PluginRuntimeState(
            applied_spec=previous,
            desired_spec=previous,
            handle=object(),
            status="loaded",
        )
    ]

    preview = preview_plugin_reconciliation(states, [desired])
    result = await reconcile_plugin_states_async(states, [desired], install, uninstall)

    assert preview[0]["action"] == "reinstall"
    assert result["ok"] is False
    assert result["plugins"][0]["status"] == "rolled_back"
    assert result['timings_ms']['install_ms'] >= 0
    assert result['timings_ms']['uninstall_ms'] >= 0
    assert result["drift"] == 1
    assert states[0].applied_spec == previous
    assert states[0].desired_spec == desired
    assert installs == [2, 1]

@pytest.mark.asyncio
async def test_runtime_cleans_partial_install_when_capability_application_fails() -> None:
    """插件主体安装后子能力失败时应立即卸载半安装实例"""
    uninstalled: list[str] = []

    class _BrokenHandle:
        """拒绝停用工具的插件句柄"""

        def disable_tool(self, _name: str) -> bool:
            return False

    async def install(_path: str, _config: dict[str, Any] | None) -> object:
        return _BrokenHandle()

    async def uninstall(name: str) -> bool:
        uninstalled.append(name)
        return True

    desired = PluginSpec(
        name="demo",
        path="demo",
        capabilities={"tools": {"search": False}},
    )
    states: list[PluginRuntimeState] = []

    result = await reconcile_plugin_states_async(states, [desired], install, uninstall)

    assert result["ok"] is False
    assert result["plugins"][0]["status"] == "error"
    assert result["drift"] == 1
    assert uninstalled == ["demo"]

@pytest.mark.parametrize("session_type,allowed", [("chat", False), ("embedded", False), ("platform", True)])
def test_platform_only(session_type, allowed):
    environment = PluginEnvironment(session_type, "onebot" if session_type == "platform" else None)
    result = check_plugin_compatibility(
        {"applicability": {"session_types": ["platform"], "platforms": "*"}},
        environment,
        "0.1.0",
    )
    assert result.allowed is allowed

def test_version_and_platform_constraints():
    meta = {"compatibility": {"satrap": ">=0.1,<0.2"}, "applicability": {"session_types": ["platform"], "platforms": ["onebot"]}}
    assert check_plugin_compatibility(meta, PluginEnvironment("platform", "onebot"), "0.1.0").allowed
    assert check_plugin_compatibility(meta, PluginEnvironment("platform", "misskey"), "0.1.0").reason_code == "platform_not_supported"
    assert check_plugin_compatibility(meta, PluginEnvironment("platform", "onebot"), "0.2.0").reason_code == "framework_version_mismatch"

@pytest.mark.parametrize("meta", [
    {"compatibility": None},
    {"compatibility": {"satrap": "wrong"}},
    {"applicability": {"session_types": []}},
    {"applicability": {"session_types": ["typo"]}},
    {"applicability": {"platforms": ["*"]}},
    {"applicability": {"hosts": ["chat"]}},
])
def test_invalid_manifest_is_not_unrestricted(meta):
    assert check_plugin_compatibility(meta, PluginEnvironment(), "0.1.0").reason_code == "invalid_manifest"

def test_legacy_manifest_remains_available():
    result = check_plugin_compatibility({}, PluginEnvironment(), "0.1.0")
    assert result.allowed
    assert result.warnings

@pytest.mark.parametrize("session_type,platform", [("platform", None), ("chat", "onebot"), ("typo", None)])
def test_environment_validation(session_type, platform):
    with pytest.raises(ValueError):
        PluginEnvironment(session_type, platform)

@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_incompatible_plugin_never_imported(tmp_path, async_mode):
    from satrap import LLM, AsyncLLM, SimpleSession, AsyncSimpleSession
    from satrap.edictum.plugin_compatibility import PluginCompatibilityError

    plugin = tmp_path / "platform_plugin"
    plugin.mkdir()
    (plugin / "meta.yaml").write_text(
        "name: platform_plugin\napplicability:\n  session_types: [platform]\n",
        encoding="utf-8",
    )
    (plugin / "tools.py").write_text("raise AssertionError('plugin imported')\n", encoding="utf-8")
    if async_mode:
        async_llm = AsyncLLM(api_key="test", model="test")
        async_session = AsyncSimpleSession(
            "compatibility-test", async_llm, db_path=str(tmp_path / "chat.db"),
            plugin_environment=PluginEnvironment("chat"),
        )
        try:
            with pytest.raises(PluginCompatibilityError):
                await async_session.install_plugin(str(plugin))
            assert async_session._wf is None
            assert not async_session.list_plugins()
        finally:
            await async_llm.client.close()
    else:
        sync_llm = LLM(api_key="test", model="test")
        sync_session = SimpleSession(
            "compatibility-test", sync_llm, db_path=str(tmp_path / "chat.db"),
            plugin_environment=PluginEnvironment("chat"),
        )
        try:
            with pytest.raises(PluginCompatibilityError):
                sync_session.install_plugin(str(plugin))
            assert not sync_session.list_plugins()
        finally:
            sync_llm.client.close()

def test_boolean_alias_keeps_switch_boolean_without_repeated_warning(monkeypatch):
    warning = Mock()
    monkeypatch.setattr("satrap.edictum.plugin_config.logger.warning", warning)
    for _ in range(200):
        schema = parse_config_schema({"config_schema": {"summary_enabled": {"type": "boolean", "default": True}}})
        field = schema["summary_enabled"]
        assert field.validate(False) is False and field.validate(True) is True
        assert schema_to_payload(schema)["summary_enabled"]["type"] == "bool"
        with pytest.raises(ValueError):
            field.validate_strict("false")
    warning.assert_not_called()
    parse_config_schema({"config_schema": {"invalid": {"type": "typo"}}})
    warning.assert_called_once()

@pytest.mark.asyncio
async def test_warning_has_one_console_live_and_file_record_with_root_handler(tmp_path: Path, monkeypatch):
    console, root_output = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stderr", console)
    root_handler = logging.StreamHandler(root_output)
    root = logging.getLogger()
    root.addHandler(root_handler)
    logger = Logger("isolated-probe", file_name=str(tmp_path / "probe.log"))
    standard_log_stream.clear()
    subscription = standard_log_stream.subscribe(history_limit=0)
    try:
        logger.warning("重复日志回归标记")
        await asyncio.sleep(0)
        assert console.getvalue().count("重复日志回归标记") == 1
        assert root_output.getvalue() == ""
        assert (tmp_path / "probe.log").read_text(encoding="utf-8").count("重复日志回归标记") == 1
        assert subscription.queue.qsize() == 1
        assert "重复日志回归标记" in subscription.queue.get_nowait().content
    finally:
        standard_log_stream.unsubscribe(subscription.subscription_id)
        logger.close()
        root.removeHandler(root_handler)
