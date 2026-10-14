"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import pytest
from typing import Any
import json
from satrap.core.config.edictum_service import EdictumConfigService
from satrap.core.framework.Base import Session
from satrap.edictum.registry import EdictumTypeDefinition, EdictumTypeRegistry, create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_catalog import PluginCatalog
from copy import deepcopy
from types import SimpleNamespace
from typing import cast
from satrap.core.APICall.LLMCall import AsyncLLM, LLM
from satrap.edictum.simple_session import AsyncSimpleSession, SimpleSession
from satrap.core.framework.providers import EdictumProvider
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.type import SessionConfig, UserCall
from typing import Protocol
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.config.session_class_service import SessionClassConfigService

class _FutureEdictumSession(Session):
    """验证自定义类型无需修改注册表设计的测试会话"""

    def __init__(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.kwargs = kwargs

def test_default_registry_exposes_builtin_types_and_accepts_future_type() -> None:
    """类型注册表应内置 simple 系列并允许独立扩展新 Edictum 类型"""
    registry = create_default_edictum_type_registry()
    registry.register(
        EdictumTypeDefinition(
            name="future",
            factory=_FutureEdictumSession,
            is_async=False,
            description="未来类型",
        )
    )

    names = [item["name"] for item in registry.list_types()]
    created = registry.create("future", session_id="future-1", feature=True)

    assert names == ["async_simple", "future", "simple"]
    assert isinstance(created, _FutureEdictumSession)
    assert created.kwargs["feature"] is True

def test_edictum_cold_config_crud_is_named_and_has_no_placeholders(tmp_path: Path) -> None:
    """Edictum 冷配置应从空对象开始并支持创建, 重命名, 禁用和删除"""
    registry = create_default_edictum_type_registry()
    storage_path = tmp_path / "edictum.json"
    manager = EdictumConfigManager(registry, storage_path)
    service = EdictumConfigService(manager, registry)

    assert service.list_configs() == {}
    assert json.loads(storage_path.read_text(encoding="utf-8")) == {}

    payload: dict[str, Any] = {
            "name": "assistant",
            "edictum_type": "async_simple",
            "model_name": "default",
            "params": {"system_prompt": "你是助手"},
            "plugins": ["session_commands", {"name": "tools", "enabled": False}],
    }
    created = service.create(payload)
    final_name, renamed = service.update(
        "assistant",
        {"name": "platform-assistant", "description": "平台会话"},
    )
    disabled = service.set_enabled(final_name, False)

    assert created["provider"] == "edictum"
    assert created["edictum_type"] == "async_simple"
    assert all(isinstance(plugin, dict) for plugin in created["plugins"])
    assert created["plugins"][0]["name"] == "session_commands"
    assert created["plugins"][0]["capabilities"]["commands"]["about"] is True
    assert final_name == "platform-assistant"
    assert renamed["description"] == "平台会话"
    assert disabled["enabled"] is False
    assert service.get("assistant") is None

    reloaded = EdictumConfigManager(registry, storage_path)
    assert reloaded.get_config("platform-assistant") is not None
    assert service.delete("platform-assistant") is True
    assert service.list_configs() == {}

def test_edictum_cold_config_rejects_unknown_type_and_fields(tmp_path: Path) -> None:
    """Edictum 冷配置应拒绝未知类型和意义不明的请求字段"""
    registry = EdictumTypeRegistry()
    manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    service = EdictumConfigService(manager, registry)

    with pytest.raises(ValueError, match="未知 Edictum 类型"):
        service.create({"name": "bad", "edictum_type": "missing"})
    with pytest.raises(ValueError, match="未知 Edictum 配置字段"):
        service.create({"name": "bad", "edictum_type": "missing", "mystery": 1})

def test_normalization_is_read_only_and_copies_are_independent(tmp_path, monkeypatch):
    """旧格式读取只归一化内存: 每快照一次不回写; 读者与冷注册均返回独立副本"""
    path = tmp_path / 'edictum.json'
    original = json.dumps({' agent ': {'edictum_type': 'async_simple', 'params': {}}})
    path.write_text(original, encoding='utf-8')
    fingerprint = path.stat()
    registry = create_default_edictum_type_registry()
    normalize = EdictumConfigManager._normalize_entry
    calls = []

    def counted(self, entry, catalog=None):
        calls.append(1)
        return normalize(self, entry, catalog)

    monkeypatch.setattr(EdictumConfigManager, '_normalize_entry', counted)
    first = EdictumConfigManager(registry, path)
    first._configs['agent']['description'] = 'private change'
    second = EdictumConfigManager(registry, path)
    second_config = second.get_config('agent')
    assert second_config is not None and second_config['description'] == ''
    assert calls == [1]
    # 读取不改动来源: 内容与指纹保持
    assert path.read_text(encoding='utf-8') == original
    current = path.stat()
    assert (current.st_mtime_ns, current.st_ctime_ns, current.st_size, current.st_ino) == (
        fingerprint.st_mtime_ns, fingerprint.st_ctime_ns, fingerprint.st_size, fingerprint.st_ino)
    second.reload()
    assert calls == [1, 1]
    second.update('agent', {'description': 'saved'})
    assert len(calls) == 4
    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert list(persisted) == ['agent']
    assert persisted['agent']['description'] == 'saved'

    # 冷注册返回独立副本: 入参与返回值的后续修改均不影响存储
    scan_path = tmp_path / "trusted_sessions"
    scan_path.mkdir()
    (scan_path / "future.py").write_text("raise RuntimeError('不应在冷配置阶段导入')\n", encoding="utf-8")
    manager = SessionClassConfigManager(storage_path=tmp_path / "session-classes.json", session_scan_paths=[str(scan_path)])
    source_params = {"model_name": "default"}
    created = manager.register_config_entry("copy-check", "trusted_sessions.future.FutureSession", params=source_params)
    source_params["model_name"] = "mutated"
    created["params"]["model_name"] = "mutated"
    stored = manager.get_config("copy-check")
    assert stored is not None and stored["params"] == {"model_name": "default"}
    stored["params"]["model_name"] = "mutated"
    reloaded = manager.get_config("copy-check")
    assert reloaded is not None and reloaded["params"] == {"model_name": "default"}

def test_external_edit_damage_recovery_and_type_revocation(tmp_path):
    registry = create_default_edictum_type_registry()
    path = tmp_path / 'edictum.json'
    writer = EdictumConfigManager(registry, path)
    writer.create('agent', {'edictum_type': 'async_simple'})
    assert EdictumConfigManager(registry, path).get_config('agent')
    data = json.loads(path.read_text(encoding='utf-8'))
    data['agent']['description'] = 'external'
    replacement = path.with_suffix('.new')
    replacement.write_text(json.dumps(data), encoding='utf-8')
    replacement.replace(path)
    external = EdictumConfigManager(registry, path).get_config('agent')
    assert external is not None and external['description'] == 'external'
    path.write_text('{invalid', encoding='utf-8')
    with pytest.raises(ValueError):
        EdictumConfigManager(registry, path)
    path.write_text(json.dumps(data), encoding='utf-8')
    restored = EdictumConfigManager(registry, path).get_config('agent')
    assert restored is not None and restored['description'] == 'external'
    registry.unregister('async_simple')
    with pytest.raises(ValueError, match='未知 Edictum 类型'):
        EdictumConfigManager(registry, path)

def test_metadata_change_invalidates_normalized_config(tmp_path):
    registry = create_default_edictum_type_registry()
    plugin = tmp_path / 'plugins' / 'probe'
    plugin.mkdir(parents=True)
    meta = plugin / 'meta.yaml'
    meta.write_text('name: probe\ntools:\n  first: first\n', encoding='utf-8')
    catalog = PluginCatalog(tmp_path / 'builtin', tmp_path / 'plugins')
    path = tmp_path / 'edictum.json'
    writer = EdictumConfigManager(registry, path, plugin_catalog=catalog)
    writer.create('agent', {'edictum_type': 'async_simple', 'plugins': ['probe']})
    before = EdictumConfigManager(registry, path, plugin_catalog=catalog)
    before_config = before.get_config('agent')
    assert before_config is not None and before_config['plugins'][0]['capabilities']['tools'] == {'first': True}
    meta.write_text('name: probe\ntools:\n  first: first\n  second: second\n', encoding='utf-8')
    after = EdictumConfigManager(registry, path, plugin_catalog=catalog)
    after_config = after.get_config('agent')
    assert after_config is not None and after_config['plugins'][0]['capabilities']['tools'] == {'first': True, 'second': True}

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("prompt", [None, "新的提示词", ""])
async def test_prompt_restart_preserves_history_and_supports_explicit_clear(tmp_path, asynchronous, prompt):
    """重启会话只替换明确配置的提示词, 清空和保留都不影响对话历史"""
    db_path = str(tmp_path / "context.db")
    if asynchronous:
        old = AsyncSimpleSession("prompt", cast(AsyncLLM, object()), system_prompt="旧提示词", db_path=db_path, enable_checkpoint=False)
        await old.initialize()
        assert old._wf is not None
        await old._wf.ctx.add_user_message("已有消息")
        session = AsyncSimpleSession("prompt", cast(AsyncLLM, object()), system_prompt=prompt, db_path=db_path, enable_checkpoint=False)
        await session.initialize()
        assert session._wf is not None
        messages = session._wf.ctx.get_context()
    else:
        old = SimpleSession("prompt", cast(LLM, object()), system_prompt="旧提示词", db_path=db_path, enable_checkpoint=False)
        old._wf.ctx.add_user_message("已有消息")
        session = SimpleSession("prompt", cast(LLM, object()), system_prompt=prompt, db_path=db_path, enable_checkpoint=False)
        messages = session._wf.ctx.get_context()
    assert [message["content"] for message in messages if message["role"] == "system"] == ["旧提示词" if prompt is None else prompt]
    assert [message["content"] for message in messages if message["role"] == "user"] == ["已有消息"]

@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_platform_settings_reach_sdk_and_survive_model_reload(tmp_path, monkeypatch, asynchronous, stream):
    """平台入口的参数覆盖进入真实请求构造, 流式和模型重载保持一致"""
    requests: list[dict[str, Any]] = []
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="回复"), finish_reason="stop")])
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="回复", tool_calls=None))])

    def create(**kwargs):
        requests.append(deepcopy(kwargs))
        return iter([chunk]) if kwargs.get("stream") else response

    async def chunks():
        yield chunk

    async def acreate(**kwargs):
        requests.append(deepcopy(kwargs))
        return chunks() if kwargs.get("stream") else response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=acreate if asynchronous else create)))
    monkeypatch.setattr("satrap.core.APICall.LLMCall.async_.AsyncOpenAI" if asynchronous else "satrap.core.APICall.LLMCall.sync.OpenAI", lambda **kwargs: client)
    llm_type = AsyncLLM if asynchronous else LLM
    llm = llm_type(api_key="test", model="probe", temperature=0.7, thinking_fields=["reasoning_effort"], suppress_error=False)
    registry = create_default_edictum_type_registry()
    configs = EdictumConfigManager(registry, tmp_path / "edictum.json")
    configs.create("assistant", {"edictum_type": "async_simple" if asynchronous else "simple", "params": {
        "thinking": "high", "temperature": 0, "model_params": {"top_p": 0.8, "max_tokens": 2048}, "stream": stream,
    }})
    provider = EdictumProvider(configs, registry, default_checkpoint_db=str(tmp_path / "context.db"))
    session = provider.create_session(SessionConfig(session_id="request", session_type_name="assistant", provider_name="edictum"), llm)
    call = UserCall(session_id="request", message="测试参数")
    if isinstance(session, AsyncSimpleSession):
        await session.initialize()
        assert await SessionManager._invoke_async_session(session, call) == "回复"
        await session.run("本次关闭思考", thinking="off")
        session.reload_llm(cast(AsyncLLM, llm_type(api_key="test", thinking_fields=["reasoning_effort"], suppress_error=False)))
        await SessionManager._invoke_async_session(session, call)
    else:
        assert isinstance(session, SimpleSession)
        assert SessionManager._invoke_sync_session(session, call) == "回复"
        session.run("本次关闭思考", thinking="off")
        session.reload_llm(cast(LLM, llm_type(api_key="test", thinking_fields=["reasoning_effort"], suppress_error=False)))
        SessionManager._invoke_sync_session(session, call)
    assert len(requests) == 3
    assert [request["extra_body"]["reasoning_effort"] for request in requests] == ["high", "none", "high"]
    for request in requests:
        assert request["temperature"] == 0
        assert request["top_p"] == 0.8
        assert request["max_tokens"] == 2048
        assert bool(request.get("stream")) == stream

