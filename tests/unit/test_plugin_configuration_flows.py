"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
import pytest
from typing import Any, Iterator
import json
from satrap.edictum.plugin_config import ConfigField, PluginConfigManager, parse_config_schema, schema_to_payload
from satrap.core.APICall.LLMCall import LLM
from satrap.core.type import LLMCallResponse, LLMCallStreamEvent
from satrap.edictum import SimpleSession
from satrap.core.config.plugin_service import PluginManagementService
from satrap.display.plugins import ChatPluginRegistry
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.core.config.document import ConfigRevisionConflict
from copy import deepcopy
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace
from collections.abc import Mapping
import weakref
import os
from satrap.core.call_context import CallOrigin, bind_call_origin, bind_tool_workflow
from satrap.core.config.administrator_groups import AdministratorService
from satrap.core.plugin_authorization import AuthorizationDecision, PluginEntryBinding, authorize_plugin_entry
from satrap.core.plugin_authorization import bind_authorization_step, bind_native_command, bind_plugin_factory_tools
from satrap.core.plugin_authorization import evaluate_plugin_permissions
from satrap.core.utils.TCBuilder.manager import ToolsManager
from satrap.core.utils.TCBuilder.tool import Tool
from satrap.edictum.plugin_permissions import PluginPermissions, parse_plugin_permissions, validate_permission_install

class _FakeLLM(LLM):
    """最小同步 fake LLM"""

    def __init__(self) -> None:
        pass

    def call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> LLMCallResponse:
        return LLMCallResponse(type="answer", content="回复")

    def stream_call(
        self, messages: list[dict[str, Any]], model: str | None = None,
        thinking: str = "off", temperature: float | None = None,
        top_p: float | None = None, max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto", img_urls: list[str] | None = None,
    ) -> Iterator[LLMCallStreamEvent]:
        yield LLMCallStreamEvent(kind="content_delta", delta="流")

def test_parse_config_schema_field_validate_and_payload():
    """schema 完整/简写/非法解析, ConfigField 校验归一, payload 可序列化"""
    meta: dict[str, Any] = {
        "config_schema": {
            "sandbox_root": {"type": "path", "default": "", "description": "沙箱根"},
            "search_timeout": {"type": "number", "default": 10},
            "mode": {"type": "select", "default": "a", "options": ["a", "b"]},
            "verbose": {"type": "bool", "default": False},
        }
    }
    schema = parse_config_schema(meta)
    assert set(schema) == {"sandbox_root", "search_timeout", "mode", "verbose"}
    assert schema["sandbox_root"].type == "path"
    assert schema["search_timeout"].default == 10
    assert schema["mode"].options == ["a", "b"]
    assert schema["verbose"].type == "bool"

    shorthand = parse_config_schema({"config_schema": {"plain": "默认值", "bad": {"type": "weird"}}})
    assert shorthand["plain"].type == "string" and shorthand["plain"].default == "默认值"
    assert shorthand["bad"].type == "string"
    assert parse_config_schema({"config_schema": "not-dict"}) == {}
    assert parse_config_schema({}) == {}

    assert ConfigField("n", "number", 10).validate("15") == 15.0
    assert ConfigField("n", "number", 10).validate(3) == 3
    assert ConfigField("b", "bool", False).validate(1) is True
    assert ConfigField("s", "select", "a", options=["a", "b"]).validate("b") == "b"
    assert ConfigField("s", "select", "a", options=["a", "b"]).validate("z") == "a"
    assert ConfigField("p", "path", "/x").validate(None) == "/x"

    payload = schema_to_payload({"root": ConfigField("root", "path", "", "根目录", [])})
    assert payload["root"]["type"] == "path" and payload["root"]["description"] == "根目录"
    json.dumps(payload)

def _schema() -> dict[str, ConfigField]:
    return {
        "sandbox_root": ConfigField("sandbox_root", "path", ""),
        "timeout": ConfigField("timeout", "number", 10),
    }

def test_plugin_config_manager_roundtrip_defaults_and_override(tmp_path: Path):
    """全局写读, 未声明键忽略, 会话覆盖优先, 缺文件全默认"""
    mgr = PluginConfigManager(tmp_path)
    mgr.save_global("p1", _schema(), {"sandbox_root": "/custom"})
    loaded = mgr.load_global("p1", _schema())
    assert loaded["sandbox_root"] == "/custom"
    assert loaded["timeout"] == 10

    mgr.save_global("p2", _schema(), {"ghost": 1, "timeout": 20})
    ignored = mgr.load_global("p2", _schema())
    assert "ghost" not in ignored
    assert ignored["timeout"] == 20

    assert mgr.resolve("p2", _schema(), {"timeout": 99})["timeout"] == 99
    assert mgr.resolve("p2", _schema())["timeout"] == 20

    assert mgr.load_global("ghost", _schema()) == {"sandbox_root": "", "timeout": 10}

@pytest.mark.parametrize("name", ["../outside", "..\\outside", "C:/outside", "..", "bad/name"])
def test_plugin_config_rejects_path_like_names(tmp_path: Path, name: str):
    """插件名称不得把配置读写路径带出配置目录"""
    config_dir = tmp_path / "config"
    mgr = PluginConfigManager(config_dir)

    with pytest.raises(ValueError, match="非法插件名称"):
        mgr.save_global(name, _schema(), {"timeout": 20})
    assert not (tmp_path / "outside.json").exists()

