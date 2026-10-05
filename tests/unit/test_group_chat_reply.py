from __future__ import annotations

from unittest.mock import AsyncMock
from contextlib import contextmanager
from pathlib import Path
from typing import cast
import asyncio
import pytest

from satrap.core.config.platform_messages import ArchiveMessage, MessageScope, PlatformMessageStore
from satrap.core.platform import PlatformAdapter, PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig, current_adapter_manager, set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.group_chat.reply import bind_reply_turn, current_reply_turn
from satrap.core.call_context import bind_call_origin, bind_tool_workflow
from satrap.core.components import At, Plain, PlatformComponentType
from satrap.core.platform.identity import BotIdentity
from satrap.core.group_chat.service import GroupChatService
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.event import MessageEvent
from satrap.core.type import Group, MessageMember, PlatformMessage, PlatformMessageType
from satrap.core.storage import StorageLayout


SCOPE = MessageScope("ob", "10000", "group", "456")
COMPONENTS = [
    {"type": "text", "text": "甲: "}, {"type": "quote", "message_id": "77"},
    {"type": "mention", "source_message_id": "77"}, {"type": "text", "text": " 回复甲; "},
    {"type": "mention", "user_id": "789"}, {"type": "text", "text": " 回复乙"},
]


@pytest.fixture(autouse=True)
def restore_manager():
    previous = current_adapter_manager()
    yield
    set_current_adapter_manager(previous)


def setup(tmp_path: Path, *, private: bool = False) -> tuple[OneBotAdapter, MessageEvent, GroupChatService]:
    registry = PlatformAdapterRegistry()
    registry.register("onebot", OneBotAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="ob", type="onebot", settings={"reply_with_quote": True, "reply_with_mention": True}))
    assert isinstance(adapter, OneBotAdapter)
    adapter._bot = AsyncMock()
    adapter._running = True
    adapter.bot_self_id = adapter.client_self_id = "10000"
    adapter.message_archive = PlatformMessageStore(StorageLayout(tmp_path).platform_db("ob"), "ob")
    import time
    adapter.message_archive.record(SCOPE, ArchiveMessage("77", "123", time.time(), "甲提及了乙", mentions=["789"]))
    adapter._bot.get_group_member_info.return_value = {"group_id": 456, "user_id": 789, "nickname": "乙"}
    adapter.resolve_self_identity = AsyncMock(return_value=BotIdentity("10000", "机器人", "456", "群名片"))
    adapter.send_message = AsyncMock(return_value=SendReceipt("success", ("sent-1",)))
    set_current_adapter_manager(manager)
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE if private else PlatformMessageType.GROUP_MESSAGE
    message.self_id = "10000"
    message.session_id = "private%123" if private else "group%456"
    message.group_id = "" if private else "456"
    message.group = Group("456", "测试群")
    message.message_id = "77"
    message.sender = MessageMember(user_id="123", nickname="甲")
    message.message = [Plain("请回应讨论"), At(qq="10000")]
    event = MessageEvent("请回应讨论", message, adapter.meta(), message.session_id, adapter)
    event.is_wake = True
    return adapter, event, GroupChatService()


def send_mock(adapter: OneBotAdapter) -> AsyncMock:
    return cast(AsyncMock, adapter.send_message)


@contextmanager
def owner(event: MessageEvent):
    workflow = object()
    with bind_reply_turn(event) as turn, bind_call_origin(event.call_origin), bind_tool_workflow(workflow):
        turn.enabled, turn.workflow = True, workflow
        turn._enabled = lambda: True
        yield turn


