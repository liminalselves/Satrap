"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from dataclasses import replace
import asyncio
import json
import sqlite3
import pytest
from satrap.core.call_context import bind_call_origin
from satrap.core.platform import set_current_adapter_manager
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger, RequestFlagRegistry
from satrap.expend.plugins.group_admin.tools import get_tools
from satrap.expend.plugins.friend_manager.tools import get_tools as friend_tools
from satrap.expend.plugins.friend_manager import tools as friend_tools_module
from satrap.core.platform import current_adapter_manager
from satrap.core.framework.Base import Session
from typing import Any, cast
from support.administration import _setup_adapter, _origin
from satrap.core.config.group_action_origin import bind_group_action_preflight, current_group_action_preflight
from satrap.core.platform.onebot.admin import AdminActionRejected, AdminActionUnconfirmed, UnsupportedAdminAction
from support.onebot import _adapter
from support.requests import attach_friend_host, incoming, restore_manager, tool
from unittest.mock import Mock
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.pipeline.attachments import AttachmentResult
from satrap.core.pipeline.manual_wake_store import ManualWakeStore
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.platform.event import MessageEvent, PlatformMetadata, PlatformMessage
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.request_diagnostics import REQUEST_CAPACITY, RequestDiagnostic, RequestDiagnosticLog
from satrap.core.type import MessageMember, PlatformMessageType
from satrap.core.framework.providers import BindingState, BindingStatus

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_inbound_query_and_friend_decision_without_raw_flag_and_no_replay(tmp_path: Path, monkeypatch, caplog):
    adapter = _setup_adapter(notice_types=[])
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "request_ledger.json"))
    adapter._bot.set_friend_add_request.return_value = {}
    monkeypatch.setattr(adapter, "_emit_notice", AsyncMock())
    adapter.bot_self_id = "10000"
    await adapter._handle_request({"self_id": "10000", "request_type": "friend", "flag": "private-flag", "user_id": "321", "comment": "验证信息"})
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        result = await tool("friend_manager_list_requests", write=False).execute()
        entry = result["data"]["items"][0]
        assert entry["comment"] == "验证信息" and entry["user_id"] == "321"
        assert "private-flag" not in json.dumps(result) and "flag" not in entry
        assert (await tool("friend_manager_handle_request", write=False).execute(request_id=entry["request_id"], approve=True))["ok"] is False
        handle = tool("friend_manager_handle_request")
        accepted = await handle.execute(request_id=entry["request_id"], approve=True, remark="测试")
        assert accepted["ok"] and accepted["data"]["state"] == "succeeded"
        repeated = await handle.execute(request_id=entry["request_id"], approve=False)
        assert repeated["ok"] and repeated["data"]["state"] == "failed"
        assert (await tool("friend_manager_list_requests").execute())["data"]["items"] == []
    adapter._bot.set_friend_add_request.assert_awaited_once_with(flag="private-flag", approve=True, remark="测试")
    assert "private-flag" not in caplog.text
    assert "private-flag" not in (tmp_path / "request_ledger.json").read_text(encoding="utf-8")

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_request_tools_support_groups_self_service_and_reject_wrong_account():
    adapter = _setup_adapter()
    await incoming(adapter)
    adapter.client_self_id = adapter.bot_self_id
    query = tool("friend_manager_list_requests", write=False)
    for origin in [_origin(), _origin(chat_type="FriendMessage", actor="999")]:
        with bind_call_origin(origin):
            assert query.is_available_for_call()
            assert (await query.execute())["ok"]
    for origin in [replace(_origin(chat_type="FriendMessage"), self_id="other")]:
        with bind_call_origin(origin):
            assert not query.is_available_for_call()
            assert not (await query.execute())["ok"]
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        disabled = tool("friend_manager_list_requests", request_managers="")
        assert disabled.is_available_for_call()
        own = await disabled.execute()
        assert own["ok"] and own["data"]["scope"] == "self" and own["data"]["items"] == []
        assert query.is_available_for_call()
        assert not (await query.execute(self_id="other"))["ok"]
        assert not (await query.execute(limit=True))["ok"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_entry_permission_is_rechecked_after_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """等待期间全量查询权限被撤销时拒绝返回已获得的数据"""
    adapter = _setup_adapter()
    await incoming(adapter)
    stages: list[str] = []
    original = adapter.friend_requests

    async def listing(account: str, limit: int, cursor: str | None) -> Any:
        stages.append("host")
        return await original(account, limit, cursor)

    original_check = friend_tools_module.require_plugin_entry_permission
    def revoked(binding: Any, **kwargs: Any) -> Any:
        stages.append("check")
        if "host" in stages:
            raise PermissionError("权限已撤销")
        return original_check(binding, **kwargs)

    query = tool("friend_manager_list_requests", write=False)
    monkeypatch.setattr(adapter, "friend_requests", listing)
    monkeypatch.setattr(friend_tools_module, "require_plugin_entry_permission", revoked)
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        result = await query.execute()
    assert stages == ["check", "host", "check"]
    assert result == {"ok": False, "error": {"code": "permission_denied", "message": "当前操作未获授权或权限已变化", "retryable": False}}

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_pending_request_survives_restart_and_completed_unknown_never_reappear(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: 1000.0)
    path = tmp_path / "ledger.json"
    first = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    await first.register("friend", "stable", self_id="10000", user_id="321", comment="首次验证")
    original = (await first.list_requests("friend", self_id="10000"))["items"][0]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: 1100.0)
    second = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    await second.register("friend", "stable", self_id="10000", user_id="321", comment="重复验证")
    restored = (await second.list_requests("friend", self_id="10000"))["items"][0]
    assert {key: value for key, value in restored.items() if key != "remaining_seconds"} == {key: value for key, value in original.items() if key != "remaining_seconds"}
    assert restored["expires_at"] == 1600.0 and restored["remaining_seconds"] == 500
    assert (await second.resolve_request("friend", original["request_id"], self_id="10000"))["flag"] == "stable"
    await second.occupy("friend", "stable", self_id="10000")
    third = RequestFlagRegistry("ob", RequestApprovalLedger(path))
    assert (await third.list_requests("friend", self_id="10000"))["items"] == []
    with pytest.raises(LookupError):
        await third.resolve_request("friend", original["request_id"], self_id="10000")
    await third.register("friend", "stable", self_id="10000", user_id="321")
    assert (await third.list_requests("friend", self_id="10000"))["items"] == []

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_expiration_missing_legacy_payload_and_corrupt_inbox_fail_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: 1000.0)
    registry = RequestFlagRegistry("ob", RequestApprovalLedger(tmp_path / "ledger.json"))
    await registry.ledger.register("ob", "10000", "friend", "legacy", user_id="7")
    missing = await registry.list_requests("friend", self_id="10000")
    assert missing["items"] == [] and missing["unavailable_count"] == 1
    await registry.register("friend", "fresh", self_id="10000", user_id="8")
    request_id = (await registry.list_requests("friend", self_id="10000"))["items"][0]["request_id"]
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: 1600.0)
    assert (await registry.list_requests("friend", self_id="10000"))["items"] == []
    with pytest.raises(LookupError):
        await registry.resolve_request("friend", request_id, self_id="10000")
    assert registry.inbox.path is not None
    with sqlite3.connect(registry.inbox.path) as db:
        row = db.execute("SELECT flag,archived_at FROM requests").fetchone()
        assert row[0] == "fresh" and row[1] is not None
    registry.inbox.path.write_bytes(b"broken")
    with pytest.raises(sqlite3.DatabaseError):
        await registry.list_requests("friend", self_id="10000")

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_request_pagination_separates_platform_account_kind_and_group(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("satrap.core.platform.onebot.request_registry.time", lambda: 1000.0)
    ledger = RequestApprovalLedger(tmp_path / "ledger.json")
    registry = RequestFlagRegistry("ob", ledger)
    other = RequestFlagRegistry("other", ledger)
    for index in range(3):
        await registry.register("group", f"g{index}", self_id="10000", group_id="456", sub_type="add", user_id="7")
    await registry.register("group", "different-group", self_id="10000", group_id="789", sub_type="invite", user_id="7")
    await registry.register("friend", "g0", self_id="10000", user_id="7")
    await registry.register("group", "g0", self_id="20000", group_id="456", sub_type="add", user_id="7")
    await other.register("group", "g0", self_id="10000", group_id="456", sub_type="add", user_id="7")
    changed_account = await registry.list_requests("group", self_id="20000", group_id="456")
    assert len(changed_account["items"]) == 1 and changed_account["items"][0]["user_id"] == "7"
    first = await registry.list_requests("group", self_id="10000", group_id="456", limit=2)
    assert first["total"] == 3 and first["has_more"]
    next_page = await registry.list_requests("group", self_id="10000", group_id="456", cursor=first["next_cursor"])
    assert len(next_page["items"]) == 1 and not next_page["has_more"]
    with pytest.raises(ValueError):
        await registry.list_requests("group", self_id="10000", group_id="789", cursor=first["next_cursor"])
    for target, kind, account in [(other, "group", "10000"), (registry, "friend", "10000"), (registry, "group", "20000")]:
        with pytest.raises(LookupError):
            await target.resolve_request(kind, first["items"][0]["request_id"], self_id=account)

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_group_request_uses_original_target_and_existing_approval_handler():
    adapter = _setup_adapter()
    await incoming(adapter, "group", group_id="456", sub_type="add")
    adapter.group_action_handler = AsyncMock(return_value={"state": "pending", "action_id": "approval"})
    with bind_call_origin(_origin()):
        queried = await tool("group_admin_list_group_requests", write=False).execute()
        request_id = queried["data"]["items"][0]["request_id"]
        handle = tool("group_admin_handle_group_request")
        bad = await handle.execute(request_id=request_id, approve=True, group_id="789")
        assert bad["ok"] is False and bad["error_type"] == "permission_denied"
        prepared = await handle.execute(request_id=request_id, approve=True)
        assert prepared["data"]["state"] == "pending"
    adapter.group_action_handler.assert_awaited_once_with("456", "handle_group_request", {
        "flag": "private-flag", "sub_type": "add", "approve": True,
        "request_id": request_id, "expected_revision": queried["data"]["items"][0]["revision"],
    })
    adapter._bot.set_group_add_request.assert_not_called()
    with bind_call_origin(_origin(chat_id="789")):
        assert (await handle.execute(request_id=request_id, approve=True))["ok"] is False

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_sync_query_executes_on_platform_loop_and_registration_failure_is_isolated(tmp_path: Path, monkeypatch, caplog):
    adapter = _setup_adapter()
    adapter._loop = asyncio.get_running_loop()
    await incoming(adapter)
    attach_friend_host(adapter)
    config = {"managers": "123"}
    query = next(t for t in friend_tools(cast(Session, object()), config) if t.tool_name == "friend_manager_list_requests")
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        result = await asyncio.to_thread(query.execute)
    assert result["ok"] and result["data"]["items"][0]["user_id"] == "321"
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / "ledger.json"))
    path = adapter.request_flags.inbox.path
    assert path is not None
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken")
    monkeypatch.setattr(adapter, "_emit_notice", AsyncMock())
    await adapter._handle_request({"self_id": "10000", "request_type": "friend", "flag": "never-log-secret", "user_id": "321"})
    adapter._emit_notice.assert_awaited_once()
    assert "request 登记失败" in caplog.text and "Traceback" in caplog.text and "never-log-secret" not in caplog.text
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        failed = await tool("friend_manager_list_requests").execute()
    assert not failed["ok"] and "请查看后端日志" in failed["error"]["message"]

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["manager", "group", "platform"])
async def test_query_rechecks_access_after_waiting(revocation, monkeypatch):
    adapter = _setup_adapter()
    await incoming(adapter, kind="group", group_id="456", sub_type="add")
    query = tool("group_admin_list_group_requests", write=False, allowed_groups="456")
    original = adapter.request_flags.list_requests

    async def revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        if revocation == "manager":
            query.config["request_managers"] = ""
        elif revocation == "group":
            query.config["allowed_groups"] = "789"
        else:
            monkeypatch.setattr(adapter, "allows_group", lambda _: False)
        return result

    monkeypatch.setattr(adapter.request_flags, "list_requests", revoke)
    with bind_call_origin(_origin()):
        result = await query.execute()
    assert result["ok"] is False and "data" not in result

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_query_does_not_discard_a_request_registered_during_read(monkeypatch):
    import time

    registry = RequestFlagRegistry("ob")
    original = registry.inbox.rows

    def insert_then_read(adapter_id, self_id, kind, now):
        assert registry.ledger._register_sync(adapter_id, self_id, kind, "new-flag", group_id="", sub_type="",
                                              user_id="321", now=now) == "registered"
        registry.inbox.register(adapter_id, self_id, kind, "new-flag", group_id="", sub_type="", user_id="321",
                                comment="查询时到达", received_at=now, now=now)
        return original(adapter_id, self_id, kind, now)

    monkeypatch.setattr(registry.inbox, "rows", insert_then_read)
    result = await registry.list_requests("friend", self_id="10000")
    assert len(result["items"]) == 1 and result["unavailable_count"] == 0
    assert registry.inbox.resolve("ob", "10000", "friend", result["items"][0]["request_id"], time.time())["flag"] == "new-flag"

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_agent_queries_request_id_and_handles_request_in_private(tmp_path: Path, asynchronous):
    from support.group_reply import setup
    from support.group_models import Script, Model, AsyncModel, Invoker
    from satrap.core.type import LLMCallResponse
    from satrap.core.pipeline.scheduler import PipelineScheduler
    from satrap.edictum import SimpleSession, AsyncSimpleSession
    from satrap.edictum.plugin_compatibility import PluginEnvironment

    adapter, event, _ = setup(tmp_path, private=True)
    adapter._loop = asyncio.get_running_loop()
    await incoming(adapter)
    adapter._bot.set_friend_add_request.return_value = {}
    def decide(messages, kwargs):
        data = json.loads(messages[-1]["content"])
        entry = data["data"]["items"][0]
        definitions = {definition["function"]["name"]: definition["function"] for definition in kwargs["tools"]}
        assert "request_id" in definitions["friend_manager_handle_request"]["parameters"]["properties"]
        assert "flag" not in definitions["friend_manager_handle_request"]["parameters"]["properties"]
        return LLMCallResponse("tools_call", "", tool_calls=[{"id": "decide", "name": "friend_manager_handle_request", "arguments": {"request_id": entry["request_id"], "approve": True}}])
    script = Script([LLMCallResponse("tools_call", "", tool_calls=[{"id": "query", "name": "friend_manager_list_requests", "arguments": {}}]),
                     decide, LLMCallResponse("message", "申请已处理")])
    model = (AsyncModel if asynchronous else Model)(script)
    session = (AsyncSimpleSession if asynchronous else SimpleSession)("request-agent", cast(Any, model), enable_checkpoint=False,
                                                                   db_path=str(tmp_path / "ctx.db"), plugin_environment=PluginEnvironment("platform", "onebot"))
    attach_friend_host(adapter)
    plugin = Path(__file__).resolve().parents[2] / "satrap/expend/plugins/friend_manager"
    config = {"managers": "123", "write_callers": "123", "request_handling_enabled": True}
    if asynchronous:
        await session.initialize()
        await session.install_plugin(str(plugin), config=config)
    else:
        session.install_plugin(str(plugin), config=config)
    await PipelineScheduler(Invoker(session, asynchronous)).execute(event)
    assert len(script.requests) == 3
    assert "private-flag" not in json.dumps(script.requests, ensure_ascii=False)
    adapter._bot.set_friend_add_request.assert_awaited_once_with(flag="private-flag", approve=True, remark="")

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.asyncio
async def test_group_request_deadline_and_revoked_request_manager_are_rechecked(tmp_path: Path):
    import time
    from satrap.core.backend.BackendManager import BackendManager, BackendConfig
    from satrap.core.platform import PlatformAdapterManager
    from satrap.core.config.group_directory import GroupDirectoryStore
    from satrap.expend.plugins.group_admin.tools import _build_tools, AsyncGroupAdminTool

    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{"id": "ob", "type": "onebot", "settings": settings}]))
    adapter = _setup_adapter(**settings)
    adapter.bot_self_id = "10000"
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["ob"] = adapter
    set_current_adapter_manager(backend._adapter_mgr)
    backend._attach_request_ledger(adapter)
    directory = GroupDirectoryStore(backend.platform_db_path("ob"))
    directory.adopt_legacy("10000", settings)
    directory.patch_account("10000", expected_revision=1, mode="all", approval_defaults={"handle_group_request": "approval_required"})
    directory.confirm_membership("10000", "456", True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("10000")
    await adapter.request_flags.register("group", "deadline-flag", self_id="10000", group_id="456", sub_type="add", user_id="321", now=time.time() - 500)
    request = (await adapter.request_flags.list_requests("group", self_id="10000", group_id="456"))["items"][0]
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action("ob", "10000", gid, "request-pending-1", action, params, actor_kind="model")
    config = {"request_managers": "123", "allowed_callers": "123", "write_tools_enabled": True}
    handle = next(t for t in _build_tools(AsyncGroupAdminTool, config) if t.tool_name == "group_admin_handle_group_request")
    with bind_call_origin(_origin()):
        submitted = await handle.execute(request_id=request["request_id"], approve=True)
    assert submitted["data"]["state"] == "pending" and submitted["data"]["expires_at"] <= request["expires_at"]
    config["request_managers"] = ""
    result = await backend.decide_group_action("ob", "10000", "456", "request-pending-1", approve=True)
    assert result["state"] == "failed" and result["result"]["reason"] == "model_permission_revoked"
    adapter._bot.set_group_add_request.assert_not_awaited()

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.parametrize('kind,sub_type,suspicious', [('friend', '', False), ('friend', '', True), ('group', 'add', False), ('group', 'invite', False)])
@pytest.mark.parametrize('failure', ['permission', 'permission_async', 'unsupported', 'disconnected'])
async def test_request_not_sent_keeps_credential_and_can_be_processed_once_after_fix(tmp_path, monkeypatch, kind, sub_type, suspicious, failure):
    adapter = _adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / 'ledger.json'))
    registry = adapter.request_flags
    await registry.register(kind, 'request-flag', self_id=adapter.bot_self_id, user_id='321',
        group_id='456' if kind == 'group' else '', sub_type=sub_type, event_time=100,
        details={'request_category': 'suspicious'} if suspicious else None)
    initial = (await registry.list_requests(kind, self_id=adapter.bot_self_id))['items'][0]
    row = await registry.resolve_request(kind, initial['request_id'], self_id=adapter.bot_self_id)
    bot = adapter._bot
    assert bot is not None
    action = 'set_group_add_request' if kind == 'group' else 'set_doubt_friends_add_request' if suspicious else 'set_friend_add_request'
    rpc = AsyncMock()
    monkeypatch.setattr(bot, action, rpc)

    async def invoke():
        if kind == 'group':
            await adapter.admin.handle_group_request('456', 'request-flag', sub_type, True,
                request_id=initial['request_id'], expected_revision=initial['revision'])
        else:
            await adapter.admin.handle_friend_request('request-flag', True, identity_digest=row['digest'], suspicious=suspicious)

    def denied():
        raise PermissionError('授权已撤销')

    async def denied_async():
        await asyncio.sleep(0)
        raise PermissionError('授权已撤销')

    if failure in {'permission', 'permission_async'}:
        guard = denied if failure == 'permission' else denied_async
        with bind_group_action_preflight(guard):
            with pytest.raises(PermissionError):
                await invoke()
            assert current_group_action_preflight() is guard
    elif failure == 'unsupported':
        monkeypatch.setattr(bot, action, None)
        with pytest.raises(UnsupportedAdminAction):
            await invoke()
        monkeypatch.setattr(bot, action, rpc)
    else:
        monkeypatch.setattr(adapter, '_bot', None)
        with pytest.raises(AdminActionUnconfirmed):
            await invoke()
        monkeypatch.setattr(adapter, '_bot', bot)
    rpc.assert_not_awaited()
    entry = registry.ledger.lookup(adapter.config.id, adapter.bot_self_id, kind, 'request-flag', identity_digest=row['digest'])
    assert entry is not None and entry['state'] == 'available'
    preserved = await registry.resolve_request(kind, initial['request_id'], self_id=adapter.bot_self_id)
    assert preserved['flag'] == 'request-flag' and preserved['execution_state'] == 'not_started'
    assert preserved['revision'] == initial['revision']
    await invoke()
    rpc.assert_awaited_once()
    completed = registry.ledger.lookup(adapter.config.id, adapter.bot_self_id, kind, 'request-flag', identity_digest=row['digest'])
    assert completed is not None and completed['state'] == 'completed'
    with pytest.raises((AdminActionRejected, ValueError)):
        await invoke()
    with pytest.raises(LookupError):
        await registry.occupy(kind, 'request-flag', self_id=adapter.bot_self_id,
            group_id='456' if kind == 'group' else '', sub_type=sub_type, identity_digest=row['digest'])
    assert rpc.await_count == 1