def _make_plugin(tmp_path: Path) -> Path:
    """造一个声明 config_schema + get_tools(session, config) 的插件"""
    plugin_dir = tmp_path / "cfg_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "meta.yaml").write_text(
        "name: cfg_plugin\n"
        "version: 0.1.0\n"
        "config_schema:\n"
        "  root:\n"
        "    type: path\n"
        "    default: ''\n"
        "  timeout:\n"
        "    type: number\n"
        "    default: 10\n",
        encoding="utf-8",
    )
    (plugin_dir / "tools.py").write_text(
        "from satrap.core.utils.TCBuilder import Tool\n"
        "\n"
        "CAPTURED = {}\n"
        "\n"
        "class CfgTool(Tool):\n"
        "    tool_name = 'cfg_tool'\n"
        "    description = 'cfg'\n"
        "    params_dict = {}\n"
        "\n"
        "    def __init__(self, config):\n"
        "        super().__init__()\n"
        "        self._config = config\n"
        "\n"
        "    def execute(self):\n"
        "        return str(self._config)\n"
        "\n"
        "def get_tools(session, config):\n"
        "    CAPTURED.update(config)\n"
        "    return [CfgTool(config)]\n",
        encoding="utf-8",
    )
    return plugin_dir

def test_install_plugin_injects_config(tmp_path: Path, monkeypatch: Any):
    """install_plugin 把合成配置注入 get_tools 工厂 (默认 + 会话覆盖)"""
    plugin_dir = _make_plugin(tmp_path)
    # 全局配置目录隔离
    from satrap.edictum import simple_session as ss_mod
    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))

    s = SimpleSession("c1", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    s.install_plugin(str(plugin_dir), config={"timeout": 42})

    tool = s._wf.tools_manager.tools["cfg_tool"]
    # 从注册的工具实例读注入的配置 (避免依赖模块名/缓存)
    captured = getattr(tool, "_config")
    assert captured["timeout"] == 42   # 会话覆盖
    assert captured["root"] == ""   # schema 默认

def test_install_plugin_config_schema_on_plugin(tmp_path: Path, monkeypatch: Any):
    """安装后 plugin.config_schema 携带序列化声明供前端渲染"""
    plugin_dir = _make_plugin(tmp_path)
    from satrap.edictum import simple_session as ss_mod
    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "cfg"))

    s = SimpleSession("c1", _FakeLLM(), db_path=str(tmp_path / "chat.db"))
    plugin = s.install_plugin(str(plugin_dir))
    assert plugin.config_schema["root"]["type"] == "path"
    assert plugin.config_schema["timeout"]["default"] == 10
    json.dumps(plugin.config_schema)

def test_bool_validate_explicit_mapping_no_fail_open(tmp_path: Path):
    """宽松 bool 分支显式映射: 字符串 "false" 不再被 bool() 误判为 True"""
    field = ConfigField("write_tools_enabled", "bool", False)
    assert field.validate("false") is False
    assert field.validate("off") is False
    assert field.validate("0") is False
    assert field.validate(0) is False
    assert field.validate("true") is True
    assert field.validate("yes") is True
    assert field.validate(1) is True
    # 无法识别的值回退默认 (fail-closed), 不沿用真值语义
    assert field.validate("yesss") is False
    assert field.validate([]) is False
    assert field.validate(2) is False
    assert ConfigField("b", "bool", True).validate("garbage") is True

def test_load_global_bool_string_is_not_fail_open(tmp_path: Path):
    """手改的全局 JSON 中字符串 "false" 加载后仍为 False"""
    import json as _json

    schema = {"write_tools_enabled": ConfigField("write_tools_enabled", "bool", False)}
    config_dir = tmp_path / "cfg"
    mgr = PluginConfigManager(config_dir)
    config_dir.mkdir(parents=True)
    (config_dir / "group_admin.json").write_text(_json.dumps({"write_tools_enabled": "false"}), encoding="utf-8")
    assert mgr.load_global("group_admin", schema)["write_tools_enabled"] is False

