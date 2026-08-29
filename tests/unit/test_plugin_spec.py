"""共享插件目录和运行规格测试"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_runtime import (
    PluginRuntimeState,
    apply_plugin_capabilities_async,
    preview_plugin_reconciliation,
    reconcile_plugin_states_async,
)
from satrap.edictum.plugin_spec import PluginSpec, parse_plugin_specs, plugin_specs_fingerprint


def _write_plugin(root: Path) -> None:
    """
    写入最小插件元数据

    参数:
    - root: 插件根目录
    """
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
    """
    共享规格应兼容字符串插件项并补全能力默认状态

    参数:
    - tmp_path: 临时目录
    """
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
    """
    已找到插件时不能静默接受未知子能力

    参数:
    - tmp_path: 临时目录
    """
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
    changed = PluginSpec(name="demo", path="demo", config={"value": 2})
    second = await reconcile_plugin_states_async(states, [changed], install, uninstall)
    third = await reconcile_plugin_states_async(states, [], install, uninstall)

    assert first["ok"] is True
    assert second["plugins"][0]["action"] == "reinstall"
    assert second["plugins"][0]["status"] == "applied"
    assert third["plugins"][0]["action"] == "remove"
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