@pytest.mark.usefixtures('restore_manager')
@pytest.mark.parametrize('kind', ['friend', 'group'])
async def test_request_timeout_after_dispatch_cannot_be_released_or_replayed(tmp_path, monkeypatch, kind):
    adapter = _adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / 'ledger.json'))
    registry = adapter.request_flags
    await registry.register(kind, 'flag', self_id=adapter.bot_self_id, user_id='321',
        group_id='456' if kind == 'group' else '', sub_type='invite' if kind == 'group' else '')
    assert adapter._bot is not None
    action = 'set_group_add_request' if kind == 'group' else 'set_friend_add_request'
    rpc = AsyncMock(side_effect=asyncio.TimeoutError())
    monkeypatch.setattr(adapter._bot, action, rpc)
    with pytest.raises(AdminActionUnconfirmed):
        if kind == 'group':
            await adapter.admin.handle_group_request('456', 'flag', 'invite', True)
        else:
            await adapter.admin.handle_friend_request('flag', True)
    entry = registry.ledger.lookup(adapter.config.id, adapter.bot_self_id, kind, 'flag')
    assert entry is not None and entry['state'] == 'unknown'
    assert await registry.ledger.release(adapter.config.id, adapter.bot_self_id, kind, 'flag') is None
    with pytest.raises(LookupError):
        await registry.occupy(kind, 'flag', self_id=adapter.bot_self_id,
            group_id='456' if kind == 'group' else '', sub_type='invite' if kind == 'group' else '')
    rpc.assert_awaited_once()