def test_catalog_sources_precedence_and_references_without_execution(tmp_path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    for base, name, version in [(builtin, "shared", "builtin"), (user, "shared", "shadow"), (user, "custom", "user")]:
        plugin = base / name
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(f"name: {name}\nversion: {version}\ntools:\n  probe: 测试\n", encoding="utf-8")
        (plugin / "tools.py").write_text("raise RuntimeError('不应执行插件代码')", encoding="utf-8")
    catalog = PluginCatalog(builtin, user)
    registry = ChatPluginRegistry(tmp_path / "chat.json")
    registry.catalog = catalog
    registry.set_enabled("shared", True)
    service = PluginManagementService(catalog, {
        "first": {"plugins": ["shared", "shared"]},
        "second": {"plugins": [{"name": "shared", "enabled": False}, {"name": "custom"}]},
        "empty": {"plugins": []},
    }, registry)
    result = {item["name"]: item for item in service.list_plugins()}
    assert result["shared"]["source"] == "builtin"
    assert result["shared"]["version"] == "builtin"
    assert result["shared"]["usage_count"] == 3
    assert result["shared"]["edictum_configs"] == ["first", "second"]
    assert result["custom"]["source"] == "user"
    assert result["custom"]["usage_count"] == 1
    assert not result["custom"]["chat_enabled"]
    assert result["custom"]["capabilities"]["tools"] == {"probe": "测试"}

def test_enabled_plugin_names_unions_chat_and_named_configs(tmp_path):
    user = tmp_path / "user"
    for name in ["chat_on", "chat_off", "named_on", "named_off", "parent_off", "unused"]:
        plugin = user / name
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(f"name: {name}\nversion: '1'\n", encoding="utf-8")
    catalog = PluginCatalog(tmp_path / "builtin", user)
    registry = ChatPluginRegistry(tmp_path / "chat.json")
    registry.catalog = catalog
    registry.set_enabled("chat_on", True)
    registry.set_enabled("chat_off", True)
    registry.set_enabled("chat_off", False)   # 已配置但停用, 不计入
    service = PluginManagementService(catalog, {
        "on": {"enabled": True, "plugins": ["named_on", {"name": "named_off", "enabled": False}]},
        "off": {"enabled": False, "plugins": ["parent_off"]},   # 配置停用开关不影响, 条目启用即计入
    }, registry)
    assert service.enabled_plugin_names() == {"chat_on", "named_on", "parent_off"}

def test_loaded_plugins_by_platform_follows_written_bindings(tmp_path):
    user = tmp_path / "user"
    for name, extra in [("plain", ""), ("entry_off", ""), ("onebot_only", "applicability:\n  platforms: [onebot]\n")]:
        plugin = user / name
        plugin.mkdir(parents=True)
        (plugin / "meta.yaml").write_text(f"name: {name}\nversion: '1'\n{extra}", encoding="utf-8")
    catalog = PluginCatalog(tmp_path / "builtin", user)
    registry = ChatPluginRegistry(tmp_path / "chat.json")
    registry.catalog = catalog
    service = PluginManagementService(catalog, {
        "main": {"enabled": True, "plugins": ["plain", {"name": "entry_off", "enabled": False}]},
        "disabled_cfg": {"enabled": False, "plugins": ["plain", "onebot_only"]},   # 配置停用仍按已写入条目计入
        "override_cfg": {"plugins": ["onebot_only"]},
    }, registry)
    result = service.loaded_plugins_by_platform([
        {"id": "qq", "type": "onebot", "enable": False,   # 平台停用开关不影响, 按已写入绑定计算
         "session_provider": "edictum", "session_type": "main",
         "session_bindings": {"private": {"mode": "value", "provider": "edictum", "config_name": "override_cfg"},
                              "group": {"mode": "inherit"}}},
        {"id": "tg", "type": "misskey", "session_provider": "edictum", "session_type": "disabled_cfg"},
        {"id": "legacy", "type": "onebot", "session_provider": "session_class", "session_type": "default"},
        {"id": "ghost", "type": "onebot", "session_provider": "edictum", "session_type": "missing_cfg"},
    ])
    assert result["qq"] == {"plain", "onebot_only"}   # 默认绑定与对话类型覆盖取并集, 停用条目不计入
    assert result["tg"] == {"plain"}   # onebot_only 声明不适用 misskey
    assert result["legacy"] == set()   # 会话类绑定没有插件
    assert result["ghost"] == set()    # 绑定指向不存在的命名配置

@pytest.fixture
def configured_service(tmp_path):
    plugin = tmp_path / "user" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "meta.yaml").write_text("""name: probe
tools:
  probe_tool: 测试工具
skills:
  probe_skill: 测试技能
config_schema:
  note:
    type: string
    default: default
  count:
    type: number
    default: 1
    minimum: 0
    maximum: 10
    integer: true
  model:
    type: llm
    default: ''
  optional_model:
    type: llm
    default: ''
    nullable: true
""", encoding="utf-8")
    catalog = PluginCatalog(tmp_path / "builtin", tmp_path / "user")
    chat = ChatPluginRegistry(tmp_path / "chat.json")
    chat.catalog = catalog
    return PluginManagementService(catalog, {}, chat, manager=PluginConfigManager(tmp_path / "config"))

def test_global_parameters_defaults_cas_and_restore(configured_service):
    service = configured_service
    first = service.get_config("probe")
    assert first["updated_at"] is None
    assert first["overrides"] == {}
    assert first["config"]["note"] == "default"
    saved = service.save_config("probe", {"note": "自定义", "count": 0}, first["revision"])
    assert saved["saved"]
    assert saved["updated_at"] is not None
    assert saved["config"]["count"] == 0
    with pytest.raises(ConfigRevisionConflict):
        service.save_config("probe", {"note": "旧页面覆盖"}, first["revision"])
    assert service.get_config("probe")["config"]["note"] == "自定义"
    restored = service.save_config("probe", {}, saved["revision"])
    assert restored["overrides"] == {}
    assert restored["config"]["note"] == "default"
    host = service.manager.host_state("probe")
    switched = service.manager.save_execution("probe", False, host["revision"])
    assert not service.list_plugins()[0]["execution_enabled"]
    assert service.get_config("probe")["config"] == restored["config"]
    assert service.manager.host_state("probe")["updated_at"] == restored["updated_at"]
    service.manager.save_execution("probe", True, switched["revision"])
    assert service.list_plugins()[0]["execution_enabled"]


