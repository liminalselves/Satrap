"""业务链路场景与集中边界契约"""
from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock
from aiocqhttp.exceptions import ActionFailed
import pytest
from satrap.core.pipeline.input_projection import FORWARD_TEXT_LIMIT, project_input, resolve_forwards
from satrap.core.platform.onebot.onebot_utils import FORWARD_NODE_LIMIT, onebot_segments_to_components
from satrap.core.platform.onebot.onebot_utils import parse_forward_nodes
from satrap.core.platform.onebot.outbound import flatten_forward_nodes, split_forward_turns
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.config.group_action_origin import bind_group_action_preflight
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.components import Forward, Image, Node, Nodes, Plain
from satrap.core.platform.event import MessageChain
from satrap.core.platform import PlatformConfig
from satrap.core.framework.providers import BindingState, BindingStatus
from typing import Any
from types import SimpleNamespace
import asyncio
import json
import weakref
from satrap.core.call_context import bind_call_origin
from satrap.core.message_forward import ForwardError
from satrap.core.platform import set_current_adapter_manager
from satrap.core.platform.onebot.forwarding import OneBotForwarding
from satrap.edictum import AsyncSimpleSession
from satrap.core.framework.Base import Session
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_migrations import migrate_forward_tool_specs
from satrap.expend.plugins.message_forward.tools import get_tools
from satrap.expend.plugins.message_forward.commands import build_commands
from satrap.core.plugin_authorization import PluginEntryBinding, bind_native_command, bind_authorization_step
from satrap.edictum.plugin_permissions import validate_permission_install
from support.administration import _setup_adapter, _origin