@pytest.mark.usefixtures('restore_manager')
async def test_release_does_not_revive_superseded_request_or_modify_new_occurrence(tmp_path, monkeypatch):
    monkeypatch.setattr('satrap.core.platform.onebot.request_registry.time', lambda: 1000.0)
    ledger = RequestApprovalLedger(tmp_path / 'ledger.json')
    registry = RequestFlagRegistry('bot', ledger)
    await registry.register('friend', 'reused', self_id='100', user_id='321', event_time=100)
    first = (await registry.list_requests('friend', self_id='100'))['items'][0]
    old = await registry.resolve_request('friend', first['request_id'], self_id='100')
    await registry.occupy('friend', 'reused', self_id='100', identity_digest=old['digest'])
    await registry.register('friend', 'reused', self_id='100', user_id='321', event_time=101)
    current = (await registry.list_requests('friend', self_id='100'))['items'][0]
    latest = await registry.resolve_request('friend', current['request_id'], self_id='100')
    await registry.release('friend', 'reused', self_id='100', identity_digest=old['digest'])
    released = ledger.lookup('bot', '100', 'friend', 'reused', identity_digest=old['digest'])
    assert released is not None and released['state'] == 'superseded'
    new = ledger.lookup('bot', '100', 'friend', 'reused', identity_digest=latest['digest'])
    assert new is not None and new['state'] == 'available'