async def test_plugin_host_execution_http_cas_and_batched_health(configured_service, monkeypatch):
    import asyncio
    import aiohttp
    from satrap.core.backend import control_server
    from satrap.core.server_auth import ServerAuth

    service = configured_service
    token = "host-plugin-test-token-longer-than-32-characters"
    auth = ServerAuth.create("127.0.0.1", 0, token=token)
    monkeypatch.setattr(control_server, "_CONTROL_AUTH", auth)
    monkeypatch.setattr("satrap.edictum.plugin_config.CONFIG_DIR", service.manager._dir)
    monkeypatch.setattr(control_server.PluginCatalog, "snapshot_async", lambda _: asyncio.sleep(0, result=service.catalog))
    monkeypatch.setattr(control_server, "_plugin_runtime_targets", lambda *args: [("Chat", "http://chat/api/chat/plugins/reconcile"), ("Edictum", "http://backend/api/edictum/plugins/reconcile")])
    calls = []

    def snapshot(target, url, name):
        calls.append((target, url, name))
        return {"target": target, "status": "available", "instances": [{"session_id": "test", "plugin": {"name": "probe", "enabled": True, "status": "loaded", "error": ""}}]}

    monkeypatch.setattr(control_server, "_plugin_snapshot_request", snapshot)
    items = [{"name": "probe", "execution_enabled": True, "chat_enabled": True, "edictum_configs": ["worker"]} for _ in range(100)]
    health = await control_server._plugin_health(items)
    assert len(calls) == 2 and all(call[2] is None for call in calls)
    assert all(item["health"]["state"] == "healthy" for item in health)
    monkeypatch.setattr(control_server, "_plugin_runtime_request", lambda target, *args: {"target": target, "status": "next_activation", "sessions": []})
    server = await asyncio.start_server(control_server._handle_request, "127.0.0.1", 0)
    address = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/config/plugins/probe/execution"
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as client:
            async with client.get(address) as response:
                assert response.status == 200
                before = await response.json()
            async with client.put(address, json={"execution_enabled": False, "expected_revision": before["revision"]}) as response:
                assert response.status == 200
                saved = await response.json()
            assert saved["saved"] and not saved["execution_enabled"] and len(saved["runtime"]) == 2
            assert all(item["status"] == "next_activation" for item in saved["runtime"])
            async with client.put(address, json={"execution_enabled": True, "expected_revision": before["revision"]}) as response:
                assert response.status == 409
            async with client.put(address, json={"execution_enabled": "false", "expected_revision": saved["revision"]}) as response:
                assert response.status == 400
            async with client.get(address.replace("probe", "not-installed")) as response:
                assert response.status == 404
        async with aiohttp.ClientSession() as client:
            async with client.get(address) as response:
                assert response.status == 401
    finally:
        server.close()
        await server.wait_closed()

@pytest.mark.parametrize("values", [{"undeclared": "x"}, {"count": -1}, {"count": 11}, {"count": 0.5}, {"note": None}])
def test_invalid_parameters_preserve_previous_file(configured_service, values):
    service = configured_service
    previous = service.get_config("probe")
    with pytest.raises(ValueError):
        service.save_config("probe", values, previous["revision"])
    assert service.get_config("probe") == previous

def test_global_model_reference_validation(configured_service):
    from types import SimpleNamespace
    service = configured_service
    service.models = SimpleNamespace(has_config=lambda kind, name: False)
    before = service.get_config("probe")
    with pytest.raises(ValueError, match="模型配置不存在"):
        service.save_config("probe", {"model": "missing"}, before["revision"])
    assert service.get_config("probe") == before

def test_global_explicit_null_is_preserved(configured_service):
    service = configured_service
    before = service.get_config("probe")
    saved = service.save_config("probe", {"optional_model": None}, before["revision"])
    assert saved["overrides"] == {"optional_model": None}
    assert saved["config"]["optional_model"] is None

def test_chat_usage_disable_preserves_capabilities_and_remove_clears_reference(configured_service):
    service = configured_service
    initial = service.get_usages("probe")["locations"][0]
    assert not initial["present"]
    added = service.save_usage("probe", "chat", "chat", {"present": True, "enabled": True, "capabilities": {"tools": {"probe_tool": False}}}, initial["revision"])["locations"][0]
    disabled = service.save_usage("probe", "chat", "chat", {"present": True, "enabled": False, "capabilities": added["capabilities"]}, added["revision"])["locations"][0]
    assert disabled["present"] and not disabled["enabled"]
    assert disabled["capabilities"]["tools"]["probe_tool"] is False
    assert service.list_plugins()[0]["usage_count"] == 1
    removed = service.save_usage("probe", "chat", "chat", {"present": False, "enabled": False, "capabilities": disabled["capabilities"]}, disabled["revision"])["locations"][0]
    assert not removed["present"]
    assert service.list_plugins()[0]["usage_count"] == 0

