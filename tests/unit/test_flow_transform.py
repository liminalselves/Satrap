"""transform 求值: source 供给, 共享求值, 等待者取消, 超时 (P1-7)"""
import asyncio
import time

import pytest

from satrap.edictum.flow.definition import parse_flow_definition, migrate_flow_definition
from satrap.edictum.flow.engine import FlowEvent
from satrap.edictum.flow.registry import FlowNodeRegistry
from satrap.edictum.flow.transform import TransformEvaluator
from satrap.edictum.flow.types import FlowMessage, StateView


def _def(nodes, edges):
    registry = FlowNodeRegistry()
    definition, _, _ = migrate_flow_definition(
        parse_flow_definition({"version": 3, "nodes": nodes, "edges": edges}), registry)
    return definition, {n.id: registry.get(n.type) for n in definition.nodes.values()}


def _event():
    return FlowEvent(event_id="e1", kind="batch", tile="t1", why="x", batch_from=0, batch_to=3)


def _msg(i, text="t"):
    return FlowMessage(message_id=f"m{i}", position=i, sender_id="u",
                       nickname="", text=f"{text}{i}", is_bot=False,
                       mentions_bot=False, time=1.0)


class TestSourceAndBucket:
    @pytest.mark.asyncio
    async def test_source_feeds_bucket_assemble(self):
        """source → bucket → assemble 全链路可拉取"""
        nodes = [
            {"id": "src", "type": "source", "title": "s", "config": {}},
            {"id": "b1", "type": "bucket", "title": "b", "config": {"include_outbound": False}},
            {"id": "asm", "type": "assemble", "title": "a", "config": {"latest_n": 2, "after_coverage": False}},
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
        ]
        edges = [
            {"from": "src.out", "to": "b1.in"},
            {"from": "b1.out", "to": "asm.messages"},
            {"from": "asm.out", "to": "w1.input"},
        ]
        definition, node_types = _def(nodes, edges)
        supply = (
            _msg(1), _msg(2, "x"), _msg(3, "x"),
            FlowMessage(message_id="o1", position=4, sender_id="bot", nickname="",
                        text="bot", is_bot=True, mentions_bot=False, time=1.0),
        )
        ev = TransformEvaluator(definition, node_types, resolve_source=lambda: supply)
        out = await ev.pull("w1", "asm", "r1", _event())
        # assemble 输出 ContextBlock 元组
        texts = [b.text for b in out]
        joined = "\n".join(texts)
        # include_outbound=False 滤掉机器人, after_coverage=False 不走覆盖,
        # latest_n=2 取最后 2 条 (x2, x3)
        assert "bot" not in joined
        assert "x3" in joined and "x2" in joined

    @pytest.mark.asyncio
    async def test_only_assemble_source_path_uses_bounded_query(self):
        """source 直连 WF 保留全量; source→assemble 才传明确的有界窗口"""
        nodes = [
            {"id": "src", "type": "source", "title": "s", "config": {}},
            {"id": "asm", "type": "assemble", "title": "a",
             "config": {"latest_n": 1, "after_coverage": True, "max_pending": 2}},
            {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
            {"id": "w2", "type": "react_workflow", "title": "w2", "config": {}},
        ]
        edges = [
            {"from": "src.out", "to": "w1.input"},
            {"from": "src.out", "to": "asm.messages"},
            {"from": "asm.out", "to": "w2.input"},
        ]
        definition, node_types = _def(nodes, edges)
        calls = []

        def source(*args):
            calls.append(args)
            return (_msg(1), _msg(2), _msg(3))

        ev = TransformEvaluator(definition, node_types, resolve_source=source)
        assert len(await ev.pull("w1", "src", "r1", _event())) == 3
        await ev.pull("w2", "asm", "r2", _event())
        assert calls[0] == ()
        assert calls[1] == ({"after": 0, "pending_limit": 2, "latest_n": 1},)

    @pytest.mark.asyncio
    async def test_bucket_filters_outbound(self):
        nodes = [
            {"id": "b1", "type": "bucket", "title": "b", "config": {"include_outbound": False}},
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
        ]
        edges = [{"from": "b1.out", "to": "w1.input"}]
        definition, node_types = _def(nodes, edges)
        supply = (
            _msg(1),
            FlowMessage(message_id="o", position=2, sender_id="bot", nickname="",
                        text="bot", is_bot=True, mentions_bot=False, time=1.0),
        )
        ev = TransformEvaluator(definition, node_types, resolve_source=lambda: supply)
        # bucket 输入没连线 -> in 口必填但没有 source; pull(b1) 直接对 bucket 求值,
        # bucket 内部 _pull_input(in) 没线返回 None -> () 空
        out = await ev.pull("w1", "b1", "r1", _event())
        assert out == ()


class TestSharing:
    @pytest.mark.asyncio
    async def test_shared_eval_once_per_event(self):
        """同一事件内两个 WF 拉同一节点只求值一次"""
        nodes = [
            {"id": "p1", "type": "plug.fn", "title": "p", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
            {"id": "w2", "type": "react_workflow", "title": "w2", "config": {}},
        ]
        edges = [{"from": "p1.out", "to": "w1.input"}, {"from": "p1.out", "to": "w2.input"}]
        registry = FlowNodeRegistry()
        from satrap.edictum.flow.registry import FlowNodeType, PortSpec
        registry.register_plugin_nodes("plug", [FlowNodeType(
            type="plug.fn", version=1, label="fn", description="", role="transform",
            inputs=(PortSpec(name="in", types=("messages",), required=False),),
            outputs=(PortSpec(name="out", types=("text",)),),
            plugin="plug",
        )])
        definition, _, _ = migrate_flow_definition(
            parse_flow_definition({"version": 3, "nodes": nodes, "edges": edges}), registry)
        node_types = {n.id: registry.get(n.type) for n in definition.nodes.values()}
        calls = []
        def fn(ctx, in_=None):
            calls.append(1)
            return "value"
        ev = TransformEvaluator(definition, node_types,
                                resolve_transform=lambda node, run_id: fn)
        e = _event()
        a, b = await asyncio.gather(ev.pull("w1", "p1", "r1", e), ev.pull("w2", "p1", "r2", e))
        assert a == b == "value"
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_error_propagates_to_all_waiters(self):
        nodes = [
            {"id": "p1", "type": "plug.fn", "title": "p", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
            {"id": "w2", "type": "react_workflow", "title": "w2", "config": {}},
        ]
        edges = [{"from": "p1.out", "to": "w1.input"}, {"from": "p1.out", "to": "w2.input"}]
        registry = FlowNodeRegistry()
        from satrap.edictum.flow.registry import FlowNodeType, PortSpec
        registry.register_plugin_nodes("plug", [FlowNodeType(
            type="plug.fn", version=1, label="fn", description="", role="transform",
            outputs=(PortSpec(name="out", types=("text",)),), plugin="plug",
        )])
        definition, _, _ = migrate_flow_definition(
            parse_flow_definition({"version": 3, "nodes": nodes, "edges": edges}), registry)
        node_types = {n.id: registry.get(n.type) for n in definition.nodes.values()}
        def fn(ctx):
            raise RuntimeError("boom")
        ev = TransformEvaluator(definition, node_types,
                                resolve_transform=lambda node, run_id: fn)
        e = _event()
        with pytest.raises(RuntimeError):
            await asyncio.gather(ev.pull("w1", "p1", "r1", e), ev.pull("w2", "p1", "r2", e))

    @pytest.mark.asyncio
    async def test_state_node_shortcircuits(self):
        nodes = [
            {"id": "s1", "type": "state", "title": "s", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
        ]
        edges = [{"from": "s1.read", "to": "w1.input"}]
        definition, node_types = _def(nodes, edges)
        ev = TransformEvaluator(definition, node_types,
                                resolve_state=lambda sid: StateView(content="v1", version=3,
                                                                    covers_to=9, committed_at=1.0))
        out = await ev.pull("w1", "s1", "r1", _event())
        assert out.content == "v1" and out.version == 3 and out.covers_to == 9


class TestTimeout:
    @pytest.mark.asyncio
    async def test_per_waiter_timeout(self):
        """等待者按自己剩余时间等; 短的先超时移出, cell 对长等待者继续"""
        nodes = [
            {"id": "p1", "type": "plug.fn", "title": "p", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
            {"id": "w2", "type": "react_workflow", "title": "w2", "config": {}},
        ]
        edges = [{"from": "p1.out", "to": "w1.input"}, {"from": "p1.out", "to": "w2.input"}]
        registry = FlowNodeRegistry()
        from satrap.edictum.flow.registry import FlowNodeType, PortSpec
        registry.register_plugin_nodes("plug", [FlowNodeType(
            type="plug.fn", version=1, label="fn", description="", role="transform",
            outputs=(PortSpec(name="out", types=("text",)),), plugin="plug",
        )])
        definition, _, _ = migrate_flow_definition(
            parse_flow_definition({"version": 3, "nodes": nodes, "edges": edges}), registry)
        node_types = {n.id: registry.get(n.type) for n in definition.nodes.values()}

        async def slow(ctx):
            await asyncio.sleep(0.35)
            return "done"

        ev = TransformEvaluator(definition, node_types,
                                resolve_transform=lambda node, run_id: slow, timeout=0.5)
        e = _event()
        # w1 只等 60ms -> 超时; w2 等 400ms -> 拿到结果
        t1 = asyncio.create_task(ev.pull("w1", "p1", "r1", e, timeout=0.06))
        t2 = asyncio.create_task(ev.pull("w2", "p1", "r2", e, timeout=0.4))
        with pytest.raises(asyncio.TimeoutError):
            await t1
        assert await t2 == "done"


class TestLifecycle:
    @staticmethod
    def _async_node():
        from satrap.edictum.flow.registry import FlowNodeType, PortSpec
        nodes = [
            {"id": "p1", "type": "plug.fn", "title": "p", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
            {"id": "w2", "type": "react_workflow", "title": "w2", "config": {}},
        ]
        edges = [{"from": "p1.out", "to": "w1.input"}, {"from": "p1.out", "to": "w2.input"}]
        registry = FlowNodeRegistry()
        registry.register_plugin_nodes("plug", [FlowNodeType(
            type="plug.fn", version=1, label="fn", description="", role="transform",
            outputs=(PortSpec(name="out", types=("text",)),), plugin="plug")])
        definition, _, _ = migrate_flow_definition(
            parse_flow_definition({"version": 3, "nodes": nodes, "edges": edges}), registry)
        return definition, {node.id: registry.get(node.type) for node in definition.nodes.values()}

    @pytest.mark.asyncio
    async def test_computation_cancelled_only_after_all_waiters_cancel(self):
        """取消一个等待者不打断共享计算, 最后一个取消才取消任务"""
        definition, node_types = self._async_node()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow(ctx):
            started.set()
            try:
                await asyncio.sleep(10)
                return "done"
            finally:
                if asyncio.current_task().cancelling():
                    cancelled.set()

        evaluator = TransformEvaluator(definition, node_types, resolve_transform=lambda node, run_id: slow)
        event = _event()
        first = asyncio.create_task(evaluator.pull("w1", "p1", "r1", event))
        second = asyncio.create_task(evaluator.pull("w2", "p1", "r2", event))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0)
        assert not cancelled.is_set() and not evaluator._cells[(event.event_id, "p1")].task.done()
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await asyncio.wait_for(cancelled.wait(), 1)

    @pytest.mark.asyncio
    async def test_argument_failure_releases_acquired_instance(self):
        """acquire 后上游参数拉取异常也归还实例引用"""
        from satrap.edictum.flow.registry import FlowNodeType, PortSpec
        nodes = [
            {"id": "up", "type": "plug.up", "title": "up", "config": {}},
            {"id": "p1", "type": "plug.fn", "title": "p", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
        ]
        edges = [{"from": "up.out", "to": "p1.in"}, {"from": "p1.out", "to": "w1.input"}]
        registry = FlowNodeRegistry()
        registry.register_plugin_nodes("plug", [
            FlowNodeType(type="plug.up", version=1, label="up", description="", role="transform",
                         outputs=(PortSpec(name="out", types=("text",)),), plugin="plug"),
            FlowNodeType(type="plug.fn", version=1, label="fn", description="", role="transform",
                         inputs=(PortSpec(name="in", types=("text",), required=True),),
                         outputs=(PortSpec(name="out", types=("text",)),), plugin="plug"),
        ])
        definition, _, _ = migrate_flow_definition(
            parse_flow_definition({"version": 3, "nodes": nodes, "edges": edges}), registry)
        node_types = {node.id: registry.get(node.type) for node in definition.nodes.values()}
        released = []

        def fail(ctx):
            raise RuntimeError("upstream")

        evaluator = TransformEvaluator(
            definition, node_types,
            resolve_transform=lambda node, run_id: fail if node.id == "up" else lambda ctx, in_: in_,
            release_ref=lambda node_id, fn: released.append(node_id))
        with pytest.raises(RuntimeError, match="upstream"):
            await evaluator.pull("w1", "p1", "r1", _event())
        assert sorted(released) == ["p1", "up"]

    @pytest.mark.asyncio
    async def test_completed_cell_survives_first_run_until_event_forget(self):
        """首个 WF 完成不删共享结果, 事件 forget 后才清缓存"""
        definition, node_types = self._async_node()
        calls = []

        async def compute(ctx):
            calls.append(1)
            return "value"

        evaluator = TransformEvaluator(definition, node_types, resolve_transform=lambda node, run_id: compute)
        event = _event()
        assert await evaluator.pull("w1", "p1", "r1", event) == "value"
        evaluator.forget_run("r1")
        assert await evaluator.pull("w2", "p1", "r2", event) == "value"
        assert calls == [1]
        evaluator.forget(event.event_id)
        assert evaluator._cells == {}