@pytest.mark.asyncio
async def test_prepare_is_atomic_sender_based_and_sends_only_once(tmp_path):
    adapter, event, service = setup(tmp_path)
    with owner(event) as turn:
        result = await service.execute("group_chat_reply", {"components": COMPONENTS})
        assert result["status"] == "prepared" and result["sent"] is False
        send_mock(adapter).assert_not_awaited()
        again = await service.execute("group_chat_reply", {"components": COMPONENTS})
        assert again["error"]["code"] == "already_prepared"
        assert await turn.commit("模型重复的最终文本")
        assert await turn.commit("再次调用宿主也不能重发")
    assert current_reply_turn() is None
    send_mock(adapter).assert_awaited_once()
    chain = send_mock(adapter).call_args.args[1]
    assert [c.type for c in chain.components] == [PlatformComponentType.Reply, PlatformComponentType.Plain, PlatformComponentType.At,
                                                PlatformComponentType.Plain, PlatformComponentType.At, PlatformComponentType.Plain]
    assert [c.qq for c in chain.components if c.type == PlatformComponentType.At] == ["123", "789"]
    assert chain.components[0].id == "77"
    assert "重复" not in "".join(c.text for c in chain.components if c.type == PlatformComponentType.Plain)


@pytest.mark.asyncio
@pytest.mark.parametrize("components", [[], [{"type": "image", "url": "x"}], [{"type": "mention", "user_id": "789", "source_message_id": "77"}],
    [{"type": "quote", "message_id": "77"}, {"type": "quote", "message_id": "77"}, {"type": "text", "text": "x"}],
    [{"type": "text", "text": "x", "group_id": "other"}], [{"type": "mention", "user_id": True}],
    [{"type": "quote", "message_id": "77"}], [{"type": "text", "text": "x" * 100001}],
])
async def test_invalid_components_never_leave_half_a_draft(tmp_path, components):
    adapter, event, service = setup(tmp_path)
    with owner(event) as turn:
        result = await service.execute("group_chat_reply", {"components": components})
        assert result["ok"] is False and turn.draft is None
        result = await service.execute("group_chat_reply", {"components": [{"type": "text", "text": "完整草稿"}]})
        assert result["ok"]
    send_mock(adapter).assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_concurrency_keeps_one_draft(tmp_path):
    _, event, service = setup(tmp_path)
    with owner(event):
        results = await asyncio.gather(*(service.execute("group_chat_reply", {"components": COMPONENTS}) for _ in range(2)))
        assert sum(result["ok"] for result in results) == 1
        assert [result["error"]["code"] for result in results if not result["ok"]] == ["already_prepared"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "recalled", "unverified", "foreign", "connection", "disabled", "failed", "subagent", "route", "instance"])
async def test_untrusted_or_changed_targets_are_not_submitted(tmp_path, change):
    adapter, event, service = setup(tmp_path)
    store = adapter.message_archive
    assert store is not None
    if change == "unverified":
        import time
        store.record(SCOPE, ArchiveMessage("bad", "123", time.time(), "未核验"))
        import sqlite3
        with sqlite3.connect(store.database) as connection:
            connection.execute("UPDATE platform_messages SET verified=0 WHERE message_id='bad'")
    if change == "foreign":
        adapter._bot.get_msg.return_value = {"message_type": "group", "self_id": 10000, "group_id": 999, "message_id": 88,
                                             "user_id": 123, "sender": {"user_id": 123}, "time": 1800000000, "message": []}
    with owner(event) as turn:
        if change == "subagent":
            with bind_tool_workflow(object()):
                result = await service.execute("group_chat_reply", {"components": COMPONENTS})
            assert result["error"]["code"] == "wrong_executor" and turn.draft is None
        elif change in {"unverified", "foreign"}:
            result = await service.execute("group_chat_reply", {"components": [{"type": "mention", "source_message_id": "bad" if change == "unverified" else "88"}]})
            assert result["ok"] is False and turn.draft is None
        else:
            assert (await service.execute("group_chat_reply", {"components": COMPONENTS}))["ok"]
            if change == "deleted":
                store.delete(SCOPE, message_ids=["77"], expected_revision=0)
            elif change == "recalled":
                store.recall(SCOPE, "77")
            elif change == "connection":
                adapter._bot = AsyncMock()
            elif change == "disabled":
                adapter.config.enable = False
            elif change == "failed":
                turn.abort()
            elif change == "route":
                adapter.config.session_bindings = {"group": {"mode": "value", "provider": "edictum", "config_name": "new-agent"}}
                adapter.apply_agent_routes()
            elif change == "instance":
                set_current_adapter_manager(PlatformAdapterManager(PlatformAdapterRegistry()))
            await turn.commit("不能兜底发送此正文")
    send_mock(adapter).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "partial", "failed", "unknown"])