def test_chat_usage_rejects_stale_edits_and_unknown_capabilities(configured_service):
    service = configured_service
    initial = service.get_usages("probe")["locations"][0]
    external = ChatPluginRegistry(service.chat.state_path)
    external.catalog = service.catalog
    external.set_enabled("probe", True)
    with pytest.raises(ConfigRevisionConflict):
        service.save_usage("probe", "chat", "chat", {"present": True, "enabled": False, "capabilities": {}}, initial["revision"])
    current = service.get_usages("probe")["locations"][0]
    with pytest.raises(ValueError, match="未声明能力"):
        service.save_usage("probe", "chat", "chat", {"present": True, "enabled": True, "capabilities": {"tools": {"unknown": True}}}, current["revision"])
    assert service.get_usages("probe")["locations"][0]["enabled"]

def test_named_usage_preserves_overrides_and_other_plugins(configured_service, tmp_path, monkeypatch):
    from satrap.edictum.registry import create_default_edictum_type_registry
    from satrap.edictum.config import EdictumConfigManager
    from satrap.core.config.edictum_service import EdictumConfigService

    service = configured_service
    registry = create_default_edictum_type_registry()
    manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    manager.plugin_catalog = service.catalog
    manager.create("assistant", {"edictum_type": "async_simple", "plugins": [{"name": "probe", "config": {"note": "命名覆盖"}}, "other"]})
    named = EdictumConfigService(manager, registry)
    named.plugin_catalog = service.catalog
    monkeypatch.setattr("satrap.core.config.edictum_service.PluginConfigManager", lambda: service.manager)
    service.edictum = named
    service.configs = named.list_configs()
    before = service.get_usages("probe")["locations"][1]
    result = service.save_usage("probe", "edictum", "assistant", {"present": True, "enabled": False, "capabilities": {"skills": {"probe_skill": False}}}, before["revision"])
    after = named.get("assistant")
    target = next(item for item in after["plugins"] if item["name"] == "probe")
    assert target["config"] == {"note": "命名覆盖"}
    assert target["capabilities"]["skills"]["probe_skill"] is False
    assert next(item for item in after["plugins"] if item["name"] == "other")
    location = result["locations"][1]
    manager.update("assistant", {"description": "其他页面修改"})
    with pytest.raises(ConfigRevisionConflict):
        service.save_usage("probe", "edictum", "assistant", {"present": False, "enabled": False, "capabilities": {}}, location["revision"])
    assert len(manager.get_config("assistant")["plugins"]) == 2

def metadata():
    return {"permission_schema_version": 1,
            "config_schema": {"readers": {"type": "textarea"}, "writers": {"type": "textarea"}},
            "tools": {"query": "读取", "write": "修改", "ordinary": "普通"}, "commands": {"manage": "管理"},
            "management_permissions": {
                "read": {"description": "查询", "caller_list": "readers", "empty_policy": "allow", "system_admin": True},
                "write": {"description": "修改", "caller_list": "writers", "empty_policy": "deny", "system_admin": True}},
            "tool_permissions": {"query": ["read"], "write": ["read", "write"]},
            "command_permissions": {"manage": {"default": ["read"], "subcommands": {"approve": ["write"]}}}}

def spec():
    meta = metadata()
    return parse_plugin_permissions(meta, parse_config_schema(meta))

def origin(user="alice"):
    return CallOrigin("p", "bot", "GroupMessage", "room", user, "message", "request")

def administrators(*, excluded=()):
    groups = [{"id": "a", "name": "管理员", "enabled": True,
               "members": [{"platform_id": "p", "user_id": "alice", "platform_instance_id": "one"}],
               "plugin_scope": {"mode": "selected", "included": ["example"], "excluded": list(excluded)}}]
    return AdministratorService(lambda: [{"id": "p", "instance_id": "one"}], groups)

def override_administrators(allow=("example",), user="carol"):
    overrides = [{"id": "solo", "enabled": True, "protect": False, "platform_id": "p", "platform_instance_id": "one",
                  "user_id": user, "allow": list(allow), "deny": []}]
    return AdministratorService(lambda: [{"id": "p", "instance_id": "one"}], [], overrides)

def _write_grant(result):
    """取 write 权限对应的授权条目, 避免 read 的空名单允许规则干扰断言"""
    return next(item for item in result.grants if item.permission == "write")

def test_override_only_grant_is_labelled_as_administrator_override():
    result = evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin("carol"), override_administrators())
    assert result.status == "allowed"
    grant = _write_grant(result)
    assert grant.source == "administrator_override" and grant.override_ids == ("solo",) and grant.group_ids == ()
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin("alice"), override_administrators()).status == "denied"

def test_administrators_fill_multiple_gates_without_changing_config():
    config = {"readers": "bob", "writers": ""}
    before = deepcopy(config)
    result = evaluate_plugin_permissions("example", spec(), "tools", "write", config, origin(), administrators())
    assert result.status == "allowed" and all(item.source == "administrator_group" for item in result.grants)
    assert config == before
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", config, origin("bob"), administrators()).status == "denied"

def test_invalid_config_and_non_platform_identity_cannot_use_admin_fallback():
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": 123}, origin(), administrators()).status == "denied"
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {}, replace(origin(), actor_kind="model"), administrators()).status == "denied"

