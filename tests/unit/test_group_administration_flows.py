"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import AsyncMock
from typing import Any, cast
import asyncio
from aiocqhttp.exceptions import ActionFailed
import pytest
import yaml
from satrap.expend.plugins.group_admin.tools import _DEFINITIONS, AsyncGroupAdminTool, get_tools
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.framework.Base import Session
from satrap.core.platform import PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig
from satrap.core.platform import current_adapter_manager, set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from support.administration import _async_tools, _origin, _setup_adapter
from dataclasses import replace
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.utils.TCBuilder.manager import ToolsManager
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.framework.providers import EdictumProvider
from satrap.core.config.group_store import GroupConfigConflict
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.edictum.config import EdictumConfigManager
from satrap.core.type import SessionConfig
from satrap.edictum import AsyncSimpleSession as _group_admin_roles_AsyncSimpleSession
from pathlib import Path
import sqlite3
from satrap.core.config.group_actions import GroupActionStore
from satrap.core.config.platform_schema import PLATFORM_SCHEMA_VERSION
from satrap.core.log import logger
from satrap.core.platform.onebot.admin import AdminActionUnconfirmed
from satrap.core.platform.onebot.group_action_types import normalize_action_params
from satrap.core.platform.receipt import SendReceipt
from satrap.edictum.simple_session.async_ import AsyncSimpleSession as _group_actions_AsyncSimpleSession
from satrap.expend.plugins.group_admin.tools import _build_tools

@pytest.fixture()
def _clear_manager():
    yield
    set_current_adapter_manager(None)

@pytest.mark.usefixtures('_clear_manager')
def test_meta_declares_exactly_the_implemented_tools():
    from pathlib import Path
    meta_path = Path(__file__).parent.parent.parent / "satrap" / "expend" / "plugins" / "group_admin" / "meta.yaml"
    meta = cast(dict[str, Any], yaml.safe_load(meta_path.read_text(encoding="utf-8")))
    assert set(meta["tools"]) == set(_DEFINITIONS)

@pytest.mark.usefixtures('_clear_manager')
def test_meta_write_switch_parses_to_real_bool():
    from pathlib import Path
    from typing import Any, cast
    from satrap.edictum.plugin_config import parse_config_schema
    meta_path = Path(__file__).parent.parent.parent / "satrap" / "expend" / "plugins" / "group_admin" / "meta.yaml"
    schema = parse_config_schema(cast(dict[str, Any], yaml.safe_load(meta_path.read_text(encoding="utf-8"))))
    field = schema["write_tools_enabled"]
    assert field.type == "bool"
    assert field.validate(True) is True and field.validate(False) is False
    assert field.validate(None) is False

@pytest.mark.usefixtures('_clear_manager')
class TestToolConstruction:
    def test_sync_session_gets_sync_tools(self):
        tools = get_tools(cast(Session, object()), {})
        assert len(tools) == len(_DEFINITIONS) == 14
        assert not any(isinstance(t, type) for t in tools)
        ban = next(t for t in tools if t.tool_name == "group_admin_ban")
        assert ban.recovery_policy == "manual"
        info = next(t for t in tools if t.tool_name == "group_admin_get_honors")
        assert info.recovery_policy == "retry"
        definition = ban.get_tool_defined()
        assert definition["function"]["parameters"]["required"] == ["user_id"]
        assert definition["function"]["parameters"]["additionalProperties"] is False

    def test_async_session_gets_async_tools(self):
        tools = _async_tools({})
        assert len(tools) == 14
        assert all(asyncio.iscoroutinefunction(t.execute) for t in tools)

@pytest.mark.usefixtures('_clear_manager')
class TestPermissionGate:
    @pytest.mark.asyncio
    async def test_missing_origin_is_rejected(self):
        _setup_adapter()
        tool = next(t for t in get_tools(cast(Session, object()), {}) if t.tool_name == "group_admin_get_honors")
        result = await asyncio.get_running_loop().run_in_executor(None, lambda: tool.execute())
        assert result["ok"] is False and result["error_type"] == "permission_denied" and "来源身份" in result["error"]

    @pytest.mark.asyncio
    async def test_write_requires_explicit_enable(self):
        _setup_adapter()
        with bind_call_origin(_origin()):
            tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_kick")
            result = await tool.execute(user_id="321")
        assert result["ok"] is False and result["error_type"] == "permission_denied" and "写操作" in result["error"]

    @pytest.mark.asyncio
    async def test_caller_and_group_allowlists(self):
        _setup_adapter()
        tool = next(t for t in _async_tools({"write_tools_enabled": True, "allowed_callers": "999"}) if t.tool_name == "group_admin_kick")
        with bind_call_origin(_origin(actor="123")):
            result = await tool.execute(user_id="321")
        assert result["ok"] is False and result["error_type"] == "permission_denied" and "调用者" in result["error"]
        tool = next(t for t in _async_tools({"allowed_callers": "123", "write_tools_enabled": True, "allowed_groups": "789"}) if t.tool_name == "group_admin_kick")
        with bind_call_origin(_origin()):
            result = await tool.execute(user_id="321")
        assert result["ok"] is False and result["error_type"] == "permission_denied" and "目标群" in result["error"]

    @pytest.mark.asyncio
    async def test_private_context_requires_explicit_group(self):
        _setup_adapter()
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_honors")
        with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
            result = await tool.execute()
        assert result["ok"] is False and result["error_type"] == "invalid_arguments" and "group_id" in result["error"]