async def test_receipts_are_preserved_without_fallback_resend(tmp_path, status):
    adapter, event, service = setup(tmp_path)
    receipt = SendReceipt(status, ("segment-1",) if status in {"success", "partial"} else (), reason="test")
    send_mock(adapter).return_value = receipt
    with owner(event) as turn:
        assert (await service.execute("group_chat_reply", {"components": COMPONENTS}))["ok"]
        await turn.commit("正文")
        await turn.commit("正文")
    assert event.last_business_receipt is receipt
    send_mock(adapter).assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_after_submit_is_unknown_and_cannot_resend(tmp_path):
    adapter, event, service = setup(tmp_path)
    started = asyncio.Event()
    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Future()
    send_mock(adapter).side_effect = slow
    with owner(event) as turn:
        assert (await service.execute("group_chat_reply", {"components": COMPONENTS}))["ok"]
        task = asyncio.create_task(turn.commit("正文"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert event.last_business_receipt is not None and event.last_business_receipt.status == "unknown"
        await turn.commit("不能重发")
    send_mock(adapter).assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["", "最终正文"])
async def test_fallback_uses_only_final_response_not_tool_preface(tmp_path, response):
    adapter, event, _ = setup(tmp_path)
    with owner(event) as turn:
        turn.capture("查询前的说明, 不能作为最终回复")
        assert await turn.commit(response)
    if not response:
        send_mock(adapter).assert_not_awaited()
    else:
        send_mock(adapter).assert_awaited_once()
        text = "".join(component.text for component in send_mock(adapter).call_args.args[1].components if component.type == PlatformComponentType.Plain)
        assert text.lstrip() == response


@pytest.mark.asyncio
async def test_explicit_text_suppresses_auto_quote_and_mention_but_fallback_keeps_them(tmp_path):
    adapter, event, service = setup(tmp_path)
    with owner(event) as turn:
        assert (await service.execute("group_chat_reply", {"components": [{"type": "text", "text": "显式文本"}]}))["ok"]
        await turn.commit("不重复")
    assert [component.type for component in send_mock(adapter).call_args.args[1].components] == [PlatformComponentType.Plain]
    send_mock(adapter).reset_mock()
    with owner(event) as turn:
        event._has_send_oper = False
        await turn.commit("兜底文本")
    assert [component.type for component in send_mock(adapter).call_args.args[1].components] == [PlatformComponentType.Reply, PlatformComponentType.At, PlatformComponentType.Plain]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_structured_reply_uses_actual_onebot_split_queue_and_archives_confirmed_segments(tmp_path, failed):
    from aiocqhttp.exceptions import ActionFailed
    adapter, event, service = setup(tmp_path)
    adapter.send_message = OneBotAdapter.send_message.__get__(adapter)
    adapter.config.settings["message_text_limit"] = 64
    adapter._bot.send_group_msg.side_effect = [{"message_id": 900}, ActionFailed({"retcode": 100, "msg": "模拟失败"}) if failed else {"message_id": 901}, {"message_id": 902}]
    components = [{"type": "quote", "message_id": "77"}, {"type": "mention", "source_message_id": "77"},
                  {"type": "text", "text": "甲" * 150}, {"type": "mention", "user_id": "789"}]
    with owner(event) as turn:
        assert (await service.execute("group_chat_reply", {"components": components}))["ok"]
        await turn.commit("不能重发")
        await turn.commit("再次提交")
    receipt = event.last_business_receipt
    assert receipt is not None and receipt.status == ("partial" if failed else "success")
    assert receipt.message_ids == (("900",) if failed else ("900", "901", "902"))
    calls = adapter._bot.send_group_msg.await_args_list
    assert len(calls) == (2 if failed else 3)
    assert [segment["type"] for segment in calls[0].kwargs["message"][:2]] == ["reply", "at"]
    if not failed:
        assert "".join(segment["data"]["text"] for call in calls for segment in call.kwargs["message"] if segment["type"] == "text") == "甲" * 150
        assert calls[-1].kwargs["message"][-1]["data"]["qq"] == "789"
    await adapter.drain_message_archive()
    assert adapter.message_archive is not None
    assert adapter.message_archive.get(SCOPE, "900") is not None
    assert (adapter.message_archive.get(SCOPE, "901") is None) == failed


@pytest.mark.asyncio
async def test_ended_scope_copies_cannot_prepare_or_submit(tmp_path):
    adapter, event, service = setup(tmp_path)
    ready = asyncio.Event()
    async def copied():
        await ready.wait()
        return await service.execute("group_chat_reply", {"components": COMPONENTS})
    with owner(event):
        task = asyncio.create_task(copied())
    ready.set()
    result = await task
    assert result["error"]["code"] == "stale_call"
    send_mock(adapter).assert_not_awaited()


@pytest.mark.asyncio
async def test_new_adapter_and_opaque_ids_use_same_reply_contract(tmp_path):
    class OpaqueAdapter(PlatformAdapter):
        async def run(self):
            return None

        def meta(self):
            from satrap.core.platform.event import PlatformMetadata
            return PlatformMetadata("扩展平台", self.config.id)

        def group_chat_capabilities(self):
            return {**super().group_chat_capabilities(), **{key: {"state": "supported"} for key in ("text", "quote", "mention")}}

    registry = PlatformAdapterRegistry()
    registry.register("opaque", OpaqueAdapter)
    manager = PlatformAdapterManager(registry)
    adapter = manager.add_adapter(PlatformConfig(id="extra", type="opaque"))
    assert isinstance(adapter, OpaqueAdapter)
    adapter.client_self_id = "bot:abc"
    adapter.message_archive = PlatformMessageStore(tmp_path / "extra.db", "extra")
    import time
    scope = MessageScope("extra", "bot:abc", "group", "room:abc")
    adapter.message_archive.record(scope, ArchiveMessage('msg:"<a>', 'member:"<b>', time.time(), "资料"))
    adapter.send_message = AsyncMock(return_value=SendReceipt("success", ("out:1",)))
    set_current_adapter_manager(manager)
    message = PlatformMessage()
    message.type = PlatformMessageType.GROUP_MESSAGE
    message.self_id, message.group_id, message.session_id = "bot:abc", "room:abc", "room:abc"
    message.message_id = 'msg:"<a>'
    message.sender = MessageMember(user_id='member:"<b>', nickname="甲")
    message.message = [Plain("新平台")]
    event = MessageEvent("新平台", message, adapter.meta(), message.session_id, adapter)
    with owner(event) as turn:
        result = await GroupChatService().execute("group_chat_reply", {"components": [
            {"type": "quote", "message_id": 'msg:"<a>'}, {"type": "mention", "user_id": 'member:"<b>'},
            {"type": "text", "text": " 保留字符"},
        ]})
        assert result["ok"]
        await turn.commit("只发送草稿")
    sent = cast(AsyncMock, adapter.send_message)
    sent.assert_awaited_once()
    assert sent.call_args.args[1].components[0].id == 'msg:"<a>'
    assert sent.call_args.args[1].components[1].qq == 'member:"<b>'