def test_permissions_may_explicitly_reject_system_admin_grants():
    meta = metadata()
    meta["management_permissions"]["write"]["system_admin"] = False
    rules = parse_plugin_permissions(meta, parse_config_schema(meta))
    assert evaluate_plugin_permissions("example", rules, "tools", "write", {}, origin(), administrators()).status == "denied"

@pytest.mark.parametrize("mutate", [
    lambda m: m.update(permission_schema_version=True),
    lambda m: m["tool_permissions"].update(write=["missing"]),
    lambda m: m["tool_permissions"].update(missing=["write"]),
    lambda m: m["tool_permissions"].update(write=["write", "write"]),
    lambda m: m["management_permissions"]["write"].update(caller_list="missing"),
    lambda m: m["management_permissions"]["write"].update(empty_policy="unknown"),
    lambda m: m["management_permissions"]["write"].update(empty_policy=[]),
    lambda m: m["management_permissions"]["write"].update(system_admin="true"),
    lambda m: m["management_permissions"]["write"].update(unknown=True),
    lambda m: m["management_permissions"]["write"].update(requirements=[True]),
    lambda m: m["management_permissions"]["write"].update(platform_group_admin=None),
    lambda m: m["management_permissions"]["write"].update(platform_group_admin="writers"),
])
def test_invalid_metadata_is_rejected(mutate):
    meta = metadata()
    mutate(meta)
    with pytest.raises(ValueError):
        parse_plugin_permissions(meta, parse_config_schema(meta))

@pytest.mark.parametrize("role,enabled,chat_type,allowed", [
    ("admin", True, "GroupMessage", True), ("owner", True, "GroupMessage", True),
    ("member", True, "GroupMessage", False), ("admin", False, "GroupMessage", False),
    ("admin", True, "FriendMessage", False), ("", True, "GroupMessage", False),
])
def test_platform_group_administrator_grants_are_explicit_and_scoped(role, enabled, chat_type, allowed):
    meta = metadata()
    meta["config_schema"]["group_admins_enabled"] = {"type": "bool", "default": False}
    meta["management_permissions"]["write"]["platform_group_admin"] = "group_admins_enabled"
    rules = parse_plugin_permissions(meta, parse_config_schema(meta))
    result = evaluate_plugin_permissions("example", rules, "tools", "write", {"group_admins_enabled": enabled},
                                         replace(origin(), group_role=role, chat_type=chat_type))
    assert (result.status == "allowed") is allowed
    if allowed:
        assert result.grants[-1].source == "platform_group_admin" and result.grants[-1].group_ids == ("room",)
    assert rules.to_payload()["management_permissions"]["write"]["platform_group_admin"] == "group_admins_enabled"

def test_actual_install_validates_subcommands_and_not_prefix_matches():
    def handler(*args):
        return args
    setattr(handler, "subcommands", ["approve"])
    validate_permission_install(spec(), {"query": object(), "write": object()}, {"manage": handler})
    assert spec().required("commands", "manage", "approve") == ("read", "write")
    assert spec().required("commands", "manage", "approve-all") == ("read",)
    setattr(handler, "subcommands", [])
    with pytest.raises(ValueError):
        validate_permission_install(spec(), {"query": object(), "write": object()}, {"manage": handler})

def test_command_requires_native_scope_and_rejects_model_tool_workflow():
    def handler(*args):
        return args
    binding = PluginEntryBinding("example", "commands", "manage", spec(), {"writers": "alice"}, weakref.ref(handler))
    with bind_call_origin(origin()):
        assert authorize_plugin_entry(binding, subcommand="approve").status == "denied"
        with bind_native_command(binding):
            assert authorize_plugin_entry(binding, subcommand="approve").status == "allowed"
            with bind_tool_workflow(object()):
                assert authorize_plugin_entry(binding, subcommand="approve").status == "denied"

def test_revoked_installed_entry_cannot_reuse_its_binding():
    class Entry:
        def is_enabled(self):
            return True
    entry = Entry()
    installed = SimpleNamespace(name="example", enabled=True, tools={"write": True})
    class Session:
        def __init__(self):
            self._wf = SimpleNamespace(tools_manager=SimpleNamespace(tools={"write": entry}, is_tool_enabled=lambda _: True))
        def list_plugins(self):
            return [installed]
    session: Any = Session()
    binding = PluginEntryBinding("example", "tools", "write", spec(), {"writers": "alice"}, weakref.ref(entry), weakref.ref(session))
    with bind_call_origin(origin()):
        assert authorize_plugin_entry(binding).status == "allowed"
        installed.tools["write"] = False
        assert authorize_plugin_entry(binding).status == "denied"

def test_copied_native_command_context_is_revoked_after_dispatch():
    def handler(*args):
        return args
    binding = PluginEntryBinding("example", "commands", "manage", spec(), {"writers": "alice"}, weakref.ref(handler))
    with bind_call_origin(origin()):
        with bind_native_command(binding):
            copied = copy_context()
            assert copied.run(authorize_plugin_entry, binding, subcommand="approve").status == "allowed"
        assert copied.run(authorize_plugin_entry, binding, subcommand="approve").status == "denied"