@pytest.mark.usefixtures('_clear_manager')
class TestExecution:
    @pytest.mark.asyncio
    async def test_ban_executes_with_current_group_and_parsed_params(self):
        adapter = _setup_adapter()
        adapter._bot.set_group_ban.return_value = {}
        config = {"allowed_callers": "123", "write_tools_enabled": True}
        tool = next(t for t in _async_tools(config) if t.tool_name == "group_admin_ban")
        with bind_call_origin(_origin()):
            result = await tool.execute(user_id="321", duration=600)
        assert result == {"ok": True}
        adapter._bot.set_group_ban.assert_awaited_once_with(group_id=456, user_id=321, duration=600)








    @pytest.mark.asyncio
    async def test_sync_tool_bridges_to_platform_loop(self):
        adapter = _setup_adapter()
        adapter._loop = asyncio.get_running_loop()
        adapter._bot.set_group_kick.return_value = {}
        config = {"allowed_callers": "123", "write_tools_enabled": True}
        tool = next(t for t in get_tools(cast(Session, object()), config) if t.tool_name == "group_admin_kick")

        def run() -> dict[str, object]:
            with bind_call_origin(_origin()):
                return tool.execute(user_id="321")

        result = await asyncio.to_thread(run)
        assert result == {"ok": True}
        adapter._bot.set_group_kick.assert_awaited_once_with(group_id=456, user_id=321, reject_add_request=False)

    @pytest.mark.asyncio
    async def test_sync_tool_without_loop_reports_error(self):
        _setup_adapter()
        config = {"allowed_callers": "123", "write_tools_enabled": True}
        tool = next(t for t in get_tools(cast(Session, object()), config) if t.tool_name == "group_admin_kick")
        with bind_call_origin(_origin()):
            result = await asyncio.to_thread(tool.execute, user_id="321")
        assert result["ok"] is False and "事件循环" in result["error"]

    @pytest.mark.asyncio
    async def test_sync_tool_with_not_running_loop_fails_fast(self):
        adapter = _setup_adapter()
        adapter._loop = asyncio.new_event_loop()
        adapter._bot.set_group_kick.return_value = {}
        config: dict[str, Any] = {"allowed_callers": "123", "write_tools_enabled": True}
        tool = next(t for t in get_tools(cast(Session, object()), config) if t.tool_name == "group_admin_kick")
        started = asyncio.get_running_loop().time()
        try:
            with bind_call_origin(_origin()):
                result = await asyncio.to_thread(tool.execute, user_id="321")
        finally:
            adapter._loop.close()
        assert asyncio.get_running_loop().time() - started < 1
        assert result["ok"] is False and result["error_type"] == "invalid_arguments" and "事件循环" in result["error"]
        adapter._bot.set_group_kick.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_tool_inside_platform_loop_fails_fast(self):
        adapter = _setup_adapter()
        adapter._loop = asyncio.get_running_loop()
        adapter._bot.set_group_kick.return_value = {}
        config: dict[str, Any] = {"allowed_callers": "123", "write_tools_enabled": True}
        tool = next(t for t in get_tools(cast(Session, object()), config) if t.tool_name == "group_admin_kick")
        started = asyncio.get_running_loop().time()
        with bind_call_origin(_origin()):
            result = tool.execute(user_id="321")
        assert asyncio.get_running_loop().time() - started < 1
        assert result["ok"] is False and result["error_type"] == "invalid_arguments" and "事件循环" in result["error"]
        adapter._bot.set_group_kick.assert_not_called()

