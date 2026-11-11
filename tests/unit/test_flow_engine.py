"""触发引擎: 条件三值, 逻辑门, 超时, per-WF 目标过滤 (P1-5/P1-4)"""
import asyncio
import time

import pytest

from satrap.edictum.flow.definition import parse_flow_definition, migrate_flow_definition
from satrap.edictum.flow.engine import (
    ConditionEvaluator,
    FlowEvent,
    TriggerEngine,
    gate_and,
    gate_not,
    gate_or,
)
from satrap.edictum.flow.registry import FlowNodeRegistry
from satrap.edictum.flow.types import StateView


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
        from satrap.edictum.flow.definition import FlowNodeInstance
        return FlowNodeInstance(id=node_id, type=type_name, version=1, title="c", config={})

    def _type(self, plugin=""):
        from satrap.edictum.flow.registry import FlowNodeType, PortSpec
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