def test_business_requirement_descriptions_are_visible_but_never_grant_permission():
    meta = metadata()
    meta["management_permissions"]["write"]["requirements"] = ["需开启功能并等待审批"]
    rules = parse_plugin_permissions(meta, parse_config_schema(meta))
    assert rules.to_payload()["management_permissions"]["write"]["requirements"] == ["需开启功能并等待审批"]
    assert evaluate_plugin_permissions("example", rules, "tools", "write", {}, origin()).status == "denied"

@pytest.mark.parametrize("kind, name", [("tools", "unregistered"), ("commands", "unregistered"), ("unknown", "write")])
def test_unknown_entries_cannot_be_treated_as_ordinary(kind, name):
    result = evaluate_plugin_permissions("example", spec(), kind, name, {}, origin(), administrators())
    assert result.status == "denied" and result.reason_code == "unknown_entry"

def test_authorization_exception_is_logged_and_denied(monkeypatch):
    import satrap.core.platform as platform
    from satrap.core import plugin_authorization as authorization
    from satrap.core.plugin_authorization import PluginPermissionDenied, require_plugin_entry_permission

    class Entry:
        def is_enabled(self):
            return True

    def broken_manager():
        raise RuntimeError("authorization fault injection")

    logged = []
    entry = Entry()
    binding = PluginEntryBinding("example", "tools", "write", spec(), {}, weakref.ref(entry))
    monkeypatch.setattr(platform, "current_adapter_manager", broken_manager)
    monkeypatch.setattr(authorization.logger, "error", logged.append)
    result = authorize_plugin_entry(binding)
    assert result.status == "denied" and result.reason_code == "authorization_error"
    assert logged and "authorization fault injection" in logged[0]
    with pytest.raises(PluginPermissionDenied) as caught:
        require_plugin_entry_permission(binding)
    assert caught.value.code == "authorization_error"
    assert str(caught.value) == "example 管理入口权限检查暂时失败, 请查看后端日志 (authorization_error)"

def test_structural_bad_config_still_reports_invalid_config():
    result = evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": 123}, origin(), administrators())
    assert result.status == "denied" and result.reason_code == "invalid_permission_config"
    denied = evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": "other"}, origin(), administrators(excluded=["example"]))
    assert denied.status == "denied" and denied.reason_code == "permission_denied"

class _EnabledEntry:
    """入口绑定需要的常驻工具替身"""

    def is_enabled(self) -> bool:
        return True

def tool_enabled(name: str) -> bool:
    """模拟仍启用的注册工具"""
    return True

_ENTRY = _EnabledEntry()

def tool_binding() -> PluginEntryBinding:
    """构造普通工具入口"""
    return PluginEntryBinding("example", "tools", "query", spec(), {}, weakref.ref(_ENTRY))

def counted_evaluations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """记录每次真实权限判定的入口与子命令"""
    from satrap.core import plugin_authorization as authorization

    recorded: list[tuple[str, str | None]] = []
    real = authorization.evaluate_plugin_permissions

    def counted(plugin_name: str, spec_value: PluginPermissions, kind: str, name: str, config: Mapping[str, Any],
                call_origin: CallOrigin | None, administrators: AdministratorService | None = None,
                *, subcommand: str | None = None) -> AuthorizationDecision:
        recorded.append((name, subcommand))
        return real(plugin_name, spec_value, kind, name, config, call_origin, administrators, subcommand=subcommand)

    monkeypatch.setattr(authorization, "evaluate_plugin_permissions", counted)
    return recorded