@pytest.mark.usefixtures('restore_manager')
async def test_release_failure_keeps_occupation_and_never_clears_credential(tmp_path, monkeypatch):
    ledger = RequestApprovalLedger(tmp_path / 'ledger.json')
    await ledger.register('bot', '100', 'friend', 'flag', user_id='321')
    await ledger.occupy('bot', '100', 'friend', 'flag')

    def failed_save():
        raise OSError('不能写入')

    monkeypatch.setattr(ledger, '_save_locked', failed_save)
    with pytest.raises(OSError):
        await ledger.release('bot', '100', 'friend', 'flag')
    entry = ledger.lookup('bot', '100', 'friend', 'flag')
    assert entry is not None and entry['state'] == 'executing'
    with pytest.raises(LookupError):
        await ledger.occupy('bot', '100', 'friend', 'flag')

@pytest.mark.usefixtures('restore_manager')
async def test_release_of_expired_occupation_requires_archived_confirmation(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('satrap.core.platform.onebot.request_registry.time', lambda: clock[0])
    ledger = RequestApprovalLedger(tmp_path / 'ledger.json')
    await ledger.register('bot', '100', 'friend', 'flag', user_id='321')
    await ledger.occupy('bot', '100', 'friend', 'flag')
    clock[0] = 1700.0
    released = await ledger.release('bot', '100', 'friend', 'flag')
    assert released is not None and released['state'] == 'expired'
    with pytest.raises(LookupError):
        await ledger.occupy('bot', '100', 'friend', 'flag')
    assert (await ledger.occupy('bot', '100', 'friend', 'flag', allow_archived=True))['state'] == 'executing'

@pytest.mark.usefixtures('restore_manager')
async def test_cancellation_after_protocol_call_started_remains_unknown(tmp_path, monkeypatch):
    adapter = _adapter()
    adapter.set_request_ledger(RequestApprovalLedger(tmp_path / 'ledger.json'))
    await adapter.request_flags.register('friend', 'flag', self_id=adapter.bot_self_id, user_id='321')
    started = asyncio.Event()

    async def protocol(**_):
        started.set()
        await asyncio.Event().wait()

    assert adapter._bot is not None
    monkeypatch.setattr(adapter._bot, 'set_friend_add_request', protocol)
    pending = asyncio.create_task(adapter.admin.handle_friend_request('flag', True))
    await asyncio.wait_for(started.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    entry = adapter.request_flags.ledger.lookup(adapter.config.id, adapter.bot_self_id, 'friend', 'flag')
    assert entry is not None and entry['state'] == 'unknown'

_NOW = 1_760_000_000.0

class _RunnableRegistry:
    """绑定判定恒为可运行的会话定义注册表替身"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """恒定答复可运行"""
        return BindingStatus(BindingState.RUNNABLE)

class _StubAdapter(OneBotAdapter):
    """不发网络请求的 OneBot 适配器替身"""

    def __init__(self, adapter_id: str = "ob") -> None:
        super().__init__(PlatformConfig(id=adapter_id, type="onebot", settings={"self_id": "10"}))
        self.started = True
        self._bot = AsyncMock()

    async def _dispatch_action(self, session_id: str, private_action: str, group_action: str, **params: Any) -> Any:
        return {"message_id": 1}

def _event(adapter: OneBotAdapter, text: str = "hello", session_id: str = "group%20") -> MessageEvent:
    message = PlatformMessage()
    message.type = PlatformMessageType.GROUP_MESSAGE
    message.self_id = "10"
    message.session_id = session_id
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="30", nickname="User")
    message.message = []
    message.message_str = text
    event = MessageEvent(
        message_str=text, platform_message=message,
        platform_meta=PlatformMetadata(name=adapter.config.id, id=adapter.config.id),
        session_id=session_id, adapter=adapter, session_type="group",
    )
    event.is_wake = True
    event.call_llm = True
    return event

def _scheduler(
    tmp_path: Path, *, store: ManualWakeStore | None = None, response: str = "", adapter: OneBotAdapter | None = None,
) -> tuple[PipelineScheduler, Any, OneBotAdapter]:
    manager = Mock()
    manager.handle_call_async = AsyncMock(return_value=response)
    manager.provider_registry = _RunnableRegistry()
    scheduler = PipelineScheduler(cast(Any, manager))
    instance = adapter if adapter is not None else _StubAdapter()
    scheduler.manual_wake_store = store if store is not None else ManualWakeStore(tmp_path / "wake.json")
    instance.set_send_attempt_recorder(scheduler.manual_wake_store)
    return scheduler, manager, instance

def _rows(detail: dict[str, object]) -> list[dict[str, Any]]:
    """诊断明细中的阶段记录"""
    return cast(list[dict[str, Any]], detail["records"])

def _stages(detail: dict[str, object]) -> dict[str, dict[str, Any]]:
    """阶段名到阶段记录的映射"""
    return {item["stage"]: item for item in _rows(detail)}

class TestDiagnosticCollection:
    @pytest.mark.asyncio
    async def test_unwoken_message_is_queryable_before_projection(self, tmp_path: Path):
        """反例: 未执行投影的拒绝也必须能查到阶段与原因"""
        scheduler, manager, adapter = _scheduler(tmp_path)
        adapter.started = True
        await adapter._handle_group_message({
            "self_id": 10, "group_id": 20, "user_id": 30, "message_id": 9, "message_type": "group",
            "message": [{"type": "text", "data": {"text": "没有唤醒词"}}],
        })
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        assert manager.handle_call_async.await_count == 0
        assert event.get_extra("input_projection") is None
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        assert [item["stage"] for item in _rows(detail)] == ["wake_decision"]
        record = _rows(detail)[0]
        assert record["status"] == "dropped" and record["reason_code"]
        assert record["message_id"] == "9"

    @pytest.mark.asyncio
    async def test_rate_limit_stage_records_waited_reason(self, tmp_path: Path):
        from satrap.core.pipeline.rate_limiter import RateLimiter

        scheduler, _, adapter = _scheduler(tmp_path)
        limiter = RateLimiter(rate=0.0, burst=1)
        scheduler.rate_limiter = limiter
        event = _event(adapter)
        scheduler.request_diagnostics.record(RequestDiagnostic(
            adapter_id="ob", session_id=event.session_id, actor_id="30", stage="wake_decision",
            decision="dropped", reason="not_woken", recorded_at=_NOW, request_id=event.call_origin.request_id,
        ))
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        # 限流拒绝与该事件其余阶段共用同一 request_id
        assert stages["wake_decision"]["status"] == "dropped"

    @pytest.mark.asyncio
    async def test_attachment_failure_is_a_stage_not_the_final_result(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """反例: 语音转写失败与最终发送结果分别展示, 不压成单一 failed"""
        scheduler, _, adapter = _scheduler(tmp_path, response="回复")
        monkeypatch.setattr(
            "satrap.core.pipeline.scheduler.resolve_attachments",
            AsyncMock(return_value=(
                AttachmentResult("record", "v.amr", "unsupported", "", "silk_needs_platform_transcode"),
                AttachmentResult("file", "a.pdf", "failed", "", "download_failed"),
            )),
        )
        event = _event(adapter)
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        assert stages["projection"]["status"] == "failed"
        assert "record:unsupported:silk_needs_platform_transcode" in stages["projection"]["attachments"]
        assert "file:failed:download_failed" in stages["projection"]["attachments"]
        # 发送阶段独立成条: 模型仍回复成功, 不因附件失败被标记为失败
        assert stages["send"]["status"] == "sent"
        assert stages["model"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_send_stage_separates_partial_from_unknown(self, tmp_path: Path):
        scheduler, _, adapter = _scheduler(tmp_path, adapter=_StubAdapter())
        store = scheduler.manual_wake_store
        assert store is not None
        event = _event(adapter)
        assert store.record_send_attempt("t1", "ob", event.session_id, event.call_origin.request_id, [{"index": 0}])
        assert store.mark_segment_submitted("t1", 0)
        assert store.record_segment_result("t1", 0, "sent", advance_to=1) is False
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        # 只有已提交未确认的段: 发送阶段为 unknown 且带关联 turn_id
        assert stages["send"]["status"] == "unknown" and stages["send"]["turn_id"] == "t1"
        assert "in_flight" in stages["send"]["reason_code"] or "not_submitted" in stages["send"]["reason_code"]

    @pytest.mark.asyncio
    async def test_send_stage_reports_confirmed_prefix_as_partial(self, tmp_path: Path):
        """已确认前缀加未尝试后缀属于 partial, 不因缺少回执被压成失败或已送达"""
        scheduler, _, adapter = _scheduler(tmp_path, adapter=_StubAdapter())
        store = scheduler.manual_wake_store
        assert store is not None
        event = _event(adapter)
        assert store.record_send_attempt(
            "t1", "ob", event.session_id, event.call_origin.request_id,
            [{"index": 0, "kind": "chunk"}, {"index": 1, "kind": "chunk"}],
        )
        assert store.mark_segment_submitted("t1", 0)
        # 第 0 段确认送达, 第 1 段仍是 planned: 前缀已确认且后缀未尝试
        assert store.record_segment_result("t1", 0, "sent") is True
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        assert stages["send"]["status"] == "partial"
        assert stages["send"]["reason_code"] == "confirmed_prefix"

    @pytest.mark.asyncio
    async def test_model_timeout_stage_is_recorded(self, tmp_path: Path):
        scheduler, _, adapter = _scheduler(tmp_path)
        scheduler.llm_timeout = 0.01
        scheduler.error_feedback = False

        async def slow(*args: Any, **kwargs: Any) -> str:
            await asyncio.sleep(0.5)
            return ""

        cast(Any, scheduler.session_manager).handle_call_async = slow
        event = _event(adapter)
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        assert stages["model"]["status"] == "unknown" and stages["model"]["reason_code"] == "llm_timeout"

class TestDiagnosticBounds:
    def test_capacity_keeps_newest_requests_and_drops_oldest(self):
        log = RequestDiagnosticLog(per_adapter=2)
        for index in range(3):
            log.record(RequestDiagnostic(
                adapter_id="ob", session_id="s", actor_id="a", stage="wake_decision", decision="dropped",
                reason="not_woken", recorded_at=_NOW + index, request_id=f"r{index}",
            ))
        assert [item["request_id"] for item in log.list_requests("ob")] == ["r2", "r1"]
        assert log.get_request("r0", "ob") is None
        assert log.stats("ob")["requests_total"] == 2 and log.stats("ob")["capacity"] == 2

    def test_records_per_request_are_bounded_and_deduped(self):
        log = RequestDiagnosticLog()
        for index in range(20):
            log.record(RequestDiagnostic(
                adapter_id="ob", session_id="s", actor_id="a", stage="model", decision="ok",
                reason="x", recorded_at=_NOW + index, request_id="r1", reason_code=f"c{index}",
            ))
        assert log.stats("ob")["records_per_request"] == 16
        detail = log.get_request("r1", "ob")
        assert detail is not None and len(_rows(detail)) == 16 and detail["truncated"] is True
        # 同阶段同原因码重复采集只保留最新一条
        for index in range(5):
            log.record(RequestDiagnostic(
                adapter_id="ob", session_id="s", actor_id="a", stage="send", decision="sent",
                reason="x", recorded_at=_NOW + index, request_id="r2", status="sent", reason_code="all_segments_confirmed",
            ))
        detail = log.get_request("r2", "ob")
        assert detail is not None
        send_rows = [item for item in _rows(detail) if item["stage"] == "send"]
        assert len(send_rows) == 1 and send_rows[0]["reason_code"] == "all_segments_confirmed"

    def test_adapter_filter_is_exact(self):
        log = RequestDiagnosticLog()
        for name in ("ob-a", "ob-b"):
            log.record(RequestDiagnostic(
                adapter_id=name, session_id="s", actor_id="a", stage="wake_decision", decision="dropped",
                reason="not_woken", recorded_at=_NOW, request_id=f"{name}-r",
            ))
        assert [item["adapter_id"] for item in log.list_requests("ob-a")] == ["ob-a"]
        assert len(log.list_requests()) == 2
        assert {item["adapter_id"] for item in log.list("ob-b")} == {"ob-b"}

    def test_stage_filter_selects_requests_with_that_stage(self):
        log = RequestDiagnosticLog()
        log.record(RequestDiagnostic(
            adapter_id="ob", session_id="s", actor_id="a", stage="wake_decision", decision="dropped",
            reason="not_woken", recorded_at=_NOW, request_id="r1",
        ))
        log.record(RequestDiagnostic(
            adapter_id="ob", session_id="s", actor_id="a", stage="send", decision="sent",
            reason="ok", recorded_at=_NOW + 1, request_id="r2", status="sent",
        ))
        assert [item["request_id"] for item in log.list_requests("ob", stages=frozenset({"send"}))] == ["r2"]
        assert [item["request_id"] for item in log.list_requests("ob", stages=frozenset({"wake_decision"}))] == ["r1"]
        # 多值取并集: 命中任一阶段即保留该请求
        assert {item["request_id"] for item in log.list_requests("ob", stages=frozenset({"send", "wake_decision"}))} == {"r1", "r2"}
        assert log.list_requests("ob", request_id="r2")[0]["stages"] == ["send"]

    def test_diagnostics_never_carry_body_text(self, tmp_path: Path):
        from satrap.core.pipeline.request_diagnostics import RECORDS_PER_REQUEST

        assert RECORDS_PER_REQUEST > 0
        log = RequestDiagnosticLog(per_adapter=REQUEST_CAPACITY)
        log.record(RequestDiagnostic(
            adapter_id="ob", session_id="s", actor_id="a", stage="model", decision="ok",
            reason="模型输出 12 字符", recorded_at=_NOW, request_id="r1", reason_code="completed",
        ))
        rendered = str(log.list_requests("ob")) + str(log.get_request("r1", "ob"))
        assert "模型输出 12 字符" in rendered
        # 记录字段白名单之外的内容不会进入查询结果
        for item in cast(list[dict[str, Any]], log.list("ob")):
            assert set(item) == {
                "recorded_at", "adapter_id", "session_id", "actor_id", "stage", "decision", "reason",
                "message_id", "request_id", "send_status", "self_id", "status", "reason_code",
                "turn_id", "attachments", "notes",
            }

class TestDiagnosticRoutes:
    @staticmethod
    def _server(tmp_path: Path) -> tuple[BackendHTTPServer, BackendManager, PipelineScheduler, OneBotAdapter]:
        backend = BackendManager()
        backend._running = True
        manager = Mock()
        manager.handle_call_async = AsyncMock(return_value="")
        manager.provider_registry = _RunnableRegistry()
        scheduler = PipelineScheduler(cast(Any, manager))
        scheduler.manual_wake_store = ManualWakeStore(tmp_path / "wake.json")
        backend._scheduler = scheduler
        backend._manual_wake_store = scheduler.manual_wake_store
        backend._adapter_mgr = PlatformAdapterManager()
        adapter = _StubAdapter()
        backend._adapter_mgr._adapters["ob"] = adapter
        return BackendHTTPServer(backend, port=0), backend, scheduler, adapter

    @pytest.mark.asyncio
    async def test_list_and_detail_routes(self, tmp_path: Path):
        server, _, scheduler, adapter = self._server(tmp_path)
        scheduler.request_diagnostics.record(RequestDiagnostic(
            adapter_id="ob", session_id="group%20", actor_id="30", stage="wake_decision", decision="dropped",
            reason="not_woken: 未命中唤醒条件", recorded_at=_NOW, request_id="r1", reason_code="not_woken",
            message_id="9",
        ))
        status, body = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob", b"")
        assert status == 200 and body["available"] is True
        records = cast(list[dict[str, Any]], body["records"])
        assert records[0]["request_id"] == "r1" and records[0]["stages"] == ["wake_decision"]
        # 摘要只给脱敏原因码, 展示用原因在明细里
        assert records[0]["reason_codes"] == ["not_woken"]
        detail = await server._route("GET", "/api/platforms/wake/diagnostics/r1", b"")
        assert detail[0] == 200 and len(cast(list[Any], detail[1]["records"])) == 1
        assert "未命中" in cast(list[dict[str, Any]], detail[1]["records"])[0]["reason"]
        missing = await server._route("GET", "/api/platforms/wake/diagnostics/nope", b"")
        assert missing[0] == 404 and missing[1]["reason"] == "not_found"
        bad_stage = await server._route("GET", "/api/platforms/wake/diagnostics?stage=nope", b"")
        assert bad_stage[0] == 400 and bad_stage[1]["error"] == "invalid_stage"
        bad_limit = await server._route("GET", "/api/platforms/wake/diagnostics?limit=abc", b"")
        assert bad_limit[0] == 400 and bad_limit[1]["error"] == "invalid_limit"
        # 旧的拒绝记录接口保持兼容
        legacy = await server._route("GET", "/api/platforms/wake/rejections?adapter_id=ob", b"")
        assert legacy[0] == 200 and cast(list[dict[str, Any]], legacy[1]["records"])[0]["stage"] == "wake_decision"

    @pytest.mark.asyncio
    async def test_diagnostics_unavailable_is_marked(self, tmp_path: Path):
        backend = BackendManager()
        server = BackendHTTPServer(backend, port=0)
        status, body = await server._route("GET", "/api/platforms/wake/diagnostics", b"")
        assert status == 200 and body["available"] is False and body["reason"] == "scheduler_unavailable"
        detail = await server._route("GET", "/api/platforms/wake/diagnostics/r1", b"")
        assert detail[0] == 503 and detail[1]["reason"] == "scheduler_unavailable"

    @pytest.mark.asyncio
    async def test_recording_failure_does_not_break_pipeline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """反例: 诊断采集异常不得影响消息处理"""
        scheduler, _, adapter = _scheduler(tmp_path, response="回复")

        def broken(record: Any) -> None:
            raise RuntimeError("diagnostics down")

        monkeypatch.setattr(scheduler.request_diagnostics, "record", broken)
        event = _event(adapter)
        await scheduler.execute(event)
        assert event.last_business_receipt is None or event.last_business_receipt.status in {"success", "unknown"}

    @pytest.mark.asyncio
    async def test_manual_request_and_plain_event_share_diagnostics(self, tmp_path: Path):
        """反例: 手动请求与普通事件的诊断共用同一批阶段"""
        from satrap.core.pipeline.manual_wake import ManualWakeTicket

        server, backend, scheduler, adapter = self._server(tmp_path)
        plain = _event(adapter)
        await scheduler.execute(plain)
        # 手动请求的生产路径会把票据的 request_id 写回事件来源, 诊断按同一标识归并
        manual = _event(adapter)
        manual._call_origin = replace(manual.call_origin, request_id="manual-1")
        scheduler.manual_wakes.tickets[manual] = ManualWakeTicket("manual-1")
        cast(ManualWakeStore, backend._manual_wake_store).accept_request("ob", "manual-1", "fp", "group%20", "op")
        await scheduler.execute(manual)
        for request_id in (plain.call_origin.request_id, "manual-1"):
            detail = scheduler.request_diagnostics.get_request(request_id)
            assert detail is not None, request_id
            stages = set(_stages(detail))
            assert {"model", "send"} <= stages
            # 空回复没有发出任何业务内容: 发送阶段不得记成已送达或失败
            send_status = _stages(detail)["send"]["status"]
            assert send_status == "skipped"
        status, body = await server._route("GET", "/api/platforms/wake/diagnostics?stage=send", b"")
        assert status == 200 and len(cast(list[Any], body["records"])) == 2

    @staticmethod
    def _seed_rejections(scheduler: PipelineScheduler) -> None:
        """生产形态的拒绝与执行记录: 有效 request_id, 决策拒绝与限流各一条"""
        seeded = (
            ("req-rejected", "wake_decision", "not_woken"),
            ("req-limited", "rate_limit", "rate_limited"),
            ("req-cooldown", "wake_decision", "cooldown"),
        )
        for index, (request_id, stage, reason_code) in enumerate(seeded):
            scheduler.request_diagnostics.record(RequestDiagnostic(
                adapter_id="ob", session_id="group%20", actor_id="30", stage=stage, decision="dropped",
                reason=f"{reason_code}: 测试", recorded_at=_NOW + index, request_id=request_id, reason_code=reason_code,
            ))
        # 被拒绝后经定时复查执行的请求: 两侧都按"该请求曾被拒绝"命中
        scheduler.request_diagnostics.record(RequestDiagnostic(
            adapter_id="ob", session_id="group%20", actor_id="30", stage="wake_decision", decision="dropped",
            reason="no_pending: 测试", recorded_at=_NOW + 4, request_id="req-resumed", reason_code="no_pending",
        ))
        scheduler.request_diagnostics.record(RequestDiagnostic(
            adapter_id="ob", session_id="group%20", actor_id="30", stage="send", decision="ok",
            reason="业务段 已确认 1 未确认 0 失败 0", recorded_at=_NOW + 5, request_id="req-resumed",
            reason_code="all_segments_confirmed", status="sent",
        ))

    @staticmethod
    def _request_ids(body: dict[str, Any]) -> set[str]:
        """摘要或记录响应里的 request_id 集合"""
        return {str(item["request_id"]) for item in cast(list[dict[str, Any]], body["records"])}

    @pytest.mark.asyncio
    async def test_stage_filter_multi_value_and_validation(self, tmp_path: Path):
        """stage 支持逗号分隔多值: 命中任一阶段即保留请求, 但仍返回该请求全部阶段"""
        server, _, scheduler, _ = self._server(tmp_path)
        self._seed_rejections(scheduler)
        single = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=send", b"")
        assert self._request_ids(single[1]) == {"req-resumed"}
        preset = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=wake_decision,rate_limit", b"")
        assert self._request_ids(preset[1]) == {"req-rejected", "req-limited", "req-cooldown", "req-resumed"}
        # 命中过滤的请求返回全部阶段, 不裁掉执行阶段明细
        limited = [item for item in cast(list[dict[str, Any]], preset[1]["records"]) if item["request_id"] == "req-resumed"]
        assert limited[0]["stages"] == ["wake_decision", "send"]
        # 重复取值去重, 首尾空白容错
        duplicate = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=send,send", b"")
        assert self._request_ids(duplicate[1]) == {"req-resumed"}
        spaced = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=%20send%20,%20model%20", b"")
        assert self._request_ids(spaced[1]) == self._request_ids(single[1])
        # 空参数不过滤: 与缺省一致
        empty = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=", b"")
        absent = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob", b"")
        assert len(self._request_ids(empty[1])) == 4
        assert self._request_ids(empty[1]) == self._request_ids(absent[1])
        # 空项与未知阶段都拒绝
        empty_item = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=send,", b"")
        assert empty_item[0] == 400 and empty_item[1]["error"] == "invalid_stage"
        unknown = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=send,nope", b"")
        assert unknown[0] == 400 and unknown[1]["error"] == "invalid_stage"
        # 与 request_id 按 AND 组合
        both = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=rate_limit&request_id=req-resumed", b"")
        assert both[0] == 200 and self._request_ids(both[1]) == set()

    @pytest.mark.asyncio
    async def test_rejection_preset_equals_legacy_rejection_requests(self, tmp_path: Path):
        """等价性: 仅看拒绝的请求集合与旧拒绝记录接口的 request_id 集合一致"""
        server, _, scheduler, _ = self._server(tmp_path)
        self._seed_rejections(scheduler)
        legacy = await server._route("GET", "/api/platforms/wake/rejections?adapter_id=ob&limit=256", b"")
        preset = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=wake_decision,rate_limit&limit=256", b"")
        assert legacy[0] == 200 and preset[0] == 200
        assert self._request_ids(legacy[1]) == self._request_ids(preset[1])
        assert self._request_ids(preset[1]) == {"req-rejected", "req-limited", "req-cooldown", "req-resumed"}

    @pytest.mark.asyncio
    async def test_rejection_preset_groups_repeated_decisions_per_request(self, tmp_path: Path):
        """定时复查会给同一请求追加第二条决策记录: 记录级与请求级的条数差异是预期行为"""
        server, _, scheduler, _ = self._server(tmp_path)
        for index, reason_code in enumerate(("frequency", "cooldown")):
            scheduler.request_diagnostics.record(RequestDiagnostic(
                adapter_id="ob", session_id="group%20", actor_id="30", stage="wake_decision", decision="dropped",
                reason=f"{reason_code}: 测试", recorded_at=_NOW + index, request_id="req-loop", reason_code=reason_code,
            ))
        legacy = await server._route("GET", "/api/platforms/wake/rejections?adapter_id=ob&limit=256", b"")
        preset = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob&stage=wake_decision,rate_limit", b"")
        # 旧接口按记录返回两条, 新视图按请求归并为一条并保留两个原因码
        assert len(cast(list[Any], legacy[1]["records"])) == 2
        summaries = cast(list[dict[str, Any]], preset[1]["records"])
        assert len(summaries) == 1 and summaries[0]["request_id"] == "req-loop"
        assert summaries[0]["reason_codes"] == ["frequency", "cooldown"]