@pytest.mark.usefixtures('_clear_manager')
class TestConversationKind:
    @pytest.mark.asyncio
    async def test_group_conversation_kind_uses_current_group(self):
        from dataclasses import replace
        adapter = _setup_adapter()
        adapter._bot.get_group_honor_info.return_value = {}
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_honors")
        origin = replace(_origin(chat_type=""), conversation_kind="group", conversation_id="456")
        with bind_call_origin(origin):
            result = await tool.execute()
        assert result["ok"] is True, result
        assert adapter._bot.get_group_honor_info.await_args.kwargs["group_id"] == 456

    @pytest.mark.asyncio
    async def test_private_conversation_kind_requires_explicit_group(self):
        from dataclasses import replace
        adapter = _setup_adapter()
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_honors")
        origin = replace(_origin(), conversation_kind="private", conversation_id="123")
        with bind_call_origin(origin):
            result = await tool.execute()
        assert result["ok"] is False and result["error_type"] == "invalid_arguments" and "group_id" in result["error"]
        adapter._bot.get_group_honor_info.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_request_requires_request_id_and_rejects_raw_credentials(self):
        adapter = _setup_adapter()
        adapter._bot.set_group_add_request.return_value = {}
        adapter.bot_self_id = "10000"
        await adapter.request_flags.register(
            "group", "f1", self_id=adapter.bot_self_id, group_id="456", sub_type="add", user_id="1",
        )
        config = {"allowed_callers": "123", "write_tools_enabled": True, "request_managers": "123"}
        tool = next(t for t in _async_tools(config) if t.tool_name == "group_admin_handle_group_request")
        with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
            bad = await tool.execute(flag="f1", sub_type="other", approve=True, group_id="456")
            missing = await tool.execute(approve=True)
            row = (await adapter.request_flags.list_requests("group", self_id="10000"))["items"][0]
            ok = await tool.execute(request_id=row["request_id"], approve=False, reason="拒绝", group_id="456")
        assert bad["ok"] is False and bad["error_type"] == "invalid_arguments"
        assert missing["ok"] is False and missing["error_type"] == "invalid_arguments"
        assert ok["ok"] is True
        adapter._bot.set_group_add_request.assert_awaited_once_with(flag="f1", sub_type="add", approve=False, reason="拒绝")

    @pytest.mark.asyncio
    async def test_group_request_rechecks_account_after_waiting(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _setup_adapter()
        adapter.bot_self_id = "10000"
        adapter._bot.set_group_add_request.return_value = {}
        await adapter.request_flags.register(
            "group", "f1", self_id="10000", group_id="456", sub_type="add", user_id="1",
        )
        listing = await adapter.request_flags.list_requests("group", self_id="10000", group_id="456")
        request_id = cast(dict[str, Any], listing["items"][0])["request_id"]
        original = adapter.request_flags.resolve_request

        async def mutate(kind: str, value: str, *, self_id: str, **options: Any) -> dict[str, Any]:
            row = await original(kind, value, self_id=self_id, **options)
            adapter.bot_self_id = "99999"
            return row

        monkeypatch.setattr(adapter.request_flags, "resolve_request", mutate)
        config: dict[str, Any] = {"allowed_callers": "123", "write_tools_enabled": True, "request_managers": "123"}
        tool = next(t for t in _async_tools(config) if t.tool_name == "group_admin_handle_group_request")
        with bind_call_origin(_origin()):
            result = await tool.execute(request_id=request_id, approve=True)
        assert result["ok"] is False and result["error_type"] == "permission_denied" and "账号" in result["error"]
        adapter._bot.set_group_add_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_write_uses_approval_handler(self):
        adapter = _setup_adapter()
        adapter.bot_self_id = "10000"
        adapter.group_action_handler = AsyncMock(return_value={"state": "pending", "action_id": "action-model-1"})
        await adapter.request_flags.register(
            "friend", "friend-flag", self_id="10000", user_id="321",
        )
        adapter._bot.set_friend_add_request.return_value = {}
        tools = _async_tools({"allowed_callers": "123", "write_tools_enabled": True, "request_managers": "123"})
        group = next(tool for tool in tools if tool.tool_name == "group_admin_set_name")
        with bind_call_origin(_origin()):
            pending = await group.execute(name="新群名")
        assert pending["ok"] is True and pending["data"]["state"] == "pending"
        adapter.group_action_handler.assert_awaited_once_with("456", "set_group_name", {"name": "新群名"})
        adapter._bot.set_friend_add_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_platform_returns_unsupported(self):
        from satrap.core.platform import PlatformAdapter
        from satrap.core.platform.event import PlatformMetadata

        class _Bare(PlatformAdapter):
            adapter_type = "bare"

            async def run(self) -> None:
                return None

            def meta(self) -> PlatformMetadata:
                return PlatformMetadata(name="bare", id="ob")

        registry = PlatformAdapterRegistry()
        registry.register("bare", _Bare)
        manager = PlatformAdapterManager(registry=registry)
        assert manager.add_adapter(PlatformConfig(id="ob", type="bare")) is not None
        set_current_adapter_manager(manager)
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_honors")
        with bind_call_origin(_origin()):
            result = await tool.execute()
        assert result["error_type"] == "unsupported"

@pytest.mark.usefixtures('_clear_manager')
class TestLoggingAndTimeout:
    @pytest.mark.asyncio
    async def test_write_action_audit_and_failure_logged(self, caplog: pytest.LogCaptureFixture):
        import logging
        adapter = _setup_adapter()
        adapter._bot.set_group_kick.return_value = {}
        tool = next(t for t in _async_tools({"allowed_callers": "123", "write_tools_enabled": True}) if t.tool_name == "group_admin_kick")
        with caplog.at_level(logging.DEBUG), bind_call_origin(_origin()):
            assert (await tool.execute(user_id="321"))["ok"] is True
            adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 1200})
            rejected = await tool.execute(user_id="321")
            assert rejected["ok"] is False and rejected["error_type"] == "rejected"
            denied = await next(t for t in _async_tools({}) if t.tool_name == "group_admin_kick").execute(user_id="321")
        assert denied["ok"] is False
        messages = [r.getMessage() for r in caplog.records if not r.name.endswith("_file")]
        assert any("写动作完成 tool=group_admin_kick actor=123 chat=456" in m for m in messages)
        assert any("动作被拒绝 tool=group_admin_kick" in m and "retcode=1200" in m for m in messages)
        assert any("权限拒绝 tool=group_admin_kick" in m for m in messages)

    @pytest.mark.asyncio
    async def test_sync_tool_timeout_cancels_and_reports_unconfirmed(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
        import logging
        from satrap.core.platform import loop_bridge
        adapter = _setup_adapter()
        adapter._loop = asyncio.get_running_loop()
        started = asyncio.Event()

        async def slow(**kwargs: object) -> dict[str, object]:
            started.set()
            await asyncio.sleep(30)
            return {}

        adapter._bot.set_group_kick = slow
        original = loop_bridge.asyncio.run_coroutine_threadsafe

        def fast_timeout(coro: Any, loop: Any) -> Any:
            future: Any = original(coro, loop)
            real_result = future.result
            future.result = lambda timeout=None: real_result(timeout=0.05)
            return future

        monkeypatch.setattr(loop_bridge.asyncio, "run_coroutine_threadsafe", fast_timeout)
        tool = next(t for t in get_tools(cast(Session, object()), {"allowed_callers": "123", "write_tools_enabled": True}) if t.tool_name == "group_admin_kick")

        def run() -> Any:
            with bind_call_origin(_origin()):
                return tool.execute(user_id="321")

        with caplog.at_level(logging.WARNING):
            result = await asyncio.to_thread(run)
        assert result["ok"] is False and result["error_type"] == "unconfirmed"
        assert any("动作超时已取消 tool=group_admin_kick" in r.getMessage() for r in caplog.records)

@pytest.mark.usefixtures('_clear_manager')
class TestFailureEnvelope:
    @pytest.mark.asyncio
    async def test_unexpected_failure_hides_internals_and_keeps_flat_shape(self, monkeypatch: pytest.MonkeyPatch,
                                                                          caplog: pytest.LogCaptureFixture):
        """未预期异常按 execution_error 归一, 内部细节只进日志"""
        import logging
        adapter = _setup_adapter()

        def broken(gid: str, honor_type: str = "all") -> dict[str, Any]:
            raise RuntimeError("内部细节 D:/secret")

        monkeypatch.setattr(adapter.admin, "get_group_honor_info", broken)
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_honors")
        with caplog.at_level(logging.ERROR), bind_call_origin(_origin()):
            result = await tool.execute()
        assert result == {"ok": False, "error": "管理操作暂不可用, 请查看后端日志",
                          "error_type": "execution_error", "tool_name": "group_admin_get_honors"}
        assert "D:/secret" not in str(result)
        assert any("内部细节 D:/secret" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_platform_unknown_result_is_reported_as_unconfirmed(self):
        """平台结果未知按 unconfirmed 归一, 不当作成功也不泄露响应正文"""
        adapter = _setup_adapter()
        adapter._bot.get_group_honor_info.side_effect = RuntimeError("内部细节 D:/secret")
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_honors")
        with bind_call_origin(_origin()):
            result = await tool.execute()
        assert result["ok"] is False and result["error_type"] == "unconfirmed"
        assert result["tool_name"] == "group_admin_get_honors" and "D:/secret" not in str(result)

@pytest.fixture
async def runtime(tmp_path):
    previous = current_adapter_manager()
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{
        "id": "bot", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings,
    }]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    adapter._bot.get_group_member_info.return_value = {"group_id": 456, "user_id": 123, "role": "admin"}
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.patch_account("100", expected_revision=1, mode="all", approval_defaults={})
    for group in ("456", "789"):
        directory.confirm_membership("100", group, True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    backend._attach_request_ledger(adapter)
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create("assistant", {"edictum_type": "async_simple", "model_name": "base", "plugins": [{
        "name": "group_admin", "config": {"group_admins_enabled": True, "write_tools_enabled": True, "high_risk_approval": True},
    }]})
    provider = EdictumProvider(config_manager, registry, default_checkpoint_db=str(backend.platform_db_path("bot")))
    manager = SessionManager(db_path=backend.platform_db_path("bot"), platform_id="bot")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="qq-admin-session", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session = provider.create_session(cfg, llm=cast(Any, object()))
    assert isinstance(session, _group_admin_roles_AsyncSimpleSession)
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.pool.put("qq-admin-session", session, "assistant")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    adapter.group_action_handler = lambda group, action, params: backend.submit_group_action(
        "bot", "100", group, "qq-admin-action", action, params, actor_kind="model",
    )
    set_current_adapter_manager(backend._adapter_mgr)
    try:
        await provider.prepare_session_async(session)
        assert session._wf is not None
        yield backend, adapter, session._wf.tools_manager, config_manager
    finally:
        await provider.release_session_async(session)
        set_current_adapter_manager(previous)

def origin(role="admin"):
    return CallOrigin("bot", "100", "GroupMessage", "456", "123", "message", "request", group_role=role)

@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_qq_administrator_uses_normal_tool_dispatch_and_approval(runtime, role):
    backend, adapter, tools, _ = runtime
    adapter._bot.get_group_member_info.return_value["role"] = role
    with bind_call_origin(origin(role)):
        result = await tools.execute_tool("group_admin_ban", {"user_id": "42", "duration": 60})
    assert result["ok"] and result["data"]["state"] == "pending"
    adapter._bot.set_group_ban.assert_not_awaited()
    approved = await backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True)
    assert approved["state"] == "succeeded"
    adapter._bot.set_group_ban.assert_awaited_once_with(group_id=456, user_id=42, duration=60)
    assert adapter._bot.get_group_member_info.await_count >= 2
    assert all(call.kwargs == {"group_id": 456, "user_id": 123, "no_cache": True}
               for call in adapter._bot.get_group_member_info.await_args_list)

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role", "switch", "group_scope", "account", "generation"])
async def test_pending_qq_administrator_action_cannot_execute_after_revocation(runtime, change):
    backend, adapter, tools, config_manager = runtime
    with bind_call_origin(origin()):
        result = await tools.execute_tool("group_admin_ban", {"user_id": "42"})
    assert result["data"]["state"] == "pending"
    if change == "role":
        adapter._bot.get_group_member_info.return_value["role"] = "member"
    elif change == "account":
        adapter.bot_self_id = "200"
    elif change == "generation":
        adapter._connection_generation += 1
    else:
        config_manager.update("assistant", {"plugins": [{"name": "group_admin", "config": {
            "group_admins_enabled": change != "switch", "write_tools_enabled": True, "high_risk_approval": True,
            "allowed_groups": "789" if change == "group_scope" else "",
        }}]})
    if change in {"account", "generation"}:
        with pytest.raises(ValueError if change == "account" else GroupConfigConflict):
            await backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True)
    else:
        approved = await backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True)
        assert approved["state"] == "failed"
    adapter._bot.set_group_ban.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["member", "private", "cross_group", "wrong_member", "wrong_group", "unavailable"])