def test_entry_decision_is_evaluated_once_inside_one_step(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    binding = tool_binding()
    with bind_call_origin(origin()), bind_authorization_step():
        first = authorize_plugin_entry(binding)
        second = authorize_plugin_entry(binding)
    assert first is second and first.status == "allowed"
    assert evaluations == [("query", None)]

def test_entry_decision_is_re_evaluated_when_revoked_between_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    class Entry:
        def is_enabled(self) -> bool:
            return True
    entry = Entry()
    installed = SimpleNamespace(name="example", enabled=True, tools={"query": True})

    class Session:
        def __init__(self) -> None:
            self._wf = SimpleNamespace(tools_manager=SimpleNamespace(tools={"query": entry}, is_tool_enabled=tool_enabled))

        def list_plugins(self) -> list[SimpleNamespace]:
            return [installed]

    session: Any = Session()
    evaluations = counted_evaluations(monkeypatch)
    binding = PluginEntryBinding("example", "tools", "query", spec(), {}, weakref.ref(entry), weakref.ref(session))
    with bind_call_origin(origin()):
        with bind_authorization_step():
            assert authorize_plugin_entry(binding).status == "allowed"
            assert authorize_plugin_entry(binding).status == "allowed"
        installed.tools["query"] = False
        with bind_authorization_step():
            assert authorize_plugin_entry(binding).status == "denied"
    assert evaluations == [("query", None)]

def test_copied_step_context_cannot_reuse_revoked_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    binding = tool_binding()
    with bind_call_origin(origin()):
        with bind_authorization_step():
            assert authorize_plugin_entry(binding).status == "allowed"
            copied = copy_context()
        # 步骤结束后身份仍存活: 复制出去的上下文不得沿用已失效作用域的判定
        assert copied.run(authorize_plugin_entry, binding).status == "allowed"
    decision = copied.run(authorize_plugin_entry, binding)
    assert decision.status == "denied" and decision.reason_code == "identity_missing"
    assert evaluations == [("query", None), ("query", None), ("query", None)]

class _AuthorizedTool(Tool):
    """记录每次授权判定结果的工具替身"""

    def __init__(self, binding: PluginEntryBinding, seen: list[AuthorizationDecision]) -> None:
        super().__init__(tool_name="query", description="读取", params_dict={})
        self._plugin_entry_binding = binding
        self._seen = seen

    def is_available_for_call(self) -> bool:
        self._seen.append(authorize_plugin_entry(self._plugin_entry_binding))
        return True

    def execute(self) -> str:
        return "ok"

def test_definition_list_is_stable_and_does_not_evaluate_caller_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    seen: list[AuthorizationDecision] = []
    manager = ToolsManager()
    manager.register_tool(_AuthorizedTool(tool_binding(), seen))
    with bind_call_origin(origin()):
        definitions = manager.get_tools_definitions()
    assert [item["function"]["name"] for item in definitions] == ["query"]
    with bind_call_origin(origin("bob")):
        assert manager.get_tools_definitions() == definitions
    assert evaluations == [] and seen == []

def test_execution_shares_one_authorization_decision_within_each_step(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    seen: list[AuthorizationDecision] = []
    manager = ToolsManager()
    manager.register_tool(_AuthorizedTool(tool_binding(), seen))
    with bind_call_origin(origin()):
        assert manager.execute_tool("query", {}) == "ok"
        assert manager.execute_tool("query", {}) == "ok"
    assert evaluations == [("query", None), ("query", None)]
    assert [decision.status for decision in seen] == ["allowed", "allowed"]
    assert seen[0] is not seen[1]

class _FactoryTool:
    """工厂绑定需要的工具替身, 支持弱引用"""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self._plugin_entry_binding: PluginEntryBinding | None = None

    def get_tool_name(self) -> str:
        return "query"

def _factory_plugin(plugin_dir: Path, name: str = "example") -> Path:
    """写出工厂插件目录的 meta.yaml"""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    meta_path = plugin_dir / "meta.yaml"
    # YAML 是 JSON 的超集, 这里直接用 JSON 写出同一份声明
    meta_path.write_text(json.dumps({"name": name, **metadata()}, ensure_ascii=False), encoding="utf-8")
    return meta_path

def _counted_meta_loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """记录每次真实的 meta.yaml 读取"""
    from satrap.core import plugin_authorization as authorization
    from satrap.edictum import plugin as plugin_module

    monkeypatch.setattr(authorization, "_FACTORY_DECLARATIONS", {})
    recorded: list[str] = []
    real = plugin_module.load_plugin_meta

    def counted(plugin_directory: Path) -> dict[str, Any]:
        recorded.append(str(plugin_directory))
        return real(plugin_directory)

    monkeypatch.setattr(plugin_module, "load_plugin_meta", counted)
    return recorded

def test_factory_declarations_reuse_and_refresh_meta_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """未变化元数据复用声明, 修改或重命名后重新读取并更新绑定"""
    loads = _counted_meta_loads(monkeypatch)
    plugin_dir = tmp_path / "example"
    meta_path = _factory_plugin(plugin_dir)
    first, second = _FactoryTool(), _FactoryTool()
    bind_plugin_factory_tools([first], str(plugin_dir / "tools.py"))
    bind_plugin_factory_tools([second], str(plugin_dir / "tools.py"))
    assert loads == [str(plugin_dir)]
    first_binding, second_binding = first._plugin_entry_binding, second._plugin_entry_binding
    assert first_binding is not None and second_binding is not None
    assert first_binding.plugin_name == "example"
    assert first_binding.permissions is second_binding.permissions

    before = meta_path.stat()
    os.utime(meta_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    touched = _FactoryTool()
    bind_plugin_factory_tools([touched], str(plugin_dir / "tools.py"))
    _factory_plugin(plugin_dir, name="renamed")
    refreshed = _FactoryTool()
    bind_plugin_factory_tools([refreshed], str(plugin_dir / "tools.py"))
    assert loads == [str(plugin_dir)] * 3
    touched_binding, refreshed_binding = touched._plugin_entry_binding, refreshed._plugin_entry_binding
    assert touched_binding is not None and refreshed_binding is not None
    assert touched_binding.plugin_name == "example"
    assert refreshed_binding.plugin_name == "renamed"

def test_factory_declarations_do_not_survive_missing_meta_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loads = _counted_meta_loads(monkeypatch)
    plugin_dir = tmp_path / "example"
    meta_path = _factory_plugin(plugin_dir)
    bind_plugin_factory_tools([_FactoryTool()], str(plugin_dir / "tools.py"))
    meta_path.unlink()
    with pytest.raises(ValueError, match="meta.yaml"):
        bind_plugin_factory_tools([_FactoryTool()], str(plugin_dir / "tools.py"))
    # 文件消失后仍走真实读取并抛出既有错误, 不返回缓存声明
    assert loads == [str(plugin_dir)] * 2
