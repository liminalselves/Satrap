"""权限插件拆分后的生命周期, 迁移和跨插件审批链路"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock
import inspect
import json
import textwrap

import pytest

from satrap.core.approval.contracts import ApprovalRequest, ApprovalResult
from satrap.core.approval.migration import migrate_approval_admin_config
from satrap.core.approval.state import approval_root, session_approval
from satrap.core.call_context import bind_tool_workflow
from satrap.core.plugin_interop.environment import bind_environment, environment_snapshot
from satrap.edictum.approval_migration import migrate_approval_globals, migrate_approval_overrides, migrate_approval_specs
from satrap.edictum.plugin_config import PluginConfigManager
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap import AsyncLLM, LLM


PRESETS = Path(__file__).resolve().parents[2] / "satrap/expend/plugins"


async def _await(value):
    return await value if inspect.isawaitable(value) else value


@pytest.mark.asyncio
async def test_debug_command_group_grant_cannot_change_global_default_or_escape_source(tmp_path, monkeypatch):
    """真实宿主管理组仅授权本群, 私聊和其它群拒绝, 个人授权才允许修改整体默认"""
    from types import SimpleNamespace
    from satrap.core.call_context import CallOrigin, bind_call_origin
    from satrap.core.config.administrator_groups import AdministratorService
    from satrap.core.platform import current_adapter_manager, set_current_adapter_manager
    from satrap.edictum import simple_session as ss_mod
    from satrap.core.log.debug.policy import policy_store
    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugins"))
    configured = {"id": "group-admins", "name": "本群管理员", "enabled": True, "members": [],
                  "chats": [{"platform_id": "qq", "chat_id": "room"}],
                  "plugin_scope": {"mode": "selected", "included": ["debug_logger"], "excluded": []}}
    administrators = AdministratorService(lambda: [{"id": "qq", "type": "onebot", "instance_id": "instance-1"}], [configured])
    previous = current_adapter_manager()
    set_current_adapter_manager(SimpleNamespace(administrator_service=administrators))
    session = AsyncSimpleSession("scoped", MagicMock(spec=AsyncLLM), db_path=str(tmp_path / "platform.db"))
    origin = CallOrigin("qq", "bot", "GroupMessage", "room", "member", "message", "request", conversation_kind="group")
    try:
        await session.install_plugin(str(PRESETS / "debug_logger"))
        with bind_call_origin(origin):
            text, matched = await session.command_handler.process_message("/debug on")
            assert matched and "已开启" in str(text)
            text, _ = await session.command_handler.process_message("/debug default on")
            assert "不能修改" in str(text)
        assert not policy_store().read().default_enabled
        for other in (replace(origin, chat_id="elsewhere"), replace(origin, chat_type="FriendMessage", conversation_kind="private")):
            with bind_call_origin(other):
                text, _ = await session.command_handler.process_message("/debug on")
                assert "已开启" not in str(text)
        personal = {**configured, "id": "personal", "chats": [], "members": [{"platform_id": "qq", "platform_instance_id": "instance-1", "user_id": "member"}]}
        administrators.apply([personal])
        with bind_call_origin(origin):
            text, _ = await session.command_handler.process_message("/debug default on")
            assert "整体 Debug 默认: 开启" in str(text)
        assert policy_store().read().default_enabled
    finally:
        for plugin in list(session.list_plugins()):
            await session.uninstall_plugin(plugin.name)
        set_current_adapter_manager(previous)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_debug_commands_share_persistent_object_policy_and_reject_model_forwarding(tmp_path, monkeypatch, asynchronous):
    """原生 Debug 命令共享宿主设置, 换上下文不丢覆盖, 模型转发与卸载旧入口均不能修改"""
    from satrap.edictum import simple_session as ss_mod
    from satrap.core.log.debug.contracts import DebugObject
    from satrap.core.log.debug.policy import policy_store

    monkeypatch.setenv("SATRAP_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "plugins"))
    cls, llm = (AsyncSimpleSession, MagicMock(spec=AsyncLLM)) if asynchronous else (SimpleSession, MagicMock(spec=LLM))
    session = cls("debug-first", llm, db_path=":memory:")
    obj = DebugObject("chat", "local", "chat", "stable-chat")
    session._debug_object = obj
    registry = session.command_handler if asynchronous else session.cmd_handler

    async def command(value):
        result, matched = await _await(registry.process_message(value))
        assert matched
        return str(result)

    try:
        await _await(session.install_plugin(str(PRESETS / "debug_logger")))
        assert not session.list_tools()
        assert "inherit" in await command("/debug status")
        assert "/new 后继续生效" in await command("/debug on")
        assert policy_store().read().resolve(obj).enabled
        with bind_tool_workflow(object()):
            assert "已设置" not in await command("/debug off")
        assert policy_store().read().resolve(obj).enabled
        assert "整体 Debug 默认: 开启" in await command("/debug default on")
        assert "inherit" in await command("/debug reset")
        assert policy_store().read().resolve(obj).enabled
        await _await(session.uninstall_plugin("debug_logger"))
        assert policy_store().read().resolve(obj).enabled
        replacement = cls("debug-new-context", llm, db_path=":memory:")
        replacement._debug_object = obj
        try:
            await _await(replacement.install_plugin(str(PRESETS / "debug_logger")))
            other_registry = replacement.command_handler if asynchronous else replacement.cmd_handler
            text, matched = await _await(other_registry.process_message("/debug status"))
            assert matched and "开启" in str(text)
        finally:
            for plugin in list(replacement.list_plugins()):
                await _await(replacement.uninstall_plugin(plugin.name))
    finally:
        for plugin in list(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_permission_plugin_lifetime_and_real_coding_execution(tmp_path, monkeypatch, asynchronous):
    """权限插件可独立切换, coding 装卸不丢模式, 计划模式继续约束真实文件操作"""
    from satrap.edictum import simple_session as ss_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    base, llm_type = (AsyncSimpleSession, AsyncLLM) if asynchronous else (SimpleSession, LLM)
    session = base("permissions", MagicMock(spec=llm_type), db_path=str(tmp_path / "chat.db"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bind_environment(session, replace(environment_snapshot(session), workspace_root=workspace, sandbox_root=tmp_path / "sandbox"))

    async def command(text):
        registry = session.command_handler if asynchronous else session.cmd_handler
        result, matched = await _await(registry.process_message(text))
        assert matched
        return str(result)

    try:
        await _await(session.install_plugin(str(PRESETS / "permissions")))
        assert not session.list_tools()
        assert "已切换" in await command("/approve mode full-access")
        engine = session_approval(session)
        assert engine.mode == "full-access"
        with bind_tool_workflow(object()):
            assert "已切换" not in await command("/approve mode user")
        assert engine.mode == "full-access"
        await _await(session.install_plugin(str(PRESETS / "satrap_coding"), config={"workspace_root": str(workspace)}))
        help_text = await command("/help")
        assert "/goal: 设置持续目标并自动推进" in help_text
        assert "/plan: 切换计划模式" in help_text
        assert "/approve: 切换执行审批" in help_text
        assert "None" not in help_text and "可信原生命令" not in help_text
        tool = session.tools_manager.tools["write_file"]
        assert "已写入" in await _await(tool.execute("test.txt", "approved"))
        assert "计划模式" in await command("/plan on")
        assert "计划模式" in await _await(tool.execute("test.txt", "blocked"))
        await _await(session.uninstall_plugin("satrap_coding"))
        assert session_approval(session) is engine and engine.mode == "full-access" and not engine.plan_mode
        assert "已切换" in await command("/approve mode auto")
        await _await(session.uninstall_plugin("permissions"))
        assert session_approval(session) is engine and engine.mode == "auto"
        registry = session.command_handler if asynchronous else session.cmd_handler
        registry.register_command("approve", lambda *args: "occupied")
        engine.set_approval_model("previous-model")
        revision = engine.revision
        with pytest.raises(ValueError, match="冲突"):
            await _await(session.install_plugin(str(PRESETS / "permissions"), config={"approval_model": "failed-model"}))
        assert engine.approval_model == "previous-model" and engine.revision == revision
        registry.unregister_command("approve")
        with pytest.raises(ValueError, match="重启会话"):
            await _await(session.install_plugin(str(PRESETS / "permissions"), config={"data_root": str(tmp_path / "other")}))
        assert session_approval(session) is engine
        await _await(session.install_plugin(str(PRESETS / "permissions")))
        assert "auto" in await command("/approve status")
        assert engine.approval_model == ""
        assert not any(row.get("role") == "user" for row in session.ctx.get_context())
    finally:
        for plugin in list(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_other_plugin_uses_host_approval_without_coding(tmp_path, monkeypatch, asynchronous):
    """第三方插件独立请求审批, 模式变化与卸载撤销旧请求和宿主引用"""
    from satrap.edictum import simple_session as ss_mod

    monkeypatch.setattr(ss_mod, "PluginConfigManager", lambda: PluginConfigManager(tmp_path / "config"))
    prefix, wait, tool_base = ("async ", "await ", "AsyncTool") if asynchronous else ("", "", "Tool")
    root = tmp_path / "consumer"
    root.mkdir()
    (root / "meta.yaml").write_text("name: approval_consumer\ntools: {operation: 请求宿主审批}\n", encoding="utf-8")
    (root / "tools.py").write_text(textwrap.dedent(f'''
        from satrap.edictum import RiskLevel
        from satrap.core.utils.TCBuilder import {tool_base}
        class Operation({tool_base}):
            tool_name = "operation"
            description = "请求宿主审批"
            params_dict = {{}}
            {prefix}def execute(self):
                allowed, reason = {wait}self.host.approvals.request_{"async" if asynchronous else "sync"}(
                    "file_write", RiskLevel.WRITE, "测试动作", {{"path": "test.txt", "content": "payload"}})
                return "approved" if allowed else reason
        def get_tools(session, config=None, *, plugin_host=None):
            tool = Operation()
            tool.host = plugin_host
            return [tool]
    '''), encoding="utf-8")
    session = (AsyncSimpleSession if asynchronous else SimpleSession)(
        "consumer", MagicMock(spec=AsyncLLM if asynchronous else LLM), db_path=str(tmp_path / "chat.db"))
    try:
        await _await(session.install_plugin(str(root)))
        tool = session.tools_manager.tools["operation"]
        assert "approval_required" in await _await(session.tools_manager.execute_tool("operation", {}))
        seen = []
        def human(request):
            seen.append(request)
            return ApprovalResult.for_request(request, "allow", "用户批准本次", "user")
        session.approval_provider = human
        assert await _await(session.tools_manager.execute_tool("operation", {})) == "approved"
        assert seen[-1].tool == "approval_consumer.operation"
        assert seen[-1].payload()["action"]["content"] == "payload"
        def changed(request):
            session_approval(session).set_mode("full-access")
            return human(request)
        session.approval_provider = changed
        assert "approval_required" in await _await(session.tools_manager.execute_tool("operation", {}))
        await _await(session.install_plugin(str(PRESETS / "permissions")))
        session_approval(session).set_mode("user")
        def reload_sync(request):
            session.uninstall_plugin("permissions")
            session.install_plugin(str(PRESETS / "permissions"), config={"approval_model": "new-reviewer"})
            return human(request)
        async def reload_async(request):
            await session.uninstall_plugin("permissions")
            await session.install_plugin(str(PRESETS / "permissions"), config={"approval_model": "new-reviewer"})
            return human(request)
        session.approval_provider = reload_async if asynchronous else reload_sync
        assert "approval_required" in await _await(session.tools_manager.execute_tool("operation", {}))
        assert session_approval(session).approval_model == "new-reviewer"
        await _await(session.uninstall_plugin("approval_consumer"))
        with pytest.raises(Exception, match="失效"):
            tool.host.approvals.snapshot()
    finally:
        for plugin in list(session.list_plugins()):
            await _await(session.uninstall_plugin(plugin.name))


def test_old_config_switches_globals_and_overrides_migrate_once(tmp_path):
    """旧命令开关, 模型配置和实例覆盖迁移, 新插件显式值优先且不重复补回"""
    from satrap.core.config.session_overrides import SessionOverrideStore
    from satrap.display.plugins import ChatPluginRegistry
    from satrap.edictum.plugin_spec import parse_plugin_specs

    catalog = PluginCatalog()
    old = [{"name": "satrap_coding", "config": {"approval_model": "old-model", "shell_timeout": 8},
            "capabilities": {"commands": {"approve": False, "plan": True}}}]
    migrated = migrate_approval_specs(old, catalog)
    coding, permissions = migrated
    assert old[0]["config"]["approval_model"] == "old-model"
    assert coding["config"] == {"shell_timeout": 8} and coding["config_version"] == 1
    assert permissions["config"]["approval_model"] == "old-model"
    assert permissions["capabilities"]["commands"]["approve"] is False
    assert migrate_approval_specs(migrated, catalog) == migrated
    assert migrate_approval_specs([coding], catalog) == [coding]
    explicit = migrate_approval_specs([*old, {"name": "permissions", "enabled": False, "config": {"approval_model": "new-model"}}], catalog)
    assert not explicit[1]["enabled"] and explicit[1]["config"]["approval_model"] == "new-model"
    assert explicit[1]["capabilities"]["commands"]["approve"] is False
    specs = parse_plugin_specs(old, catalog, require_available=True)
    assert {spec.name for spec in specs} == {"permissions", "satrap_coding"}
    from satrap.edictum.plugin_runtime import reconcile_plugin_states

    legacy_root = tmp_path / "legacy-state"
    legacy_root.mkdir()
    (legacy_root / "permissions.json").write_text('{"version": 2, "mode": "auto"}', encoding="utf-8")
    runtime_specs = parse_plugin_specs([{**old[0], "config": {**old[0]["config"], "data_root": str(legacy_root)}}], catalog)
    session = SimpleSession("migrated", MagicMock(spec=LLM), db_path=str(tmp_path / "runtime.db"))
    try:
        result = reconcile_plugin_states([], runtime_specs, session.install_plugin, session.uninstall_plugin, session=session)
        assert result["ok"], result
        assert {plugin.name for plugin in session.list_plugins()} == {"permissions", "satrap_coding"}
        engine = session_approval(session)
        assert engine.mode == "auto" and engine.approval_model == "old-model"
        assert engine.rules_file.parent == legacy_root.resolve()
        assert not session.cmd_handler.is_command_enabled("approve")
    finally:
        for plugin in list(session.list_plugins()):
            session.uninstall_plugin(plugin.name)
    state_path = tmp_path / "chat-plugins.json"
    state_path.write_text(json.dumps({"satrap_coding": {"enabled": True, "capabilities": {"commands": {"approve": False}}}}), encoding="utf-8")
    registry = ChatPluginRegistry(state_path)
    states = {item["name"]: item for item in registry.scan()}
    assert states["permissions"]["enabled"]
    assert not states["permissions"]["capabilities"]["commands"][0]["enabled"]
    manager = PluginConfigManager(tmp_path / "config")
    manager.global_path("satrap_coding").parent.mkdir(parents=True)
    manager.global_path("satrap_coding").write_text(json.dumps({"approval_model": "global-old", "shell_timeout": 9}), encoding="utf-8")
    manager.global_path("permissions").write_text(json.dumps({"approval_model": "global-new"}), encoding="utf-8")
    migrate_approval_globals(manager)
    assert json.loads(manager.global_path("permissions").read_text(encoding="utf-8"))["approval_model"] == "global-new"
    assert json.loads(manager.global_path("satrap_coding").read_text(encoding="utf-8")) == {"shell_timeout": 9}
    store = SessionOverrideStore(tmp_path / "platform.db")
    store.replace("session", "plugins.satrap_coding", {"approval_model": "instance-old", "shell_timeout": 10}, expected_revision=0)
    assert migrate_approval_overrides(store, "session")
    assert store.read("session", "plugins.permissions")["overrides"] == {"approval_model": "instance-old"}
    assert not migrate_approval_overrides(store, "session")
    assert store.read("session", "plugins.satrap_coding")["overrides"] == {"shell_timeout": 10}


def test_old_mode_logs_and_admin_denials_preserved(tmp_path, monkeypatch):
    """保留旧模式和审计日志, 管理员授权与否决一起迁移, 后续撤销不会复活"""
    from types import SimpleNamespace

    session = SimpleNamespace(session_id="legacy")
    bind_environment(session, replace(environment_snapshot(session), session_root=tmp_path / "session"))
    legacy = tmp_path / "session/cache/satrap_coding"
    legacy.mkdir(parents=True)
    original = b'{"version": 2, "mode": "auto"}'
    (legacy / "permissions.json").write_bytes(original)
    (legacy / "approval_log.jsonl").write_text('{"old": true}\n', encoding="utf-8")
    engine = session_approval(session)
    assert engine.mode == "auto" and engine.rules_file.parent == approval_root(session)
    assert engine.log_file.read_bytes() == (legacy / "approval_log.jsonl").read_bytes()
    assert (legacy / "permissions.json").read_bytes() == original
    engine.set_mode("user")
    assert session_approval(session, legacy_root=legacy).mode == "user"
    from satrap.core.approval import state as approval_state

    monkeypatch.setattr(approval_state, "get_project_root", lambda: tmp_path)
    embedded = SimpleNamespace(session_id="embedded-legacy")
    embedded_legacy = approval_root(embedded).parent
    embedded_legacy.mkdir(parents=True)
    (embedded_legacy / "permissions.json").write_bytes(original)
    assert session_approval(embedded).mode == "auto"
    groups = [{"plugin_scope": {"mode": "selected", "included": ["satrap_coding"], "excluded": []}}]
    overrides = [{"allow": [], "deny": ["satrap_coding"]}]
    migrated = migrate_approval_admin_config({"administrator_groups": groups, "administrator_overrides": overrides})
    assert migrated["administrator_groups"][0]["plugin_scope"]["included"] == ["satrap_coding", "permissions"]
    assert migrated["administrator_overrides"][0]["deny"] == ["satrap_coding", "permissions"]
    migrated["administrator_groups"][0]["plugin_scope"]["included"].remove("permissions")
    assert migrate_approval_admin_config(migrated) == migrated
    from satrap.core.backend.BackendManager import BackendConfig
    from satrap.core.config.administrator_groups import AdministratorService
    from satrap.core.call_context import CallOrigin
    from satrap.core.config.document import load_config_document, save_config_document

    instance = "a" * 32
    old_config = {"platforms": [{"id": "qq", "type": "onebot", "instance_id": instance}],
                  "administrator_groups": [{"id": "hosts", "name": "宿主管理员", "enabled": True,
                      "members": [{"platform_id": "qq", "platform_instance_id": instance, "user_id": user}
                                  for user in ("allowed", "denied")],
                      "plugin_scope": {"mode": "selected", "included": ["satrap_coding"], "excluded": []}}],
                  "administrator_overrides": [{"id": "blocked", "enabled": True, "platform_id": "qq",
                      "platform_instance_id": instance, "user_id": "denied", "allow": [], "deny": ["satrap_coding"]}]}
    config = BackendConfig.from_dict(old_config)
    administrators = AdministratorService(lambda: config.platforms, config.administrator_groups, config.administrator_overrides)
    for user, expected in (("allowed", True), ("denied", False), ("stranger", False)):
        origin = CallOrigin("qq", "bot", "GroupMessage", "group", user, "msg", "request")
        assert administrators.resolve(origin, "permissions").allowed is expected
    path = tmp_path / "config.json"
    save_config_document(path, old_config)
    saved = load_config_document(path)
    assert saved["approval_plugin_migration"] == 1
    saved["administrator_groups"][0]["plugin_scope"]["included"].remove("permissions")
    save_config_document(path, saved)
    assert "permissions" not in load_config_document(path)["administrator_groups"][0]["plugin_scope"]["included"]
