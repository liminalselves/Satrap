"""触发引擎: 条件三值, 逻辑门, 超时, per-WF 目标过滤 (P1-5/P1-4)"""
import asyncio
import time

import pytest

from satrap.edictum.flow.definition import parse_flow_definition, migrate_flow_definition
from satrap.edictum.flow.engine import ConditionEvaluator, TriggerEngine, gate_and, gate_not, gate_or
from satrap.core.type.flow.events import FlowEvent
from satrap.edictum.flow.registry import FlowNodeRegistry
from satrap.core.type.flow.ports import StateView


def _def(nodes, edges):
    registry = FlowNodeRegistry()
    definition, _, _ = migrate_flow_definition(
        parse_flow_definition({"version": 3, "nodes": nodes, "edges": edges}), registry)
    return definition, {n.id: registry.get(n.type) for n in definition.nodes.values()}


def _event(kind="message"):
    return FlowEvent(event_id="e1", kind=kind, tile="t1", why="test")


class TestGates:
    """三值逻辑门真值表"""

    @pytest.mark.parametrize("values,expected", [
        ([True, True], True),
        ([True, False], False),
        ([False, "error"], False),       # 假优先于错误
        ([True, "error"], "error"),
        (["error", "error"], "error"),
        ([], True),
    ])
    def test_and(self, values, expected):
        assert gate_and(values) == expected

    @pytest.mark.parametrize("values,expected", [
        ([False, False], False),
        ([True, "error"], True),         # 真优先于错误
        ([False, "error"], "error"),
        ([], False),
    ])
    def test_or(self, values, expected):
        assert gate_or(values) == expected

    @pytest.mark.parametrize("value,expected", [
        (True, False), (False, True), ("error", "error"),
    ])
    def test_not(self, value, expected):
        assert gate_not(value) == expected


