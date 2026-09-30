"""合并转发入站解析, 回源投影与出站发送分流"""
from typing import cast
from unittest.mock import AsyncMock

from aiocqhttp.exceptions import ActionFailed
import pytest

from satrap.core.pipeline.input_projection import FORWARD_TEXT_LIMIT, project_input, resolve_forwards
from satrap.core.platform.onebot.onebot_utils import FORWARD_NODE_LIMIT, onebot_segments_to_components, parse_forward_nodes
from satrap.core.platform.onebot.outbound import flatten_forward_nodes, split_forward_turns
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.components import Forward, Image, Node, Nodes, Plain, Reply
from satrap.core.platform.event import MessageChain
from satrap.core.platform import PlatformConfig
from satrap.core.framework.providers import BindingState, BindingStatus


class _RunnableRegistry:
    """绑定判定恒为可运行的会话定义注册表替身"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """
        恒定答复可运行

        返回:
        - BindingStatus: 可运行
        """
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
