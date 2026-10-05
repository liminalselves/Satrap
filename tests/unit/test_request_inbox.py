from pathlib import Path
from unittest.mock import AsyncMock
from dataclasses import replace
import asyncio
import json
import sqlite3

import pytest

from satrap.core.call_context import bind_call_origin
from satrap.core.plugin_authorization import bind_plugin_factory_tools
from satrap.core.platform import set_current_adapter_manager
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger, RequestFlagRegistry
from satrap.expend.plugins.group_admin.tools import get_tools
from satrap.expend.plugins.friend_manager.tools import AsyncFriendTool, get_tools as friend_tools
from satrap.core.friends.service import FriendService
from satrap.core.platform import current_adapter_manager
from satrap.core.framework.Base import Session
from typing import Any, cast
from .test_group_admin_plugin import _setup_adapter, _async_tools, _origin


@pytest.fixture(autouse=True)
def restore_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(FriendService, "_test_path", tmp_path / "friends.db", raising=False)
    yield
    set_current_adapter_manager(None)


def tool(name, *, write=True, **extra):
    if name.startswith("friend_manager_"):
        adapter = current_adapter_manager().get_adapter("ob")
        attach_friend_host(adapter)
        config = {"managers": extra.pop("request_managers", "123"), "write_callers": "123", "request_handling_enabled": write, **extra}
        item = AsyncFriendTool(name, "好友工具", {})
        item.config = config
        bind_plugin_factory_tools([item], str(Path(__file__).resolve().parents[2] / "satrap/expend/plugins/friend_manager/tools.py"))
        return item
    return next(t for t in _async_tools({"request_managers": "123", "allowed_callers": "123", "write_tools_enabled": write, **extra}) if t.tool_name == name)


def attach_friend_host(adapter):
    adapter._running = True
    if adapter.friend_host is None:
        def verify(source, target):
            source.verify(target)
            return "authorized"
        adapter.friend_host = FriendService("ob", FriendService._test_path, lambda: adapter, verify, lambda: ["123"])


async def incoming(adapter, kind="friend", flag="private-flag", **extra):
    adapter.bot_self_id = "10000"
    await adapter._register_request_flag({"self_id": "10000", "request_type": kind, "flag": flag,
                                          "user_id": "321", "comment": "我是测试好友", **extra})


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


@pytest.mark.asyncio
async def test_request_permissions_private_visibility_account_change_and_invalid_scope():
    adapter = _setup_adapter()
    await incoming(adapter)
    query = tool("friend_manager_list_requests", write=False)
    for origin in [_origin(), _origin(chat_type="FriendMessage", actor="999"), replace(_origin(chat_type="FriendMessage"), self_id="other")]:
        with bind_call_origin(origin):
            assert not query.is_available_for_call()
            assert not (await query.execute())["ok"]
    with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
        disabled = tool("friend_manager_list_requests", request_managers="")
        assert not disabled.is_available_for_call()
        assert not (await disabled.execute())["ok"]
        assert query.is_available_for_call()
        assert not (await query.execute(self_id="other"))["ok"]
        assert not (await query.execute(limit=True))["ok"]


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
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    registry.inbox.path.write_bytes(b"broken")
    with pytest.raises(sqlite3.DatabaseError):
        await registry.list_requests("friend", self_id="10000")


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
        assert bad["status"] == "error"
        prepared = await handle.execute(request_id=request_id, approve=True)
        assert prepared["data"]["state"] == "pending"
    adapter.group_action_handler.assert_awaited_once_with("456", "handle_group_request", {"flag": "private-flag", "sub_type": "add", "approve": True})
    adapter._bot.set_group_add_request.assert_not_called()
    with bind_call_origin(_origin(chat_id="789")):
        assert (await handle.execute(request_id=request_id, approve=True))["status"] == "error"


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
    assert result["status"] == "error" and "data" not in result


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


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_agent_queries_request_id_and_handles_request_in_private(tmp_path: Path, asynchronous):
    from .test_group_chat_reply import setup
    from .test_group_chat_plugin import Script, Model, AsyncModel, Invoker
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