async def test_qq_role_does_not_grant_other_members_or_other_groups(runtime, case):
    _, adapter, tools, _ = runtime
    source = origin("member" if case == "member" else "admin")
    params = {"user_id": "42"}
    if case == "private":
        source = replace(source, chat_type="FriendMessage", chat_id="123")
        params["group_id"] = "456"
    elif case == "cross_group":
        params["group_id"] = "789"
    elif case == "wrong_member":
        adapter._bot.get_group_member_info.return_value["user_id"] = 999
    elif case == "wrong_group":
        adapter._bot.get_group_member_info.return_value["group_id"] = 789
    elif case == "unavailable":
        adapter._bot.get_group_member_info.side_effect = RuntimeError("平台故障")
    with bind_call_origin(source):
        result = await tools.execute_tool("group_admin_ban", params)
    assert result["ok"] is False
    adapter._bot.set_group_ban.assert_not_awaited()

@pytest.mark.asyncio
async def test_role_is_rechecked_after_action_waits_to_send(runtime, monkeypatch):
    backend, adapter, tools, _ = runtime
    with bind_call_origin(origin()):
        assert (await tools.execute_tool("group_admin_ban", {"user_id": "42"}))["data"]["state"] == "pending"
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter.admin.ban_group_member

    async def delayed(group_id, user_id, duration=1800):
        entered.set()
        await release.wait()
        await original(group_id, user_id, duration)

    monkeypatch.setattr(adapter.admin, "ban_group_member", delayed)
    task = asyncio.create_task(backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True))
    await entered.wait()
    adapter._bot.get_group_member_info.return_value["role"] = "member"
    release.set()
    result = await task
    assert result["state"] == "failed" and result["result"]["reason"] == "model_permission_revoked"
    adapter._bot.set_group_ban.assert_not_awaited()

@pytest.mark.asyncio
async def test_qq_administrator_can_query_and_approve_only_member_applications(runtime):
    backend, adapter, tools, _ = runtime
    await adapter.request_flags.register("group", "member-request", self_id="100", group_id="456", sub_type="add", user_id="42")
    await adapter.request_flags.register("group", "bot-invitation", self_id="100", group_id="456", sub_type="invite", user_id="42")
    with bind_call_origin(origin()):
        listing = await tools.execute_tool("group_admin_list_group_requests", {})
        assert listing["ok"] and listing["data"]["total"] == 1
        result = await tools.execute_tool("group_admin_handle_group_request", {
            "request_id": listing["data"]["items"][0]["request_id"], "approve": True,
        })
    assert result["ok"] and result["data"]["state"] == "pending"
    approved = await backend.decide_group_action("bot", "100", "456", "qq-admin-action", approve=True)
    assert approved["state"] == "succeeded"
    adapter._bot.set_group_add_request.assert_awaited_once_with(flag="member-request", sub_type="add", approve=True, reason="")