@pytest.mark.parametrize("params", [
    {"thinking": "invalid"}, {"model_params": []}, {"model_params": {"api_key": "bad"}},
    {"model_params": {"temperature": True}}, {"model_params": {"temperature": float("nan")}},
    {"model_params": {"temperature": 3}}, {"model_params": {"top_p": 0}},
    {"model_params": {"max_tokens": 2.5}}, {"temperature": 0, "model_params": {"temperature": 1}},
])
def test_invalid_settings_rejected_before_persistence(tmp_path, params):
    """非法子配置应明确报错而不是保存后静默丢弃"""
    configs = EdictumConfigManager(create_default_edictum_type_registry(), tmp_path / "edictum.json")
    with pytest.raises(ValueError):
        configs.create("bad", {"edictum_type": "async_simple", "params": params})
    assert configs.list_configs() == {}

class _SysPathMonkeyPatch(Protocol):
    """声明测试依赖的模块搜索路径修改接口"""

    def syspath_prepend(self, path: str) -> None:
        """将目录加入模块搜索路径开头"""
        ...

def test_session_class_config_service_cold_crud_and_reload(tmp_path: Path):
    """共享服务应在不导入会话类时完成冷配置增删改查"""
    storage_path = tmp_path / "session-classes.json"
    scan_path = tmp_path / "trusted_sessions"
    scan_path.mkdir()
    (scan_path / "future.py").write_text("raise RuntimeError('不应在冷配置阶段导入')\n", encoding="utf-8")
    manager = SessionClassConfigManager(
        storage_path=storage_path,
        session_scan_paths=[str(scan_path)],
    )
    service = SessionClassConfigService(manager)

    created = service.create(
        {
            "name": "cold",
            "class_path": "trusted_sessions.future.FutureSession",
            "is_async": True,
            "description": "冷创建",
            "params": {"model_name": "default"},
        }
    )

    assert created["class_path"] == "trusted_sessions.future.FutureSession"
    assert created["is_async"] is True
    assert service.list_configs()["cold"]["params"] == {"model_name": "default"}

    updated = service.update(
        "cold",
        {
            "name": "renamed",
            "class_path": "trusted_sessions.future.ProviderSession",
            "description": "已重命名",
            "context_key": "room_id",
        },
    )
    service.set_enabled("renamed", False)

    assert updated["description"] == "已重命名"
    assert service.get("cold") is None
    renamed_cfg = service.get("renamed")
    assert renamed_cfg is not None
    assert renamed_cfg["enabled"] is False

    reloaded = SessionClassConfigService(
        SessionClassConfigManager(
            storage_path=storage_path,
            session_scan_paths=[str(scan_path)],
        )
    )
    reloaded_cfg = reloaded.get("renamed")
    assert reloaded_cfg is not None
    assert reloaded_cfg["class_path"] == "trusted_sessions.future.ProviderSession"
    assert reloaded.delete("renamed") is True
    assert reloaded.list_configs() == {}