class TestConditionEvaluator:
    """条件求值: 插件函数, 超时, 池满, 未知类型"""

    def _node(self, node_id="c1", type_name="cond_time"):
        from satrap.core.type.flow.definition import FlowNodeInstance
        return FlowNodeInstance(id=node_id, type=type_name, version=1, title="c", config={})

    def _type(self, plugin=""):
        from satrap.core.type.flow.registry import FlowNodeType, PortSpec
        return FlowNodeType(
            type="p.cond" if plugin else "cond_time", version=1, label="c",
            description="", role="condition",
            outputs=(PortSpec(name="out", types=("condition",)),),
            plugin=plugin,
        )

    @pytest.mark.asyncio
    async def test_plugin_missing_impl_is_error(self):
        """插件条件没有可用实现 (setup 失败/未加载) 判错误, 不落内置判真"""
        ev = ConditionEvaluator()
        result = await ev.evaluate(self._node(), self._type(plugin="p"), None, {}, _event())
        assert result == "error"

    @pytest.mark.asyncio
    async def test_unknown_builtin_type_is_error(self):
        """未知内置条件类型判错误而不是放行"""
        ev = ConditionEvaluator()
        node = self._node(type_name="cond_no_such")
        result = await ev.evaluate(node, self._type(), None, {}, _event())
        assert result == "error"

    @pytest.mark.asyncio
    async def test_plugin_fn_timeout_is_error(self):
        """超过 150ms 的插件条件判错误, 不等线程跑完"""
        ev = ConditionEvaluator(timeout=0.05)
        started = time.monotonic()

        def slow(ctx):
            time.sleep(1.0)
            return True

        result = await ev.evaluate(self._node(), self._type(plugin="p"), slow, {}, _event())
        elapsed = time.monotonic() - started
        assert result == "error"
        assert elapsed < 0.5, f"超时应立即返回, 实际等了 {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_plugin_fn_exception_is_error(self):
        ev = ConditionEvaluator()

        def boom(ctx):
            raise RuntimeError("x")

        result = await ev.evaluate(self._node(), self._type(plugin="p"), boom, {}, _event())
        assert result == "error"

    @pytest.mark.asyncio
    async def test_calls_callback_uses_real_budget_arguments_and_tri_state(self):
        """ctx.calls 按需调用真实 _calls_used, 不预求值; 真/假都正常返回"""
        from satrap.edictum.flow.budget import FlowCallBudget
        from satrap.edictum.flow.session import AsyncFlowSession

        session = AsyncFlowSession("s", flow={"version": 3, "nodes": [], "edges": []})
        session._budget = FlowCallBudget(max_calls=0)
        session._budget.check_and_spend(source="wf", node_id="w1", event_id="e", run_id="r")
        seen = []

        def within(ctx):
            value = ctx.calls(kind="wf", scope="target", window_seconds=123, target="w1")
            seen.append(value)
            return value >= 1

        evaluator = ConditionEvaluator()
        node, node_type = self._node(), self._type(plugin="p")
        assert await evaluator.evaluate(
            node, node_type, within, {}, _event(), calls_used=session._calls_used) is True
        assert await evaluator.evaluate(
            node, node_type, within, {}, FlowEvent(event_id="e2", kind="message", tile="t1", why="x"),
            calls_used=lambda kind, scope, window_seconds, target: 0) is False
        assert seen == [1, 0]

    @pytest.mark.asyncio
    async def test_default_target_budget_uses_current_wf_and_strictest_target(self):
        """llm_budget 默认配置不传 target, 按本次候选 WF 计用量且多目标取最严格值"""
        from satrap.edictum.flow.budget import FlowCallBudget
        from satrap.expend.plugins.llm_budget.flow_nodes import within

        budget = FlowCallBudget(max_calls=0)
        for node_id, count in (("w1", 1), ("w2", 2)):
            for index in range(count):
                budget.check_and_spend(
                    source="wf", node_id=node_id, event_id=f"e-{node_id}-{index}", run_id="")
        config = {"scope": "target", "count": "wf", "window_minutes": 60, "max_calls": 2}
        evaluator = ConditionEvaluator()
        node, node_type = self._node(), self._type(plugin="llm_budget")
        assert await evaluator.evaluate(
            node, node_type, within, config, _event(), calls_used=budget.calls,
            targets=("w1",)) is True
        assert await evaluator.evaluate(
            node, node_type, within, config,
            FlowEvent(event_id="e2", kind="message", tile="t1", why="x"),
            calls_used=budget.calls, targets=("w1", "w2")) is False
        config["max_calls"] = 1
        assert await evaluator.evaluate(
            node, node_type, within, config,
            FlowEvent(event_id="e3", kind="message", tile="t1", why="x"),
            calls_used=budget.calls, targets=("w1",)) is False

    @pytest.mark.asyncio
    async def test_condition_flow_node_diagnostics_include_result_and_duration(self):
        """条件节点真/假/错误都有 start/end, 耗时与三值结果"""
        events = []
        evaluator = ConditionEvaluator(
            diagnostic=lambda *args, **kwargs: events.append((args, kwargs)))
        node, node_type = self._node(), self._type(plugin="p")
        for index, fn in enumerate((lambda ctx: True, lambda ctx: False, lambda ctx: 1 / 0)):
            result = await evaluator.evaluate(
                node, node_type, fn, {},
                FlowEvent(event_id=f"diag-{index}", kind="message", tile="t1", why="x"))
            assert result == (True if index == 0 else False if index == 1 else "error")
        starts = [item for item in events if item[0][:2] == ("flow_node", "start")]
        ends = [item for item in events if item[0][:2] == ("flow_node", "end")]
        assert len(starts) == len(ends) == 3
        assert [item[1]["metadata"]["result"] for item in ends] == ["true", "false", "error"]
        assert all(item[1]["metadata"]["duration_ms"] >= 0 for item in ends)
        assert [item[1]["status"] for item in ends] == ["completed", "completed", "failed"]

    @pytest.mark.asyncio
    async def test_condition_pool_full_is_error_and_releases_acquire(self):
        """真实 32 槽池满后不执行条件函数, 已 acquire 的实例引用立即归还"""
        import threading

        gate = threading.Event()
        blocker = ConditionEvaluator(timeout=5)
        node, node_type = self._node(), self._type(plugin="p")

        def wait(ctx):
            gate.wait(4)
            return True

        tasks = []
        try:
            for index in range(32):
                tasks.append(asyncio.create_task(blocker.evaluate(
                    node, node_type, wait, {},
                    FlowEvent(event_id=f"fill-{index}", kind="message", tile="t1", why="x"))))
                await asyncio.sleep(0)
            called, released = [], []
            evaluator = ConditionEvaluator(release_ref=lambda node_id, fn: released.append(node_id))
            result = await evaluator.evaluate(
                node, node_type, lambda ctx: called.append(1), {}, _event())
            assert result == "error"
            assert called == [] and released == ["c1"]
        finally:
            gate.set()
            if tasks:
                await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_timeout_worker_release_survives_loop_close_and_runs_once(self):
        """超时工作线程在原 loop 停止并关闭后归还, 不反等 future 且只 release 一次"""
        import threading

        started = threading.Event()
        allow_return = threading.Event()
        evaluation_done = threading.Event()
        released = []
        errors = []
        loop = asyncio.new_event_loop()
        evaluator = ConditionEvaluator(
            timeout=0.05, release_ref=lambda node_id, fn: released.append(node_id))
        node, node_type = self._node(), self._type(plugin="p")

        def condition(ctx):
            started.set()
            allow_return.wait(3)
            return True

        def run_loop():
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(evaluator.evaluate(
                    node, node_type, condition, {}, _event()))
                assert result == "error"
                evaluation_done.set()
                loop.run_forever()
            except BaseException as error:
                errors.append(error)
                evaluation_done.set()

        thread = threading.Thread(target=run_loop)
        thread.start()
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert await asyncio.to_thread(evaluation_done.wait, 2)
            loop.call_soon_threadsafe(loop.stop)
            await asyncio.to_thread(thread.join, 2)
            assert not thread.is_alive() and not errors
            loop.close()
            allow_return.set()
            deadline = time.monotonic() + 2
            while not released and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert released == ["c1"]
        finally:
            allow_return.set()
            if thread.is_alive():
                loop.call_soon_threadsafe(loop.stop)
                await asyncio.to_thread(thread.join, 2)
            if not loop.is_closed():
                loop.close()

    @pytest.mark.asyncio
    async def test_plugin_fn_result_cached_per_event(self):
        """同一事件内同一条件只求值一次"""
        calls = []
        ev = ConditionEvaluator()

        def fn(ctx):
            calls.append(1)
            return True

        n, t = self._node(), self._type(plugin="p")
        assert await ev.evaluate(n, t, fn, {}, _event()) is True
        assert await ev.evaluate(n, t, fn, {}, _event()) is True
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_builtin_cond_time(self):
        ev = ConditionEvaluator()
        node = self._node(type_name="cond_time")
        result = await ev.evaluate(node, self._type(), None,
                                   {"start": "00:00", "end": "24:00"}, _event())
        assert result is True
        # 换事件避免缓存; 直接验证边界: start==end 的区间按实现永远为真
        # (a<=b 分支: a<=minutes<b, 00:00<=m<00:00 永假; a>b 跨零点分支永真)
        node2 = self._node(type_name="cond_time")
        result = await ev.evaluate(node2, self._type(), None,
                                   {"start": "12:00", "end": "12:00"},
                                   FlowEvent(event_id="e2", kind="message", tile="t", why="x"))
        # start==end 走跨零点分支 (a>b 为 False, 走 a<=b), 恒为 False
        assert result is False

    @pytest.mark.asyncio
    async def test_builtin_cond_message_needs_message(self):
        ev = ConditionEvaluator()
        node = self._node(type_name="cond_message")
        result = await ev.evaluate(node, self._type(), None,
                                   {"kind": "keyword", "keywords": "foo"}, _event(kind="batch"))
        assert result is False


