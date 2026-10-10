"""原消息转发的对话归属, 完整性与同步异步契约"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from types import SimpleNamespace
import asyncio
import json
import weakref

from aiocqhttp.exceptions import ActionFailed
import pytest

from satrap.core.call_context import bind_call_origin
from satrap.core.message_forward import ForwardError
from satrap.core.platform import set_current_adapter_manager
from satrap.core.platform.onebot.forwarding import OneBotForwarding
from satrap.edictum import AsyncSimpleSession
from satrap.core.framework.Base import Session
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_migrations import migrate_forward_tool_specs
from satrap.expend.plugins.message_forward.tools import get_tools, AsyncForwardTool
from satrap.expend.plugins.message_forward.commands import build_commands
from satrap.core.plugin_authorization import PluginEntryBinding, bind_native_command, bind_authorization_step
from satrap.edictum.plugin_permissions import validate_permission_install
from .test_group_admin_plugin import _setup_adapter, _origin


@pytest.fixture(autouse=True)
def cleanup():
    yield
    set_current_adapter_manager(None)


def setup():
    adapter = _setup_adapter()
    adapter.bot_self_id = "10000"
    adapter._running = True
    adapter._bot.send_group_forward_msg.return_value = {"message_id": 9}
    adapter._bot.send_private_forward_msg.return_value = {"message_id": 10}
    adapter._bot.forward_group_single_msg.return_value = {"message_id": 11}
    adapter._bot.forward_friend_single_msg.return_value = {"message_id": 12}
    return adapter


def message(kind: str, conversation_id: str, identity: str = "77", *, forward: bool = False) -> dict[str, Any]:
    segments = [{"type": "text", "data": {"text": "原文" * 3000}}, {"type": "image", "data": {"file": "original-image"}}]
    if forward:
        segments = [{"type": "forward", "data": {"id": "original-card"}}]
    return {"message_id": int(identity), "self_id": 10000, "message_type": "group" if kind == "group" else "private",
            **({"group_id": int(conversation_id)} if kind == "group" else {"user_id": int(conversation_id)}),
            "sender": {"user_id": int(conversation_id) if kind == "private" else 321, "nickname": "原发送者"}, "message": segments}


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind,target_kind", [("group", "group"), ("group", "private"), ("private", "group"), ("private", "private")])
@pytest.mark.parametrize("mode", ["merge", "existing_forward"])
async def test_original_forward_uses_real_message_reference_in_all_directions(source_kind, target_kind, mode):
    adapter = setup()
    source = {"conversation_kind": source_kind, "conversation_id": "456"}
    target = {"conversation_kind": target_kind, "conversation_id": "789"}
    adapter._bot.get_msg.return_value = message(source_kind, "456", forward=mode == "existing_forward")
    service = OneBotForwarding(adapter, _origin(), lambda: None)
    result = await service.send(["77"], mode, source, target)
    assert result["status"] == "success" and result["content_modified"] is False
    if mode == "merge":
        call = adapter._bot.send_group_forward_msg if target_kind == "group" else adapter._bot.send_private_forward_msg
        assert call.await_args.kwargs["messages"] == [{"type": "node", "data": {"id": "77"}}]
    else:
        call = adapter._bot.forward_group_single_msg if target_kind == "group" else adapter._bot.forward_friend_single_msg
        assert call.await_args.kwargs["message_id"] == "77"
    adapter._bot.get_forward_msg.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"group_id": 999}, {"self_id": 999}, {"message_id": 88}, {"message_type": "private"}])
async def test_wrong_source_is_rejected_before_send(changes):
    adapter = setup()
    adapter._bot.get_msg.return_value = {**message("group", "456"), **changes}
    service = OneBotForwarding(adapter, _origin(), lambda: None)
    with pytest.raises(ForwardError, match="来源"):
        await service.send(["77"], "merge", {"conversation_kind": "group", "conversation_id": "456"}, {"conversation_kind": "private", "conversation_id": "789"})
    adapter._bot.send_private_forward_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_sender_from_another_conversation_does_not_authorize_read():
    adapter = setup()
    payload = message("private", "789")
    adapter._bot.get_msg.return_value = payload
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).source_message({"conversation_kind": "private", "conversation_id": "456"}, "77")


@pytest.mark.asyncio
async def test_private_target_does_not_override_contradictory_sender():
    adapter = setup()
    payload = message("private", "456")
    payload["sender"]["user_id"] = 789
    adapter._bot.get_msg.return_value = payload
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).source_message({"conversation_kind": "private", "conversation_id": "456"}, "77")


@pytest.mark.asyncio
@pytest.mark.parametrize("segments", [[{"type": "text", "data": {"text": "forward id: original-card"}}], [{"type": "node", "data": {"content": [{"type": "forward", "data": {"id": "original-card"}}]}}]])
async def test_body_or_nested_card_does_not_authorize_top_level_forward(segments):
    adapter = setup()
    adapter._bot.get_msg.return_value = {**message("group", "456"), "message": segments}
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).read("77", {"conversation_kind": "group", "conversation_id": "456"})
    adapter._bot.get_forward_msg.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("contradiction", [{"self_id": 999}, {"group_id": 999}])
async def test_forward_response_cannot_change_proved_account_or_group(contradiction):
    adapter = setup()
    adapter._bot.get_msg.return_value = message("group", "456", forward=True)
    adapter._bot.get_forward_msg.return_value = {"messages": [], **contradiction}
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).read("77", {"conversation_kind": "group", "conversation_id": "456"})


@pytest.mark.asyncio
async def test_native_command_requires_actual_native_scope_then_cross_forwards(tmp_path):
    adapter = setup()
    adapter._bot.get_msg.return_value = message("group", "456")
    class FakeSession(AsyncSimpleSession):
        def __init__(self):
            self.command_handler = cast(Any, SimpleNamespace(_permission_bindings={}))
    session = FakeSession()
    config = {"send_enabled": True, "cross_conversation_enabled": True, "write_callers": "123", "cross_callers": "123"}
    _, commands = build_commands(session, config)
    entry = PluginCatalog(user_dir=tmp_path / "plugins").get("message_forward")
    assert entry is not None
    validate_permission_install(entry.permissions, {name: object() for name in entry.capabilities["tools"]}, commands)
    handler = commands["forward"]
    binding = PluginEntryBinding("message_forward", "commands", "forward", entry.permissions, config, weakref.ref(handler))
    session.command_handler._permission_bindings["forward"] = binding
    with bind_call_origin(_origin()):
        denied = await handler("merge", "private", "789", "77")
        assert "权限" in denied
        adapter._bot.get_msg.assert_not_awaited()
        with bind_native_command(binding):
            result = json.loads(await handler("merge", "private", "789", "77"))
            assert result["status"] == "success" and not result["content_modified"]
    adapter._bot.send_private_forward_msg.assert_awaited_once_with(user_id=789, messages=[{"type": "node", "data": {"id": "77"}}])


@pytest.mark.asyncio
async def test_preview_truncation_does_not_change_original_forward_payload():
    adapter = setup()
    adapter._bot.get_msg.return_value = message("group", "456", forward=True)
    adapter._bot.get_forward_msg.return_value = {"messages": [{"content": "x" * 20000}] * 25}
    service = OneBotForwarding(adapter, _origin(), lambda: None)
    source = {"conversation_kind": "group", "conversation_id": "456"}
    preview = await service.read("77", source)
    assert preview["preview_truncated"] and len(preview["preview"]) == 12000
    await service.send(["77"], "existing_forward", source, source)
    adapter._bot.forward_group_single_msg.assert_awaited_once_with(group_id=456, message_id="77")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["merge", "existing_forward"])
async def test_unsupported_original_forward_never_degrades_to_text(mode):
    adapter = setup()
    adapter._bot.get_msg.return_value = message("group", "456", forward=True)
    error = ActionFailed({"retcode": 10002})
    adapter._bot.send_group_forward_msg.side_effect = error
    adapter._bot.forward_group_single_msg.side_effect = error
    source = {"conversation_kind": "group", "conversation_id": "456"}
    result = await OneBotForwarding(adapter, _origin(), lambda: None).send(["77"], mode, source, source)
    assert result["status"] == "failed"
    adapter._bot.send_group_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_change_during_lookup_blocks_send():
    adapter = setup()
    async def lookup(**kwargs):
        adapter.bot_self_id = "20000"
        return message("group", "456")
    adapter._bot.get_msg.side_effect = lookup
    source = {"conversation_kind": "group", "conversation_id": "456"}
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).send(["77"], "merge", source, source)
    adapter._bot.send_group_forward_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_plugin_authorizes_model_cross_conversation_and_blocks_spoofed_parameters():
    adapter = setup()
    class FakeSession(AsyncSimpleSession):
        def __init__(self):
            pass
    tools = get_tools(FakeSession(), {"send_enabled": True, "write_callers": "123", "cross_conversation_enabled": True, "cross_callers": "123"})
    tool = next(item for item in tools if item.tool_name == "message_forward_send")
    adapter._bot.get_msg.return_value = message("group", "456")
    with bind_call_origin(_origin()):
        assert tool.is_available_for_call()
        result = await tool.execute(message_ids=["77"], target={"conversation_kind": "private", "conversation_id": "789"})
        assert result["ok"]
        adapter._bot.send_private_forward_msg.assert_awaited_once()
        tool.config["cross_callers"] = "other"
        result = await tool.execute(message_ids=["77"], target={"conversation_kind": "private", "conversation_id": "789"})
        assert result["error"]["code"] == "permission_denied"
        result = await tool.execute(message_ids=["77"], actor_id="123", content="假原文")
        assert result["error"]["code"] == "invalid_parameters"
    adapter._bot.get_msg.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_and_async_tools_have_same_definitions_and_send_original_references():
    adapter = setup()
    adapter._loop = asyncio.get_running_loop()
    adapter._bot.get_msg.return_value = message("group", "456")
    class FakeSession(AsyncSimpleSession):
        def __init__(self):
            pass
    config = {"send_enabled": True, "write_callers": "123"}
    sync_tools = get_tools(cast(Session, object()), config)
    async_tools = get_tools(FakeSession(), config)
    for sync, async_ in zip(sync_tools, async_tools):
        assert sync.get_tool_defined() == async_.get_tool_defined()
        parameters = sync.get_tool_defined()["function"]["parameters"]
        assert parameters["additionalProperties"] is False
    sync = next(item for item in sync_tools if item.tool_name == "message_forward_send")
    async_ = next(item for item in async_tools if item.tool_name == "message_forward_send")
    with bind_call_origin(_origin()):
        a = await asyncio.to_thread(sync.execute, message_ids=["77"])
        b = await async_.execute(message_ids=["77"])
    assert a == b and a["data"]["content_modified"] is False
    assert adapter._bot.send_group_forward_msg.await_count == 2
    for call in adapter._bot.send_group_forward_msg.await_args_list:
        assert call.kwargs["messages"] == [{"type": "node", "data": {"id": "77"}}]


def test_migration_does_not_enable_new_private_or_original_send_capabilities(tmp_path):
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    original = [{"name": "group_admin", "enabled": True, "config": {"allowed_callers": "123", "write_tools_enabled": True},
                 "capabilities": {"tools": {"group_admin_get_forward": True, "group_admin_send_forward": True}}}]
    migrated = migrate_forward_tool_specs(original, catalog)
    forward = next(item for item in migrated if item.get("name") == "message_forward")
    assert "allow_private" not in forward["config"] and "allowed_groups" not in forward["config"]
    assert forward["config"]["cross_conversation_enabled"] is False
    assert forward["capabilities"]["tools"]["message_forward_compose"] is True
    assert forward["capabilities"]["tools"]["message_forward_send"] is False
    assert not forward["capabilities"]["commands"]["forward"]
    assert migrate_forward_tool_specs(migrated, catalog) == migrated
    assert "group_admin_get_forward" in original[0]["capabilities"]["tools"]


def test_new_group_admin_configuration_does_not_install_forward_plugin(tmp_path):
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    for value in [["group_admin"], [{"name": "group_admin", "config": {"write_tools_enabled": True}}]]:
        assert migrate_forward_tool_specs(value, catalog) == value


@pytest.mark.asyncio
async def test_permission_revocation_during_lookup_bypasses_step_cache():
    adapter = setup()
    class FakeSession(AsyncSimpleSession):
        def __init__(self):
            pass
    handle = next(item for item in get_tools(FakeSession(), {"send_enabled": True, "write_callers": "123"}) if item.tool_name == "message_forward_send")
    async def lookup(**kwargs):
        handle.config["write_callers"] = ""
        return message("group", "456")
    adapter._bot.get_msg.side_effect = lookup
    with bind_call_origin(_origin()), bind_authorization_step():
        assert handle.is_available_for_call()
        result = await handle.execute(message_ids=["77"])
        assert not result["ok"]
    adapter._bot.send_group_forward_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejection_at_queued_dispatch_is_known_failure_without_send():
    adapter = setup()
    adapter._bot.get_msg.return_value = message("group", "456")
    count = [0]
    def check():
        count[0] += 1
        if count[0] >= 3:
            raise PermissionError("权限已撤销")
    source = {"conversation_kind": "group", "conversation_id": "456"}
    result = await OneBotForwarding(adapter, _origin(), check).send(["77"], "merge", source, source)
    assert result["status"] == "failed" and result["reason"] == "preflight_rejected"
    adapter._bot.send_group_forward_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_read_requires_cross_grant_and_rechecks_it_after_lookup():
    adapter = setup()
    class FakeSession(AsyncSimpleSession):
        def __init__(self):
            pass
    config = {"cross_conversation_enabled": True, "cross_callers": "123", "allow_private": False, "allowed_groups": "other"}
    handle = next(item for item in get_tools(FakeSession(), config) if item.tool_name == "message_forward_read")
    adapter._bot.get_msg.return_value = message("private", "789", forward=True)
    adapter._bot.get_forward_msg.return_value = {"messages": []}
    source = {"conversation_kind": "private", "conversation_id": "789"}
    with bind_call_origin(_origin()):
        assert (await handle.execute(source_message_id="77", source=source))["ok"]
        handle.config["cross_callers"] = ""
        denied = await handle.execute(source_message_id="77", source=source)
        assert denied["error"]["code"] == "permission_denied"
        handle.config["cross_callers"] = "123"
        async def revoke(**kwargs):
            handle.config["cross_callers"] = ""
            return message("private", "789", forward=True)
        adapter._bot.get_msg.side_effect = revoke
        assert not (await handle.execute(source_message_id="77", source=source))["ok"]
    assert adapter._bot.get_msg.await_count == 2


@pytest.mark.asyncio
async def test_native_forward_timeout_is_unknown_and_does_not_retry_or_degrade():
    adapter = setup()
    adapter._bot.get_msg.return_value = message("group", "456", forward=True)
    adapter._bot.forward_group_single_msg.side_effect = asyncio.TimeoutError()
    source = {"conversation_kind": "group", "conversation_id": "456"}
    result = await OneBotForwarding(adapter, _origin(), lambda: None).send(["77"], "existing_forward", source, source)
    assert result["status"] == "unknown"
    adapter._bot.forward_group_single_msg.assert_awaited_once()
    adapter._bot.send_group_forward_msg.assert_not_awaited()
    adapter._bot.send_group_msg.assert_not_awaited()