def test_session_class_config_service_rejects_invalid_and_conflicting_input(tmp_path: Path):
    """共享服务应拒绝非法字段和名称冲突"""
    service = SessionClassConfigService(
        SessionClassConfigManager(storage_path=tmp_path / "session-classes.json")
    )
    service.create({"name": "first", "class_path": "satrap.core.framework.Base.Session"})
    service.create({"name": "second", "class_path": "satrap.core.framework.Base.AsyncSession"})

    with pytest.raises(ValueError, match="名称已存在"):
        service.update("first", {"name": "second"})
    with pytest.raises(ValueError, match="未知会话类配置字段"):
        service.update("first", {"unknown": True})
    with pytest.raises(ValueError, match="params 必须是对象"):
        service.create({"name": "bad", "class_path": "example.Bad", "params": []})

    first_cfg = service.get("first")
    assert first_cfg is not None
    assert first_cfg["class_path"] == "satrap.core.framework.Base.Session"
    second_cfg = service.get("second")
    assert second_cfg is not None
    assert second_cfg["class_path"] == "satrap.core.framework.Base.AsyncSession"

def test_session_class_config_rejects_untrusted_module_without_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """管理接口应在导入前拒绝扫描目录外的模块"""
    marker = tmp_path / "imported.txt"
    module_path = tmp_path / "evil_session.py"
    module_path.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported', encoding='utf-8')\n",
        encoding="utf-8",
    )
    cast(_SysPathMonkeyPatch, monkeypatch).syspath_prepend(str(tmp_path))
    service = SessionClassConfigService(
        SessionClassConfigManager(
            storage_path=tmp_path / "session-classes.json",
            session_scan_paths=[str(tmp_path / "trusted")],
        )
    )

    with pytest.raises(ValueError, match="不在可信代码根"):
        service.create({"name": "evil", "class_path": "evil_session.EvilSession"})

    assert not marker.exists()