@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,write_enabled,current_role,success", [
    (True, True, "admin", True), (False, True, "admin", False),
    (True, False, "admin", False), (True, True, "member", False),
])
async def test_synchronous_qq_administrator_calls_are_verified_on_platform_loop(runtime, enabled, write_enabled, current_role, success):
    _, adapter, _, _ = runtime
    adapter._loop = asyncio.get_running_loop()
    adapter.group_action_handler = None
    adapter._bot.get_group_member_info.return_value["role"] = current_role
    tool = next(tool for tool in get_tools(cast(Session, object()), {
        "group_admins_enabled": enabled, "write_tools_enabled": write_enabled,
    }) if tool.tool_name == "group_admin_ban")
    manager = ToolsManager()
    manager.register_tool(tool)
    with bind_call_origin(origin()):
        result = await asyncio.to_thread(manager.execute_tool, "group_admin_ban", {"user_id": "42", "duration": 60})
    assert result["ok"] is success
    if success:
        adapter._bot.set_group_ban.assert_awaited_once_with(group_id=456, user_id=42, duration=60)
        assert adapter._bot.get_group_member_info.await_count == 2
    else:
        adapter._bot.set_group_ban.assert_not_awaited()

def test_pending_decision_is_atomic_and_cannot_replay(tmp_path: Path) -> None:
    store = GroupActionStore(tmp_path / "platform.db")
    params = {"user_id": "42", "reject_add_request": False}
    record, created = store.submit(
        "action-0001", "10000", "456", "kick_group_member", params,
        "panel", 7, approval_required=True,
    )
    assert created and record["state"] == "pending"
    duplicate, created = store.submit(
        "action-0001", "10000", "456", "kick_group_member", params,
        "panel", 7, approval_required=True,
    )
    assert not created and duplicate == record
    with pytest.raises(GroupConfigConflict):
        store.submit("action-0001", "10000", "456", "kick_group_member",
                     {"user_id": "43"}, "panel", 7, approval_required=True)
    occupied = store.decide("10000", "456", "action-0001", approve=True, policy_revision=7)
    assert occupied["state"] == "executing"
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-0001", approve=True, policy_revision=7)
    settled = store.settle("10000", "456", "action-0001", "succeeded", "platform_confirmed")
    assert settled["state"] == "succeeded"
    with pytest.raises(GroupConfigConflict):
        store.settle("10000", "456", "action-0001", "succeeded", "platform_confirmed")

@pytest.mark.parametrize("details", [
    {"retcode": True}, {"retcode": "100"}, {"retcode": 2 ** 32},
    {"message": "bad\nresponse"}, {"message": "x" * 513}, {"message": ""},
    {"data": {"token": "secret"}}, {"reason": "override"}, {},
])
def test_invalid_result_diagnostics_do_not_settle_action(tmp_path: Path, details: dict[str, object]) -> None:
    store = GroupActionStore(tmp_path / "platform.db")
    store.submit("action-diagnostic", "100", "456", "set_group_card",
                 {"user_id": "100", "card": ""}, "panel", 1, approval_required=False)
    with pytest.raises(ValueError):
        store.settle("100", "456", "action-diagnostic", "failed", "AdminActionRejected", details)
    pending = store.get("100", "456", "action-diagnostic")
    assert pending is not None and pending["state"] == "executing" and pending["result"] is None
    saved = store.settle("100", "456", "action-diagnostic", "failed", "AdminActionRejected", {
        "retcode": 100, "message": "平台拒绝执行该动作 (retcode=100)",
    })
    assert GroupActionStore(tmp_path / "platform.db").get("100", "456", "action-diagnostic") == saved