class TestTriggerEngineTargets:
    """fire(targets=...) 只触发指定 WF (阈值磁贴的 per-WF 分发)"""

    @pytest.mark.asyncio
    async def test_graph_candidates_drive_default_budget_strictest_target(self):
        """真实触发图推导候选 WF, 默认目标预算按最严格候选阻断。"""
        from satrap.edictum.flow.budget import FlowCallBudget
        from satrap.core.type.flow.registry import FlowNodeType, PortSpec
        from satrap.expend.plugins.llm_budget.flow_nodes import within

        registry = FlowNodeRegistry()
        registry.register_plugin_nodes("llm_budget", [FlowNodeType(
            type="llm_budget.within", version=1, label="budget", description="",
            role="condition", outputs=(PortSpec(name="out", types=("condition",)),),
            plugin="llm_budget")])
        definition, _, _ = migrate_flow_definition(parse_flow_definition({
            "version": 3,
            "nodes": [
                {"id": "t1", "type": "wake", "title": "t", "config": {}},
                {"id": "c1", "type": "llm_budget.within", "title": "budget", "config": {
                    "scope": "target", "count": "wf", "window_minutes": 60, "max_calls": 2}},
                {"id": "g1", "type": "and", "title": "gate", "config": {}},
                {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
                {"id": "w2", "type": "react_workflow", "title": "w2", "config": {}},
                {"id": "unreachable", "type": "react_workflow", "title": "other", "config": {}},
            ],
            "edges": [
                {"from": "t1.event", "to": "g1.a"},
                {"from": "c1.out", "to": "g1.b"},
                {"from": "g1.out", "to": "w1.trigger"},
                {"from": "g1.out", "to": "w2.trigger"},
            ],
        }), registry)
        node_types = {node.id: registry.get(node.type) for node in definition.nodes.values()}
        budget = FlowCallBudget(max_calls=0)
        for wf, count in (("w1", 1), ("w2", 2), ("unreachable", 3)):
            for index in range(count):
                budget.check_and_spend(source="wf", node_id=wf, event_id=f"{wf}-{index}", run_id="")
        queried = []

        def calls_used(**kwargs):
            queried.append(kwargs["target"])
            return budget.calls(**kwargs)

        engine = TriggerEngine(definition, node_types, calls_used=calls_used,
                               resolve_condition=lambda node: within)
        fired = []
        engine.on_trigger(lambda wf, event: fired.append(wf))
        await engine.fire("t1", _event())
        assert queried == ["w1", "w2"]
        assert fired == []  # w1 尚有余量, w2 已达上限

        queried.clear()
        await engine.fire("t1", FlowEvent(event_id="e2", kind="message", tile="t1", why="test"),
                          targets={"w1"})
        assert queried == ["w1"]
        assert fired == ["w1"]

    @pytest.mark.asyncio
    async def test_targets_filter_limits_receivers(self):
        nodes = [
            {"id": "src", "type": "source", "title": "s", "config": {}},
            {"id": "t1", "type": "threshold", "title": "t", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
            {"id": "w2", "type": "react_workflow", "title": "w2", "config": {}},
        ]
        edges = [
            {"from": "src.out", "to": "t1.messages"},
            {"from": "t1.event", "to": "w1.trigger"},
            {"from": "t1.event", "to": "w2.trigger"},
        ]
        definition, node_types = _def(nodes, edges)
        engine = TriggerEngine(definition, node_types)
        fired = []
        engine.on_trigger(lambda w, e: fired.append(w))
        await engine.fire("t1", _event(kind="batch"), targets={"w1"})
        assert fired == ["w1"]

    @pytest.mark.asyncio
    async def test_no_targets_fires_all(self):
        nodes = [
            {"id": "src", "type": "source", "title": "s", "config": {}},
            {"id": "t1", "type": "threshold", "title": "t", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w1", "config": {}},
            {"id": "w2", "type": "react_workflow", "title": "w2", "config": {}},
        ]
        edges = [
            {"from": "src.out", "to": "t1.messages"},
            {"from": "t1.event", "to": "w1.trigger"},
            {"from": "t1.event", "to": "w2.trigger"},
        ]
        definition, node_types = _def(nodes, edges)
        engine = TriggerEngine(definition, node_types)
        fired = []
        engine.on_trigger(lambda w, e: fired.append(w))
        await engine.fire("t1", _event(kind="batch"))
        await asyncio.sleep(0)
        assert sorted(fired) == ["w1", "w2"]

    @pytest.mark.asyncio
    async def test_condition_gate_and_logic(self):
        """AND 门: 触发+条件为真才放行; 条件为假/错误不放行"""
        nodes = [
            {"id": "t1", "type": "wake", "title": "t", "config": {}},
            {"id": "c1", "type": "cond_time", "title": "c", "config": {"start": "00:00", "end": "00:00"}},
            {"id": "g1", "type": "and", "title": "g", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
        ]
        edges = [
            {"from": "t1.event", "to": "g1.a"},
            {"from": "c1.out", "to": "g1.b"},
            {"from": "g1.out", "to": "w1.trigger"},
        ]
        definition, node_types = _def(nodes, edges)
        engine = TriggerEngine(definition, node_types)
        fired = []
        engine.on_trigger(lambda w, e: fired.append(w))
        await engine.fire("t1", _event())
        # cond_time 00:00-00:00 为假 -> 不放行
        assert fired == []

    @pytest.mark.asyncio
    async def test_cached_condition_does_not_acquire_again(self):
        """同一事件经 when 与逻辑门复用条件, 第二次不再 acquire"""
        from satrap.core.type.flow.registry import FlowNodeType, PortSpec
        registry = FlowNodeRegistry()
        registry.register_plugin_nodes("p", [FlowNodeType(
            type="p.ok", version=1, label="ok", description="", role="condition",
            outputs=(PortSpec(name="out", types=("condition",)),), plugin="p")])
        definition, _, _ = migrate_flow_definition(parse_flow_definition({
            "version": 3,
            "nodes": [
                {"id": "t1", "type": "wake", "title": "t", "config": {}},
                {"id": "c1", "type": "p.ok", "title": "c", "config": {}},
                {"id": "g1", "type": "and", "title": "g", "config": {}},
                {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
            ],
            "edges": [
                {"from": "c1.out", "to": "t1.when"}, {"from": "t1.event", "to": "g1.a"},
                {"from": "c1.out", "to": "g1.b"}, {"from": "g1.out", "to": "w1.trigger"},
            ],
        }), registry)
        node_types = {node.id: registry.get(node.type) for node in definition.nodes.values()}
        acquired, released, fired = [], [], []

        def condition(ctx):
            return True

        def acquire(node):
            acquired.append(node.id)
            return condition

        engine = TriggerEngine(
            definition, node_types, resolve_condition=acquire,
            release_ref=lambda node_id, fn: released.append(node_id))
        engine.on_trigger(lambda wf_id, event: fired.append(wf_id))
        event = _event()
        await engine.fire("t1", event)
        assert acquired == ["c1"] and released == ["c1"] and fired == ["w1"]
        await engine.finish("w1", event)
        assert not engine._evaluator._cache and not engine._event_refs

    @pytest.mark.asyncio
    async def test_when_port_blocks_tile(self):
        """磁贴 when 条件为假时不发事件"""
        nodes = [
            {"id": "t1", "type": "wake", "title": "t", "config": {}},
            {"id": "c1", "type": "cond_time", "title": "c", "config": {"start": "00:00", "end": "00:00"}},
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
        ]
        edges = [
            {"from": "c1.out", "to": "t1.when"},
            {"from": "t1.event", "to": "w1.trigger"},
        ]
        definition, node_types = _def(nodes, edges)
        engine = TriggerEngine(definition, node_types)
        fired = []
        engine.on_trigger(lambda w, e: fired.append(w))
        await engine.fire("t1", _event())
        assert fired == []

    @pytest.mark.asyncio
    async def test_busy_queue_drop_merge(self):
        """on_busy: 忙碌时 queue 排队, drop 丢弃"""
        nodes = [
            {"id": "t1", "type": "wake", "title": "t", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {"on_busy": "queue"}},
        ]
        edges = [{"from": "t1.event", "to": "w1.trigger"}]
        definition, node_types = _def(nodes, edges)
        engine = TriggerEngine(definition, node_types)
        gate = asyncio.Event()
        fired = []

        async def handler(w, e):
            fired.append(e.event_id)
            await gate.wait()

        engine.on_trigger(handler)
        await engine.fire("t1", FlowEvent(event_id="e1", kind="message", tile="t1", why="1"))
        await engine.fire("t1", FlowEvent(event_id="e2", kind="message", tile="t1", why="2"))
        await asyncio.sleep(0)
        # e1 在跑, e2 排队
        assert fired == ["e1"]
        gate.set()
        await asyncio.sleep(0.05)
        # 模拟运行结束: 手动 finish 应取出 e2
        next_event = await engine.finish("w1", FlowEvent(event_id="e1", kind="message", tile="t1", why="1"))
        assert next_event is not None and next_event.event_id == "e2"

    @pytest.mark.asyncio
    async def test_merge_transfers_event_completion_waiter(self):
        """被 merge 替换的旧事件等到最新合并事件完成, 不提前结束"""
        nodes = [
            {"id": "t1", "type": "wake", "title": "t", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {"on_busy": "merge"}},
        ]
        definition, node_types = _def(nodes, [{"from": "t1.event", "to": "w1.trigger"}])
        engine = TriggerEngine(definition, node_types)
        gate = asyncio.Event()

        async def handler(w, event):
            await gate.wait()

        engine.on_trigger(handler)
        events = [FlowEvent(event_id=f"e{index}", kind="message", tile="t1", why="test") for index in range(1, 4)]
        for event in events:
            await engine.fire("t1", event)
        await asyncio.sleep(0)
        waiter = asyncio.create_task(engine.wait_for_events(("e2",)))
        await asyncio.sleep(0)
        assert not waiter.done()
        gate.set()
        next_event = await engine.finish("w1", events[0])
        assert next_event is not None and next_event.event_id == "e3"
        assert not waiter.done()
        await engine.finish("w1", events[2])
        await asyncio.wait_for(waiter, 1)
        assert engine._event_aliases == {}
        await engine.shutdown()

    @pytest.mark.asyncio
    async def test_cancel_before_handler_start_releases_event_waiter(self):
        """WF 协程尚未启动就取消时, 引擎仍释放事件并结束等待"""
        nodes = [
            {"id": "t1", "type": "wake", "title": "t", "config": {}},
            {"id": "w1", "type": "react_workflow", "title": "w", "config": {}},
        ]
        definition, node_types = _def(nodes, [{"from": "t1.event", "to": "w1.trigger"}])
        engine = TriggerEngine(definition, node_types)

        async def handler(w, event):
            raise AssertionError("取消前不应进入 handler")

        engine.on_trigger(handler)
        event = FlowEvent(event_id="e1", kind="message", tile="t1", why="test")
        await engine.fire("t1", event)
        waiter = asyncio.create_task(engine.wait_for_events(("e1",)))
        await engine.cancel_workflow("w1")
        await asyncio.wait_for(waiter, 1)
        assert engine._event_refs == {}
        await engine.shutdown()