class _RunnableRegistry:
    """绑定判定恒为可运行的会话定义注册表替身"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """恒定答复可运行"""
        return BindingStatus(BindingState.RUNNABLE)

def forward_payload(nodes: list[dict[str, object]] | None = None, **extra: object) -> dict[str, object]:
    """构造 get_forward_msg 响应"""
    default_nodes: list[dict[str, object]] = [
        {"type": "node", "data": {"nickname": "小明", "user_id": 321, "time": 1700000000,
                                  "content": [{"type": "text", "data": {"text": "第一句"}}]}},
        {"type": "node", "data": {"nickname": "小红", "user_id": 654, "time": 1700000001,
                                  "content": [{"type": "text", "data": {"text": "第二句"}}]}},
    ]
    return {"messages": nodes if nodes is not None else default_nodes, **extra}

async def make_event(settings: dict[str, object] | None = None, segments: list[dict[str, object]] | None = None):
    """用真实适配器入站转换构造已唤醒的群消息事件"""
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings or {})))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    segments = segments or [
        {"type": "forward", "data": {"id": "fwd-1"}},
        {"type": "at", "data": {"qq": "10000"}},
        {"type": "text", "data": {"text": " 看看这个"}},
    ]
    await adapter._handle_group_message({
        "self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 77,
        "message_type": "group", "message": segments,
    })
    return adapter, adapter._event_queue.get_nowait()

class TestForwardParsing:
    def test_forward_segment_without_inline_content_keeps_id_only(self):
        components, text = onebot_segments_to_components([{"type": "forward", "data": {"id": "f1"}}])
        assert isinstance(components[0], Forward) and components[0].id == "f1" and components[0].nodes is None
        assert text == "[转发]"

    def test_inline_content_is_parsed_without_lookup(self):
        segment = {"type": "forward", "data": {"id": "f1", "content": [
            {"nickname": "小明", "user_id": 321, "content": [{"type": "text", "data": {"text": "内联"}}]},
        ]}}
        components, _ = onebot_segments_to_components([segment])
        forward = components[0]
        assert isinstance(forward, Forward) and forward.nodes is not None
        assert forward.nodes[0].name == "小明" and isinstance(forward.nodes[0].content[0], Plain)

    def test_node_count_is_capped(self):
        items = [{"nickname": str(i), "content": "x"} for i in range(FORWARD_NODE_LIMIT + 5)]
        assert len(parse_forward_nodes(items)) == FORWARD_NODE_LIMIT

    def test_message_field_and_string_content_and_nested_forward(self):
        nodes = parse_forward_nodes([{
            "type": "node",
            "data": {"name": "小明", "uin": 321, "message": "纯文本"},
        }, {
            "type": "node",
            "data": {"nickname": "小红", "user_id": 654,
                     "content": [{"type": "forward", "data": {"id": "inner"}}]},
        }])
        assert nodes[0].name == "小明" and nodes[0].uin == "321"
        assert isinstance(nodes[0].content[0], Plain) and nodes[0].content[0].text == "纯文本"
        assert isinstance(nodes[1].content[0], Forward) and nodes[1].content[0].id == "inner"

    def test_deeply_nested_inline_forward_stops_expanding(self):
        segment: dict = {"type": "text", "data": {"text": "最深"}}
        for _ in range(12):
            segment = {"type": "forward", "data": {"id": "x", "content": [{"type": "node", "data": {"name": "n", "content": [segment]}}]}}
        components, _ = onebot_segments_to_components([segment])
        depth = 0
        current = components[0]
        while isinstance(current, Forward) and current.nodes:
            depth += 1
            current = current.nodes[0].content[0]
        assert isinstance(current, Forward) and current.nodes is None
        assert depth == 2

    def test_non_dict_and_missing_content_nodes_are_skipped_or_empty(self):
        nodes = parse_forward_nodes(["bad", {"nickname": "无正文"}])
        assert len(nodes) == 1 and nodes[0].content == []

class TestFetchForwardMessage:
    @pytest.mark.asyncio
    async def test_fetch_parses_nodes_and_checks_account(self):
        adapter, event = await make_event()
        adapter._bot.get_forward_msg.return_value = forward_payload()
        nodes = await adapter.fetch_forward_message("fwd-1", event.session_id)
        assert nodes is not None and len(nodes) == 2 and nodes[0].name == "小明"
        adapter._bot.get_forward_msg.assert_awaited_once_with(id="fwd-1")
        adapter._bot.get_forward_msg.return_value = forward_payload(self_id=20000)
        assert await adapter.fetch_forward_message("fwd-1", event.session_id) is None

    @pytest.mark.asyncio
    async def test_fetch_rejects_tightened_whitelist_and_bad_payload(self):
        adapter, event = await make_event()
        adapter.config.settings["group_whitelist"] = ["789"]
        assert await adapter.fetch_forward_message("fwd-1", event.session_id) is None
        adapter._bot.get_forward_msg.assert_not_awaited()
        adapter, event = await make_event()
        adapter._bot.get_forward_msg.return_value = {"messages": "not-a-list"}
        assert await adapter.fetch_forward_message("fwd-1", event.session_id) is None
        adapter._bot.get_forward_msg.return_value = {"messages": [], "blob": "x" * 300000}
        assert await adapter.fetch_forward_message("fwd-1", event.session_id) is None

    @pytest.mark.asyncio
    async def test_fetch_timeout_and_missing_bot_return_none(self):
        adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
        assert await adapter.fetch_forward_message("fwd-1", "group%456") is None
        adapter, event = await make_event()
        adapter._bot.get_forward_msg.side_effect = RuntimeError("boom")
        assert await adapter.fetch_forward_message("fwd-1", event.session_id) is None

    @pytest.mark.asyncio
    async def test_fetch_rejects_contradictory_group_when_expected(self):
        adapter, event = await make_event()
        adapter._bot.get_forward_msg.return_value = {**forward_payload(), "group_id": 999}
        # 未声明期望群号时保持既有行为 (入站补全不额外回源)
        assert await adapter.fetch_forward_message("fwd-1", event.session_id) is not None
        assert await adapter.fetch_forward_message("fwd-1", event.session_id, expect_group_id="456") is None
        adapter._bot.get_forward_msg.return_value = {**forward_payload(), "group_id": 456}
        assert await adapter.fetch_forward_message("fwd-1", event.session_id, expect_group_id="456") is not None

class TestResolveAndProjection:
    @pytest.mark.asyncio
    async def test_resolve_fills_nodes_and_projection_renders_block(self):
        adapter, event = await make_event()
        adapter._bot.get_forward_msg.return_value = forward_payload()
        status = await resolve_forwards(event)
        assert status == "resolved"
        projected = project_input(event, "none", status)
        assert projected.forward_status == "resolved"
        assert projected.message.startswith("[转发消息 2 条:\n- 小明: 第一句\n- 小红: 第二句]")
        assert "[转发]" not in projected.message
        assert projected.message.endswith("@10000 看看这个")

    @pytest.mark.asyncio
    async def test_inline_nodes_skip_lookup(self):
        adapter, event = await make_event(segments=[
            {"type": "forward", "data": {"id": "f1", "content": [
                {"nickname": "小明", "user_id": 321, "content": "内联正文"},
            ]}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        assert await resolve_forwards(event) == "resolved"
        adapter._bot.get_forward_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lookup_disabled_and_failed_keep_placeholder(self):
        adapter, event = await make_event(settings={"forward_lookup": False})
        status = await resolve_forwards(event)
        assert status == "disabled"
        adapter._bot.get_forward_msg.assert_not_awaited()
        projected = project_input(event, "none", status)
        assert "[转发]" in projected.message and "forward_unresolved" in projected.notes
        adapter, event = await make_event()
        adapter._bot.get_forward_msg.side_effect = RuntimeError("down")
        assert await resolve_forwards(event) == "unavailable"

    @pytest.mark.asyncio
    async def test_resolve_limit_leaves_later_forwards_unresolved(self):
        adapter, event = await make_event(segments=[
            {"type": "forward", "data": {"id": "a"}},
            {"type": "forward", "data": {"id": "b"}},
            {"type": "forward", "data": {"id": "c"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_forward_msg.return_value = forward_payload()
        status = await resolve_forwards(event)
        assert status == "partial"
        # 每事件最多回源两条转发, 第三条保留占位
        assert adapter._bot.get_forward_msg.await_count == 2
        forwards = [c for c in event.get_messages() if isinstance(c, Forward)]
        assert forwards[0].nodes is not None and forwards[1].nodes is not None and forwards[2].nodes is None

    @pytest.mark.asyncio
    async def test_forward_media_shares_budget_and_text_truncated(self):
        big = "y" * (FORWARD_TEXT_LIMIT + 50)
        adapter, event = await make_event({"input_media_limit": 4}, segments=[
            {"type": "reply", "data": {"id": "5"}},
            {"type": "forward", "data": {"id": "f1"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_msg.return_value = {
            "message_id": 5, "group_id": 456, "message_type": "group", "self_id": 10000,
            "sender": {"user_id": 321, "nickname": "小明"}, "time": 1700000000,
            "message": [{"type": "image", "data": {"url": f"http://x/q{i}.png"}} for i in range(3)],
        }
        adapter._bot.get_forward_msg.return_value = forward_payload(nodes=[
            {"nickname": "小红", "user_id": 654, "content": [{"type": "text", "data": {"text": big}}]},
            {"nickname": "小蓝", "user_id": 655,
             "content": [{"type": "image", "data": {"url": f"http://x/f{i}.png"}} for i in range(3)]},
        ])
        from satrap.core.pipeline.input_projection import resolve_quotes
        quote_status = await resolve_quotes(event)
        forward_status = await resolve_forwards(event)
        projected = project_input(event, quote_status, forward_status)
        assert len(projected.images) == 4
        assert "forward_media_truncated" in projected.notes and "forward_truncated" in projected.notes
        assert "[引用 小明 的消息: [图片][图片][图片]]" in projected.message
        assert "[转发消息 2 条:" in projected.message

    @pytest.mark.asyncio
    async def test_scheduler_passes_forward_context_to_session(self):
        adapter, event = await make_event(segments=[
            {"type": "forward", "data": {"id": "f1"}},
            {"type": "at", "data": {"qq": "10000"}},
        ])
        adapter._bot.get_forward_msg.return_value = forward_payload()
        manager = AsyncMock()
        manager.provider_registry = _RunnableRegistry()
        manager.handle_call_async.return_value = "收到"
        await PipelineScheduler(manager).execute(event)
        call = manager.handle_call_async.call_args.args[0]
        assert "[转发消息 2 条:" in call.message
        assert event.get_extra("input_projection").forward_status == "resolved"

    @pytest.mark.asyncio
    async def test_unwoken_forward_does_not_lookup(self):
        adapter, event = await make_event(segments=[
            {"type": "forward", "data": {"id": "f1"}},
            {"type": "text", "data": {"text": "没叫机器人"}},
        ])
        manager = AsyncMock()
        manager.provider_registry = _RunnableRegistry()
        await PipelineScheduler(manager).execute(event)
        adapter._bot.get_forward_msg.assert_not_awaited()
        manager.handle_call_async.assert_not_awaited()

class TestOutboundForward:
    def _adapter(self, **settings: object) -> OneBotAdapter:
        adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=settings))
        adapter._bot = AsyncMock()
        return adapter

    @pytest.mark.asyncio
    async def test_forward_waits_for_async_authorization_and_does_not_send_after_rejection(self):
        adapter = self._adapter()
        checked = []

        async def preflight():
            checked.append(True)
            raise PermissionError("管理员权限已撤销")

        with bind_group_action_preflight(preflight):
            receipt = await adapter.send_message("group%456", MessageChain([Nodes([Node([Plain("甲")])])]))
        assert checked == [True] and receipt.status == "failed" and receipt.reason == "preflight_rejected"
        adapter._bot.send_group_forward_msg.assert_not_awaited()

    def test_split_turns_merges_adjacent_and_preserves_order(self):
        node_a, node_b = Node([Plain("一")]), Node([Plain("二")])
        turns = split_forward_turns([Plain("A"), node_a, Nodes([node_b]), Plain("C")])
        assert [kind for kind, _ in turns] == ["normal", "forward", "normal"]
        assert turns[1][1] == [node_a, node_b]
        assert split_forward_turns([]) == []
        assert [kind for kind, _ in split_forward_turns([node_a])] == ["forward"]

    def test_split_turns_diverts_files_without_merging(self):
        from satrap.core.components import File

        node = Node([Plain("一")])
        file_a, file_b = File(name="a.bin"), File(name="b.bin")
        turns = split_forward_turns([Plain("A"), file_a, file_b, node, Plain("B"), file_a])
        assert [kind for kind, _ in turns] == ["normal", "file", "file", "forward", "normal", "file"]
        assert turns[1][1] == [file_a] and turns[2][1] == [file_b]
        assert turns[5][1] == [file_a]

    def test_flatten_replaces_nested_forward_with_placeholder(self):
        flat = flatten_forward_nodes([Node([Plain("甲"), Forward(id="f9")]), Node([Plain("乙")])])
        assert [type(c) for c in flat] == [Plain, Plain, Plain]
        assert cast(Plain, flat[1]).text == "[转发]"

    @pytest.mark.asyncio
    async def test_mixed_chain_sends_normal_forward_normal_in_order(self):
        adapter = self._adapter()
        order: list[str] = []

        async def send_normal(**kwargs: object) -> dict[str, int]:
            order.append("send_group_msg")
            return {"message_id": len(order)}

        async def send_forward(**kwargs: object) -> dict[str, int]:
            order.append("send_group_forward_msg")
            return {"message_id": 2}

        adapter._bot.send_group_msg.side_effect = send_normal
        adapter._bot.send_group_forward_msg.side_effect = send_forward
        nodes = [Node([Plain("节点")], name="小明", uin="321")]
        receipt = await adapter.send_message("group%456", MessageChain([Plain("前文"), Nodes(nodes), Plain("后文")]))
        assert receipt.status == "success" and receipt.message_ids == ("1", "2", "3")
        forward_call = adapter._bot.send_group_forward_msg.await_args
        assert forward_call.kwargs["group_id"] == 456
        assert forward_call.kwargs["messages"] == [await nodes[0].to_dict()]
        assert order == ["send_group_msg", "send_group_forward_msg", "send_group_msg"]

    @pytest.mark.asyncio
    async def test_private_forward_uses_private_api(self):
        adapter = self._adapter()
        adapter._bot.send_private_forward_msg.return_value = {"message_id": 9}
        receipt = await adapter.send_message("private%123", MessageChain([Nodes([Node([Plain("hi")])])]))
        assert receipt.status == "success" and receipt.message_ids == ("9",)
        adapter._bot.send_private_forward_msg.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unsupported_forward_degrades_to_segmented_send(self):
        adapter = self._adapter()
        adapter._bot.send_group_forward_msg.side_effect = ActionFailed({"retcode": 10002})
        adapter._bot.send_group_msg.return_value = {"message_id": 5}
        receipt = await adapter.send_message("group%456", MessageChain([Nodes([Node([Plain("甲")]), Node([Plain("乙")])])]))
        assert receipt.status == "success" and receipt.message_ids == ("5",)
        sent_texts = [seg["data"]["text"] for seg in adapter._bot.send_group_msg.await_args.kwargs["message"]]
        assert sent_texts == ["甲", "乙"]

    @pytest.mark.asyncio
    async def test_business_rejection_is_not_degraded_and_stops_following_turns(self):
        adapter = self._adapter()
        adapter._bot.send_group_forward_msg.side_effect = ActionFailed({"retcode": 1200})
        receipt = await adapter.send_message("group%456", MessageChain([Nodes([Node([Plain("甲")])]), Plain("后续")]))
        assert receipt.status == "failed" and receipt.reason == "action_rejected"
        adapter._bot.send_group_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_forward_without_message_id_is_unknown(self):
        adapter = self._adapter()
        adapter._bot.send_group_forward_msg.return_value = {}
        receipt = await adapter.send_message("group%456", MessageChain([Nodes([Node([Plain("甲")])])]))
        assert receipt.status == "unknown" and receipt.reason == "missing_message_id"

    @pytest.mark.asyncio
    async def test_forward_respects_group_whitelist(self):
        adapter = self._adapter(group_whitelist=["789"])
        receipt = await adapter.send_message("group%456", MessageChain([Nodes([Node([Plain("甲")])])]))
        assert receipt.status == "failed" and receipt.reason == "target_unavailable"
        adapter._bot.send_group_forward_msg.assert_not_awaited()

@pytest.mark.parametrize("value", ["true", 1])
def test_forward_lookup_must_be_boolean(value: object):
    with pytest.raises(ValueError, match="forward_lookup"):
        validate_wake_policy({"forward_lookup": value})

def test_forward_component_keeps_resolved_nodes():
    forward = Forward(id="f", nodes=[Node([Plain("x")])])
    assert forward.nodes is not None and cast(Plain, forward.nodes[0].content[0]).text == "x"
    assert Forward(id="g").nodes is None

@pytest.fixture()
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

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"group_id": 999}, {"self_id": 999}, {"message_id": 88}, {"message_type": "private"}])
async def test_wrong_source_is_rejected_before_send(changes):
    adapter = setup()
    adapter._bot.get_msg.return_value = {**message("group", "456"), **changes}
    service = OneBotForwarding(adapter, _origin(), lambda: None)
    with pytest.raises(ForwardError, match="来源"):
        await service.send(["77"], "merge", {"conversation_kind": "group", "conversation_id": "456"}, {"conversation_kind": "private", "conversation_id": "789"})
    adapter._bot.send_private_forward_msg.assert_not_awaited()

@pytest.mark.usefixtures('cleanup')
@pytest.mark.asyncio
async def test_private_sender_from_another_conversation_does_not_authorize_read():
    adapter = setup()
    payload = message("private", "789")
    adapter._bot.get_msg.return_value = payload
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).source_message({"conversation_kind": "private", "conversation_id": "456"}, "77")

@pytest.mark.usefixtures('cleanup')
@pytest.mark.asyncio
async def test_private_target_does_not_override_contradictory_sender():
    adapter = setup()
    payload = message("private", "456")
    payload["sender"]["user_id"] = 789
    adapter._bot.get_msg.return_value = payload
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).source_message({"conversation_kind": "private", "conversation_id": "456"}, "77")

@pytest.mark.usefixtures('cleanup')
@pytest.mark.asyncio
@pytest.mark.parametrize("segments", [[{"type": "text", "data": {"text": "forward id: original-card"}}], [{"type": "node", "data": {"content": [{"type": "forward", "data": {"id": "original-card"}}]}}]])
async def test_body_or_nested_card_does_not_authorize_top_level_forward(segments):
    adapter = setup()
    adapter._bot.get_msg.return_value = {**message("group", "456"), "message": segments}
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).read("77", {"conversation_kind": "group", "conversation_id": "456"})
    adapter._bot.get_forward_msg.assert_not_awaited()

@pytest.mark.usefixtures('cleanup')
@pytest.mark.asyncio
@pytest.mark.parametrize("contradiction", [{"self_id": 999}, {"group_id": 999}])
async def test_forward_response_cannot_change_proved_account_or_group(contradiction):
    adapter = setup()
    adapter._bot.get_msg.return_value = message("group", "456", forward=True)
    adapter._bot.get_forward_msg.return_value = {"messages": [], **contradiction}
    with pytest.raises(ForwardError):
        await OneBotForwarding(adapter, _origin(), lambda: None).read("77", {"conversation_kind": "group", "conversation_id": "456"})

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
def test_migration_only_requires_missing_forward_plugin_for_explicit_legacy_tools():
    catalog = SimpleNamespace(get=lambda _: None)
    ordinary = [{"name": "group_admin", "capabilities": {"tools": {"group_admin_ban": True}}}]
    assert migrate_forward_tool_specs(ordinary, catalog) == ordinary
    legacy = [{"name": "group_admin", "capabilities": {"tools": {"group_admin_get_forward": False}}}]
    with pytest.raises(ValueError, match="旧转发工具迁移需要可用的 message_forward 插件"):
        migrate_forward_tool_specs(legacy, catalog)

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
def test_new_group_admin_configuration_does_not_install_forward_plugin(tmp_path):
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    for value in [["group_admin"], [{"name": "group_admin", "config": {"write_tools_enabled": True}}]]:
        assert migrate_forward_tool_specs(value, catalog) == value

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
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

@pytest.mark.usefixtures('cleanup')
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