def test_expiration_and_policy_change_persist_after_conflict(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    for action_id in ("action-expire", "action-policy"):
        store.submit(action_id, "10000", "456", "set_group_whole_ban",
                     {"enable": True}, "panel", 3, approval_required=True)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE group_actions SET expires_at=0 WHERE action_id='action-expire'")
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-expire", approve=True, policy_revision=3)
    expired = store.get("10000", "456", "action-expire")
    assert expired is not None and expired["state"] == "expired"
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-policy", approve=True, policy_revision=4)
    policy = store.get("10000", "456", "action-policy")
    assert policy is not None
    assert policy["state"] == "expired"
    assert policy["result"]["reason"] == "policy_changed"

def test_executing_recovers_unknown_without_retry(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    store.submit("action-0002", "10000", "456", "leave_group",
                 {"dismiss": False}, "model", 2, approval_required=False)
    recovered = GroupActionStore(database, recover=True)
    record = recovered.get("10000", "456", "action-0002")
    assert record is not None
    assert record["state"] == "unknown"
    assert record["result"]["reason"] == "interrupted_restart"
    with pytest.raises(GroupConfigConflict):
        recovered.settle("10000", "456", "action-0002", "succeeded", "platform_confirmed")

def test_pending_model_action_expires_when_source_is_lost_on_restart(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    origin = {"adapter_id": "bot", "self_id": "100", "chat_type": "GroupMessage",
              "chat_id": "456", "actor_id": "123", "session_id": "sid", "tool_name": "group_admin_kick"}
    record, _ = store.submit("model-restart-1", "100", "456", "kick_group_member", {"user_id": "42"},
                             "model", 1, approval_required=True, model_origin=origin)
    assert record["state"] == "pending"
    assert "model_origin" not in record
    assert store.model_origin("100", "456", "model-restart-1") == origin
    recovered = GroupActionStore(database, recover=True)
    expired = recovered.get("100", "456", "model-restart-1")
    assert expired is not None and expired["state"] == "expired"
    assert expired["result"]["reason"] == "model_source_lost_on_restart"

def test_v2_action_database_migrates_without_losing_records(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    store.submit("legacy-action-1", "100", "456", "set_group_name", {"name": "旧群名"},
                 "panel", 1, approval_required=True)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE group_actions DROP COLUMN model_origin_json")
        for table in ("memory_refs", "memory_proposals", "memory_operations", "memory_audit", "memories",
                      "group_chat_reminder_attempts", "group_chat_reminders", "reminder_operations"):
            connection.execute(f"DROP TABLE {table}")
        for table in ("friend_actions", "friend_policies", "group_chat_assets", "group_chat_summary_refs", "group_chat_summaries", "group_chat_summary_snapshots",
                      "platform_message_policy", "platform_message_backups", "platform_messages", "platform_message_chats"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 2")
    migrated = GroupActionStore(database)
    record = migrated.get("100", "456", "legacy-action-1")
    assert record is not None and record["state"] == "pending"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION
        assert "model_origin_json" in {row[1] for row in connection.execute("PRAGMA table_info(group_actions)")}

@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["write", "caller", "group", "tool"])
async def test_pending_model_action_rechecks_current_permission(tmp_path: Path, revocation: str) -> None:
    """审批不能替已撤销的模型工具权限补授权"""
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[
        {"id": "bot", "type": "onebot", "settings": settings},
    ]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.confirm_membership("100", "456", True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
        "bot", "100", gid, "model-pending-1", action, params, actor_kind="model",
    )
    set_current_adapter_manager(backend._adapter_mgr)
    config = {"write_tools_enabled": True, "allowed_callers": "123", "allowed_groups": "456"}
    tool = next(item for item in _build_tools(AsyncGroupAdminTool, config) if item.tool_name == "group_admin_kick")
    origin = CallOrigin("bot", "100", "GroupMessage", "456", "123", "message-1", "request-1")
    try:
        with bind_call_origin(origin):
            submitted = await tool.execute(user_id="42")
        assert submitted["data"]["state"] == "pending"
        adapter._bot.set_group_kick.assert_not_awaited()
        if revocation == "write":
            config["write_tools_enabled"] = False
        elif revocation == "caller":
            config["allowed_callers"] = "999"
        elif revocation == "group":
            config["allowed_groups"] = "789"
        else:
            tool.disable()
        result = await backend.decide_group_action("bot", "100", "456", "model-pending-1", approve=True)
        assert result["state"] == "failed"
        assert result["result"]["reason"] == "model_permission_revoked"
        adapter._bot.set_group_kick.assert_not_awaited()
    finally:
        set_current_adapter_manager(None)

@pytest.mark.asyncio
async def test_pending_model_action_uses_latest_source_group_plugin_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """群 A 停用管理插件后不能批准它提交给群 B 的旧动作"""
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[
        {"id": "bot", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings},
    ]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.patch_account("100", expected_revision=1, mode="all", approval_defaults={})
    for group_id in ("456", "789"):
        directory.confirm_membership("100", group_id, True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create("assistant", {
        "edictum_type": "async_simple", "model_name": "base",
        "plugins": [{"name": "group_admin", "enabled": True, "config": {
            "write_tools_enabled": True, "allowed_callers": "123", "allowed_groups": "789",
        }}],
    })
    provider = EdictumProvider(config_manager, registry, default_checkpoint_db=str(backend.platform_db_path("bot")))
    manager = SessionManager(db_path=backend.platform_db_path("bot"), platform_id="bot")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="source-session", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session = provider.create_session(cfg, llm=cast(Any, object()))
    assert isinstance(session, _group_actions_AsyncSimpleSession)
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.pool.put("source-session", session, "assistant")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
        "bot", "100", gid, "source-group-action", action, params, actor_kind="model",
    )
    set_current_adapter_manager(backend._adapter_mgr)
    try:
        await provider.prepare_session_async(session)
        assert session._wf is not None
        tool = session._wf.tools_manager.tools["group_admin_kick"]
        origin = CallOrigin("bot", "100", "GroupMessage", "456", "123", "message-1", "request-1")
        with bind_call_origin(origin):
            submitted = await tool.execute(group_id="789", user_id="42")
        assert submitted["data"]["state"] == "pending"
        directory.patch_group("100", "456", "session", {"plugins": {"mode": "value", "value": [
            {"name": "group_admin", "mode": "disabled"},
        ]}}, expected_revision=0)
        result = await backend.decide_group_action("bot", "100", "789", "source-group-action", approve=True)
        assert result["state"] == "failed" and result["result"]["reason"] == "model_permission_revoked"
        adapter._bot.set_group_kick.assert_not_awaited()
        directory.patch_group("100", "456", "session", {"plugins": {"mode": "value", "value": [
            {"name": "group_admin", "mode": "enabled"},
        ]}}, expected_revision=1)
        adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
            "bot", "100", gid, "race-source-action", action, params, actor_kind="model",
        )
        with bind_call_origin(origin):
            race = await tool.execute(group_id="789", user_id="42")
        assert race["data"]["state"] == "pending"
        entered = asyncio.Event()
        release = asyncio.Event()
        original_kick = adapter.admin.kick_group_member

        async def delayed_kick(group_id: Any, user_id: Any, reject_add_request: bool = False) -> None:
            entered.set()
            await release.wait()
            await original_kick(group_id, user_id, reject_add_request)

        monkeypatch.setattr(adapter.admin, "kick_group_member", delayed_kick)
        decision = asyncio.create_task(backend.decide_group_action(
            "bot", "100", "789", "race-source-action", approve=True,
        ))
        await entered.wait()
        directory.patch_group("100", "456", "session", {"plugins": {"mode": "value", "value": [
            {"name": "group_admin", "mode": "disabled"},
        ]}}, expected_revision=2)
        release.set()
        race_result = await decision
        assert race_result["state"] == "failed" and race_result["result"]["reason"] == "model_permission_revoked"
        adapter._bot.set_group_kick.assert_not_awaited()
        adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
            "bot", "100", gid, "invalid-source-action", action, params, actor_kind="model",
        )
        with sqlite3.connect(backend.platform_db_path("bot")) as connection:
            connection.execute("UPDATE group_configs SET config_json='{' WHERE self_id='100' AND group_id='456'")
        with bind_call_origin(origin):
            denied = await tool.execute(group_id="789", user_id="42")
        assert denied["ok"] is False
        assert GroupActionStore(backend.platform_db_path("bot")).get("100", "789", "invalid-source-action") is None
    finally:
        await provider.release_session_async(session)
        set_current_adapter_manager(None)

@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["GroupMessage", "FriendMessage"])
async def test_model_invite_uses_occupied_flag_for_unjoined_group(tmp_path: Path, chat_type: str) -> None:
    """未入群邀请由请求账本证明归属, 同一 flag 的第二个模型动作不能调用平台"""
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[
        {"id": "bot", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings},
    ]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    backend._attach_request_ledger(adapter)
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.patch_account("100", expected_revision=1, mode="all", approval_defaults={
        "handle_group_request": "approval_required",
    })
    directory.confirm_membership("100", "456", True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    assert not adapter.allows_management_target("999")

    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create("assistant", {
        "edictum_type": "async_simple", "model_name": "base",
        "plugins": [{"name": "account_requests", "enabled": True, "config": {
            "allowed_callers": "123", "allowed_invitation_groups": "999", "request_managers": "123",
        }}],
    })
    provider = EdictumProvider(config_manager, registry, default_checkpoint_db=str(backend.platform_db_path("bot")))
    manager = SessionManager(db_path=backend.platform_db_path("bot"), platform_id="bot")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="invite-session", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session = provider.create_session(cfg, llm=cast(Any, object()))
    assert isinstance(session, _group_actions_AsyncSimpleSession)
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.pool.put("invite-session", session, "assistant")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    origin = CallOrigin("bot", "100", chat_type, "456" if chat_type == "GroupMessage" else "123", "123", "message-1", "request-1")
    set_current_adapter_manager(backend._adapter_mgr)
    try:
        await provider.prepare_session_async(session)
        assert session._wf is not None
        tool = session._wf.tools_manager.tools["account_requests_handle_group_invite"]

        await adapter.request_flags.register(
            "group", "panel-invite", self_id="100", group_id="999", sub_type="invite", user_id="123",
        )
        panel_row = (await backend.account_group_invites("bot", "100"))["items"][0]
        panel_params = {"request_id": panel_row["request_id"], "expected_revision": panel_row["revision"], "approve": True}
        panel = await backend.submit_account_group_invite("bot", "100", panel_params, actor_kind="panel", action_id="panel-invite-action")
        assert panel["state"] == "succeeded"
        assert await backend.submit_account_group_invite("bot", "100", panel_params, actor_kind="panel", action_id="panel-invite-action") == panel

        await adapter.request_flags.register(
            "group", "model-invite", self_id="100", group_id="999", sub_type="invite", user_id="123",
        )
        adapter.account_request_handler = lambda params: backend.submit_account_group_invite(
            "bot", "100", params, actor_kind="model", action_id="model-invite-action",
        )
        row = (await backend.account_group_invites("bot", "100"))["items"][0]
        with bind_call_origin(origin):
            submitted = await tool.execute(request_id=row["request_id"], approve=True)
        assert submitted["data"]["state"] == "pending"
        model_result = await backend.decide_account_invite("bot", "100", "model-invite-action", approve=True)
        assert model_result["state"] == "succeeded"

        await adapter.request_flags.register(
            "group", "race-invite", self_id="100", group_id="999", sub_type="invite", user_id="123",
        )
        action_ids = iter(("race-invite-first", "race-invite-second"))
        adapter.account_request_handler = lambda params: backend.submit_account_group_invite(
            "bot", "100", params, actor_kind="model", action_id=next(action_ids),
        )
        row = (await backend.account_group_invites("bot", "100"))["items"][0]
        with bind_call_origin(origin):
            first = await tool.execute(request_id=row["request_id"], approve=True)
            second = await tool.execute(request_id=row["request_id"], approve=True)
        assert first["data"]["state"] == second["data"]["state"] == "pending"

        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_request(**kwargs: Any) -> dict[str, Any]:
            if kwargs["flag"] == "race-invite":
                entered.set()
                await release.wait()
            return {}

        adapter._bot.set_group_add_request.side_effect = delayed_request
        first_decision = asyncio.create_task(backend.decide_group_action(
            "bot", "100", "999", "race-invite-first", approve=True,
        ))
        await entered.wait()
        second_result = await backend.decide_group_action(
            "bot", "100", "999", "race-invite-second", approve=True,
        )
        assert second_result["state"] == "failed"
        assert second_result["result"]["reason"] == "AdminActionRejected"
        release.set()
        first_result = await first_decision
        assert first_result["state"] == "succeeded"
        calls = adapter._bot.set_group_add_request.await_args_list
        assert [call.kwargs["flag"] for call in calls] == ["panel-invite", "model-invite", "race-invite"]
        entry = adapter.request_flags.ledger.lookup("bot", "100", "group", "race-invite")
        assert entry is not None and entry["state"] == "completed"
        await adapter.request_flags.register(
            "group", "revoked-invite", self_id="100", group_id="999", sub_type="invite", user_id="123",
        )
        adapter.account_request_handler = lambda params: backend.submit_account_group_invite(
            "bot", "100", params, actor_kind="model", action_id="revoked-invite-action",
        )
        row = (await backend.account_group_invites("bot", "100"))["items"][0]
        with bind_call_origin(origin):
            pending = await tool.execute(request_id=row["request_id"], approve=True)
        assert pending["data"]["state"] == "pending"
        config_manager.update("assistant", {"plugins": [{"name": "account_requests", "enabled": True, "config": {
            "allowed_callers": "123", "allowed_invitation_groups": "999", "request_managers": "",
        }}]})
        assert getattr(tool, "config")["request_managers"] == "123"
        revoked = await backend.decide_group_action("bot", "100", "999", "revoked-invite-action", approve=True)
        assert revoked["state"] == "failed" and revoked["result"]["reason"] == "model_permission_revoked"
        assert adapter._bot.set_group_add_request.await_count == 3
    finally:
        await provider.release_session_async(session)
        set_current_adapter_manager(None)

def test_group_request_flag_never_enters_persistent_parameters(tmp_path: Path) -> None:
    params, flag = normalize_action_params(
        "handle_group_request", {"flag": "secret-flag", "sub_type": "invite", "approve": False}, "10000",
    )
    assert flag == "secret-flag"
    assert "flag" not in params and "flag_digest" in params
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    store.submit("action-0003", "10000", "456", "handle_group_request",
                 params, "panel", 1, approval_required=True)
    assert b"secret-flag" not in database.read_bytes()
    recovered = GroupActionStore(database, recover=True)
    record = recovered.get("10000", "456", "action-0003")
    assert record is not None and record["state"] == "expired"
    assert record["result"]["reason"] == "request_flag_lost_on_restart"

@pytest.mark.asyncio
async def test_approved_card_rejection_persists_safe_diagnostics_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(
        data_root=str(tmp_path), platforms=[{"id": "bot", "type": "onebot", "settings": settings}],
    ))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    client = AsyncMock()
    adapter._bot = client
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.confirm_membership("100", "456", True)
    directory.patch_group("100", "456", "approval", {
        "set_group_card": {"mode": "value", "value": "approval_required"},
    }, expected_revision=0)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    warnings: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(logger, "warning", warnings.append)
    monkeypatch.setattr(logger, "error", errors.append)
    params: dict[str, object] = {"user_id": "100", "card": ""}
    pending = await backend.submit_group_action(
        "bot", "100", "456", "clear-card-rejected", "set_group_card", params, actor_kind="panel",
    )
    assert pending["state"] == "pending"
    client.set_group_card.assert_not_awaited()
    client.set_group_card.side_effect = ActionFailed({
        "retcode": 100, "message": "secret-response", "data": {"token": "secret-token"},
    })
    rejected = await backend.decide_group_action("bot", "100", "456", "clear-card-rejected", approve=True)
    assert rejected["state"] == "failed"
    assert rejected["result"] == {
        "reason": "AdminActionRejected", "retcode": 100, "message": "平台拒绝执行该动作 (retcode=100)",
    }
    client.set_group_card.assert_awaited_once_with(group_id=456, user_id=100, card="")
    assert any("clear-card-rejected" in line and "retcode=100" in line for line in warnings)
    assert "secret" not in str(rejected) + str(warnings) + str(errors)
    replay = await backend.submit_group_action(
        "bot", "100", "456", "clear-card-rejected", "set_group_card", params, actor_kind="panel",
    )
    assert replay == rejected
    with pytest.raises(GroupConfigConflict):
        await backend.decide_group_action("bot", "100", "456", "clear-card-rejected", approve=True)
    client.set_group_card.assert_awaited_once()
    assert GroupActionStore(backend.platform_db_path("bot")).get("100", "456", "clear-card-rejected") == rejected

    original = adapter.admin.set_group_card
    monkeypatch.setattr(adapter.admin, "set_group_card", AsyncMock(side_effect=RuntimeError("unexpected-failure")))
    await backend.submit_group_action(
        "bot", "100", "456", "card-unexpected", "set_group_card", params, actor_kind="panel",
    )
    unknown = await backend.decide_group_action("bot", "100", "456", "card-unexpected", approve=True)
    assert unknown["state"] == "unknown"
    assert any("card-unexpected" in line and "Traceback" in line and "unexpected-failure" in line for line in errors)

    monkeypatch.setattr(adapter.admin, "set_group_card", original)
    client.set_group_card.side_effect = None
    client.set_group_card.return_value = {}
    await backend.submit_group_action(
        "bot", "100", "456", "card-next-action", "set_group_card", params, actor_kind="panel",
    )
    success = await backend.decide_group_action("bot", "100", "456", "card-next-action", approve=True)
    assert success["state"] == "succeeded"
    assert client.set_group_card.await_count == 2

@pytest.mark.asyncio
async def test_runtime_approval_and_auto_mode_execute_exactly_once(tmp_path: Path) -> None:
    """关闭响应的已加入群仍可管理, 人工批准前没有平台副作用"""
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(
        data_root=str(tmp_path), platforms=[{"id": "bot", "type": "onebot", "settings": settings}],
    ))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.confirm_membership("100", "456", True)
    directory.patch_group("100", "456", "approval", {
        "set_group_name": {"mode": "value", "value": "approval_required"},
    }, expected_revision=0)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    assert not adapter.allows_group("456")
    assert adapter.allows_management_target("456")
    adapter.admin.set_group_name = AsyncMock()
    params: dict[str, object] = {"name": "新群名"}
    pending = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-1", "set_group_name", params, actor_kind="panel",
    )
    assert pending["state"] == "pending"
    adapter.admin.set_group_name.assert_not_awaited()
    approved = await backend.decide_group_action("bot", "100", "456", "action-runtime-1", approve=True)
    assert approved["state"] == "succeeded"
    adapter.admin.set_group_name.assert_awaited_once_with("456", name="新群名")
    duplicate = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-1", "set_group_name", params, actor_kind="panel",
    )
    assert duplicate == approved
    adapter.admin.set_group_name.assert_awaited_once()
    with pytest.raises(GroupConfigConflict):
        await backend.decide_group_action("bot", "100", "456", "action-runtime-1", approve=True)
    directory.patch_group("100", "456", "approval", {
        "set_group_name": {"mode": "value", "value": "auto_execute"},
    }, expected_revision=1)
    executed = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-2", "set_group_name", {"name": "自动群名"}, actor_kind="panel",
    )
    assert executed["state"] == "succeeded"
    assert adapter.admin.set_group_name.await_count == 2
    adapter.admin.set_group_name = AsyncMock(side_effect=AdminActionUnconfirmed("网络超时"))
    unknown = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-3", "set_group_name", {"name": "未知群名"}, actor_kind="panel",
    )
    assert unknown["state"] == "unknown"
    repeated = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-3", "set_group_name", {"name": "未知群名"}, actor_kind="panel",
    )
    assert repeated == unknown
    adapter.admin.set_group_name.assert_awaited_once()
    adapter._running = True
    adapter._capability_states["set_group_name"] = (adapter.connection_generation(), "unsupported")
    metadata = await backend.group_action_types("bot", "100", "456")
    name_action = next(item for item in metadata["items"] if item["action_type"] == "set_group_name")
    assert name_action["capability"] == "unsupported" and name_action["available"] is False
    adapter.send_management_message = AsyncMock(return_value=SendReceipt("unknown", reason="network_timeout"))
    manual = await backend.send_group_message("bot", "100", "456", "action-send-1", "直接发送")
    assert manual["state"] == "unknown"
    repeated_send = await backend.send_group_message("bot", "100", "456", "action-send-1", "直接发送")
    assert repeated_send == manual
    adapter.send_management_message.assert_awaited_once_with("456", "直接发送")
    with pytest.raises(GroupConfigConflict):
        await backend.send_group_message("bot", "100", "456", "action-send-1", "另一条消息")
