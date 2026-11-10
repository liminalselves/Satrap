"""
流程触发引擎: 事件, 条件三值, 逻辑门, on_busy 策略, 完成与交接

以触发事件为边界传播: 磁贴 -> (AND 时间窗 | AND/OR 条件门 | OR 直通) -> WF.trigger;
条件节点按三值求值 (真/假/错误), 插件条件函数走 FLOW_CONDITION_WORKERS 有界线程池
"""
from __future__ import annotations

from satrap.core.log.debug.hooks import record_debug
from satrap.core.utils.async_worker import WorkerBusyError
from satrap.edictum.flow.definition import FlowDefinition, FlowEdge, FlowNodeInstance
from satrap.edictum.flow.registry import FlowNodeType
from satrap.edictum.flow.types import FLOW_CONDITION_WORKERS, FlowMessage, StateView

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from satrap.core.log import logger


ConditionResult = Literal[True, False, "error"]
"""条件求值三值"""


@dataclass(frozen=True)
class FlowEvent:
    """一次触发事件"""

    event_id: str
    kind: Literal["message", "batch", "call"]
    """message: 唤醒的单条消息; batch: 阈值批次; call: 交接工具或完成事件"""
    tile: str
    """发出事件的磁贴/工具节点 id"""
    why: str
    message: FlowMessage | None = None
    """kind=message 时的触发消息"""
    batch: tuple[FlowMessage, ...] | None = None
    """kind=batch 时的批次消息"""
    batch_from: int = 0
    batch_to: int = 0
    payload: str = ""
    """kind=call 时交接工具的参数或上游输出"""
    source_wf: str = ""
    """kind=call 时调用方 WF 节点 id"""
    occurred_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class EventContext:
    """条件函数拿到的只读上下文"""

    kind: str
    now: float
    """UTC 时间戳"""
    message: FlowMessage | None = None
    batch: tuple[FlowMessage, ...] | None = None
    calls_used: int = 0
    """由宿主填充的调用计数, 条件用 ctx.calls() 读取"""


class ConditionEvaluator:
    """条件节点求值器: 内置直接调, 插件函数进有界线程池, 超时/异常/池满判错误"""

    def __init__(
        self,
        timeout: float = 0.15,
        *,
        release_ref: Callable[[str, Any], Any] | None = None,
    ) -> None:
        """
        初始化求值器

        参数:
        - timeout: 条件函数限时, 默认 150 毫秒
        - release_ref: 归还实例引用回调 (node_id, instance); 来自 FlowNodeInstancePool
        """
        self.timeout = timeout
        self._release_ref = release_ref
        self._cache: dict[tuple[str, str], bool | str] = {}
        """(event_id, 节点 id) -> 结果缓存; 每个事件对每个条件节点最多求值一次"""

    async def evaluate(
        self,
        node: FlowNodeInstance,
        node_type: FlowNodeType,
        fn: Callable[..., Any] | None,
        config: dict[str, Any],
        event: FlowEvent,
        state_view: StateView | None = None,
        calls_used: int = 0,
    ) -> bool | str:
        """
        对一个条件节点求值, 结果在同一事件内复用

        参数:
        - node: 条件节点实例
        - node_type: 节点契约
        - fn: 插件条件函数 (内置节点为 None)
        - config: 生效配置
        - event: 当前触发事件
        - state_view: state 输入连到的只读视图 (cond_state 用)
        - calls_used: 宿主调用计数 (llm_budget 类条件用)

        返回:
        - True / False / "error"; 每个事件最多求值一次, 结果在事件内复用
        """
        key = (event.event_id, node.id)
        if key in self._cache:
            return self._cache[key]
        result = await self._evaluate_once(node, node_type, fn, config, event, state_view, calls_used)
        self._cache[key] = result
        return result

    async def _evaluate_once(
        self, node: FlowNodeInstance, node_type: FlowNodeType,
        fn: Callable[..., Any] | None, config: dict[str, Any],
        event: FlowEvent, state_view: StateView | None, calls_used: int,
    ) -> bool | str:
        """实际求值一次; 插件条件进线程池, 内置在事件循环里"""
        if node_type.plugin and fn is not None:
            # 插件条件函数: 同步函数进有界线程池, 超时/池满/异常判错误
            try:
                ctx = self._build_ctx(node_type, config, event, state_view, calls_used)
                args = self._cond_args(node_type, ctx, state_view)
                return await asyncio.wait_for(
                    FLOW_CONDITION_WORKERS.run(fn, *args),
                    timeout=self.timeout,
                )
            except (WorkerBusyError, asyncio.TimeoutError, Exception) as error:
                record_debug("flow_trigger", "blocked", bodies={"output": {
                    "node": node.id, "reason": type(error).__name__}})
                logger.warning(f"[流程引擎] 条件节点 {node.id} 求值失败 ({type(error).__name__}), 判为错误")
                return "error"
            finally:
                if self._release_ref is not None:
                    result = self._release_ref(node.id, fn)
                    if inspect.isawaitable(result):
                        await result
        # 内置条件节点直接在事件循环求值
        try:
            return self._builtin(node, node_type, config, event, state_view)
        except Exception as error:
            logger.warning(f"[流程引擎] 内置条件 {node.id} 求值异常: {error}")
            return "error"

    def _build_ctx(
        self, node_type: FlowNodeType, config: dict[str, Any],
        event: FlowEvent, state_view: StateView | None, calls_used: int,
    ) -> Any:
        """组装条件函数的只读 ctx"""
        from types import SimpleNamespace
        ns = SimpleNamespace(
            config=config,
            now=time.time(),
            kind=event.kind,
            message=event.message,
            batch=event.batch,
            calls=lambda kind, scope, window_seconds: calls_used,
            local_time_between=lambda start, end: _local_time_between(start, end, ns.now),
        )
        return ns

    @staticmethod
    def _cond_args(node_type: FlowNodeType, ctx: Any, state_view: StateView | None) -> tuple[Any, ...]:
        """按输入端口顺序组装调用参数; ctx 固定第一, state 输入给 StateView"""
        args: list[Any] = [ctx]
        for port in node_type.inputs:
            if port.name == "when":
                continue
            if port.types == ("state",):
                args.append(state_view if state_view is not None else StateView.empty())
            else:
                args.append(None)
        return tuple(args)

    def _builtin(
        self, node: FlowNodeInstance, node_type: FlowNodeType,
        config: dict[str, Any], event: FlowEvent, state_view: StateView | None,
    ) -> bool | str:
        """内置条件节点求值"""
        if node.type == "cond_message":
            msg = event.message
            if msg is None:
                return False   # 没有单条消息的触发里恒为假
            kind = str(config.get("kind") or "mention")
            if kind == "mention":
                return msg.mentions_bot
            if kind == "keyword":
                keywords = [x.strip() for x in str(config.get("keywords") or "").splitlines() if x.strip()]
                return any(k in msg.text for k in keywords) if keywords else False
            if kind == "sender":
                senders = [x.strip() for x in str(config.get("senders") or "").splitlines() if x.strip()]
                return msg.sender_id in senders or msg.nickname in senders
            return False
        if node.type == "cond_time":
            return _local_time_between(str(config.get("start") or "00:00"),
                                       str(config.get("end") or "24:00"), time.time())
        if node.type == "cond_state":
            if state_view is None:
                return "error"   # 状态槽不可用时判错误
            check = str(config.get("check") or "nonempty")
            if check == "nonempty":
                return bool(state_view.content)
            return state_view.version >= int(config.get("min_version") or 1)
        return True

    def forget(self, event_id: str) -> None:
        """事件结束时清理该事件的缓存"""
        for key in [k for k in self._cache if k[0] == event_id]:
            del self._cache[key]


def _local_time_between(start: str, end: str, now: float) -> bool:
    """
    按本机时区判断当前时刻是否在 [start, end) 内; 结束早于开始表示跨零点

    参数:
    - start: HH:MM
    - end: HH:MM
    - now: UTC 时间戳

    返回:
    - 在时段内为 True
    """
    def _minutes(text: str) -> int:
        hh, mm = text.split(":")
        return int(hh) * 60 + int(mm)
    a, b = _minutes(start), _minutes(end)
    local = time.localtime(now)
    minutes = local.tm_hour * 60 + local.tm_min
    if a <= b:
        return a <= minutes < b
    return minutes >= a or minutes < b


# ---------- 逻辑门三值运算 ----------

def gate_not(value: bool | str) -> bool | str:
    """NOT: 真->假, 假->真, 错误->错误"""
    if value == "error":
        return "error"
    return not value


def gate_and(values: list[bool | str]) -> bool | str:
    """AND: 有任一假->假; 否则有错误->错误; 否则->真"""
    if any(v is False for v in values):
        return False
    if any(v == "error" for v in values):
        return "error"
    return True


def gate_or(values: list[bool | str]) -> bool | str:
    """OR: 有任一真->真; 否则有错误->错误; 否则->假"""
    if any(v is True for v in values):
        return True
    if any(v == "error" for v in values):
        return "error"
    return False


# ---------- 触发引擎 ----------

@dataclass
class _WfState:
    """一个 WF 的运行队列状态"""

    busy: bool = False
    queued: list[FlowEvent] = field(default_factory=list)
    merged: FlowEvent | None = None


class TriggerEngine:
    """
    流程触发引擎: 管理事件传播, 逻辑门求值, WF 调度与 on_busy 策略

    以触发事件为边界: 磁贴发出的事件经逻辑门到达 WF.trigger 即调 trigger(wf, event)
    """

    def __init__(
        self,
        definition: FlowDefinition,
        node_types: dict[str, FlowNodeType],
        *,
        evaluate_condition: Callable[..., Any] | None = None,
        resolve_state: Callable[[str], StateView] | None = None,
        calls_used: Callable[[str], int] | None = None,
        resolve_condition: Callable[[FlowNodeInstance], Callable[..., Any] | None] | None = None,
        release_ref: Callable[[str, Any], Any] | None = None,
    ) -> None:
        """
        初始化引擎

        参数:
        - definition: 已迁移的流程定义
        - node_types: {节点 id: 节点契约}
        - evaluate_condition: 插件条件求值回调 (async), 缺省用内置 ConditionEvaluator
        - resolve_state: state 槽读取回调 {state_node_id: StateView}
        - calls_used: 调用计数回调 {wf_id: int}
        - resolve_condition: 插件条件实现解析回调 {node: fn}; 来自 FlowNodeInstancePool.acquire
        - release_ref: 归还实例引用回调 {node_id, instance}; 与 resolve_condition 成对
        """
        self.definition = definition
        self.node_types = node_types
        self._resolve_state = resolve_state or (lambda state_id: StateView.empty())
        self._calls_used = calls_used or (lambda wf_id: 0)
        self._resolve_condition = resolve_condition or (lambda node: None)
        self._release_ref = release_ref
        self._evaluator = evaluate_condition or ConditionEvaluator(release_ref=release_ref)
        self._wf: dict[str, _WfState] = {n.id: _WfState() for n in definition.nodes.values()
                                         if (t := node_types.get(n.id)) and t.role == "workflow"}
        self._handlers: list[Callable[[str, FlowEvent], Any]] = []
        self._arrivals: dict[str, dict[str, float]] = {}
        """AND 门时间窗: {gate_id: {edge_key: 到达时刻}}"""
        self._diag = record_debug

    def on_trigger(self, handler: Callable[[str, FlowEvent], Any]) -> None:
        """登记 WF 触发回调 handler(wf_id, event)"""
        self._handlers.append(handler)

    # ---------- 对外入口 ----------

    async def fire(self, tile_id: str, event: FlowEvent) -> None:
        """
        让一个磁贴/工具节点发出事件, 经 when 条件和逻辑门传播

        参数:
        - tile_id: 磁贴节点 id
        - event: 触发事件
        """
        node = self.definition.nodes.get(tile_id)
        node_type = self.node_types.get(tile_id)
        if node is None or node_type is None or node_type.role != "trigger":
            return
        if not await self._when_ok(tile_id, event):
            self._diag("flow_trigger", "blocked", bodies={"output": {"tile": tile_id, "reason": "when_false"}})
            return
        await self._emit(tile_id, "event", event, depth=0)

    async def complete(self, wf_id: str, event: FlowEvent, output: str) -> None:
        """
        WF 完成后发 done 事件给下游

        参数:
        - wf_id: 完成的 WF 节点 id
        - event: 驱动它的原始事件 (沿用 event_id 传播)
        - output: WF 输出文本
        """
        done = FlowEvent(
            event_id=event.event_id, kind="call", tile=wf_id,
            why=f"{self._title(wf_id)} 完成", payload=output, source_wf=wf_id,
        )
        await self._emit(wf_id, "done", done, depth=0)

    async def handoff(self, tool_tile: str, event: FlowEvent) -> bool:
        """
        交接工具被调用: 检查 when 条件, 满足则发出 call 事件

        参数:
        - tool_tile: 交接工具磁贴节点 id
        - event: 调用方 WF 带来的 call 事件

        返回:
        - True 表示已交给下游 WF; False 表示条件为假未触发
        """
        if not await self._when_ok(tool_tile, event):
            self._diag("flow_trigger", "blocked", bodies={"output": {"tile": tool_tile, "reason": "when_false"}})
            return False
        await self._emit(tool_tile, "event", event, depth=0)
        return True

    # ---------- 事件传播 ----------

    async def _emit(self, source: str, port: str, event: FlowEvent, depth: int) -> None:
        """把事件沿 source.port 的输出边发出; 深度上限 20 防环"""
        if depth > 20:
            logger.error("[流程引擎] 事件传播超过 20 层, 已停止")
            return
        for e in self.definition.edges:
            if e.source != source or e.source_port != port:
                continue
            target_node = self.definition.nodes.get(e.target)
            target_type = self.node_types.get(e.target)
            if target_node is None or target_type is None:
                continue
            if target_type.role == "workflow" and e.target_port == "trigger":
                await self._trigger(e.target, event)
            elif target_type.type == "or":
                # OR 直通: 任一输入都放行 (条件是条件, 事件是事件)
                await self._emit(e.target, "out", event, depth + 1)
            elif target_type.type == "and":
                await self._gate_and(e.target, e, event, depth)

    async def _gate_and(self, gate_id: str, edge: FlowEdge, event: FlowEvent, depth: int) -> None:
        """AND 门: 多触发按时间窗等待; 触发+条件按三值求值"""
        gate_node = self.definition.nodes[gate_id]
        in_edges = [e for e in self.definition.edges if e.target == gate_id]
        gate_type = self.node_types[gate_id]
        event_edges = [e for e in in_edges if self._edge_type(e) == "event"]
        cond_edges = [e for e in in_edges if self._edge_type(e) != "event"]

        # 多触发输入: 先在时间窗内都到达
        if len(event_edges) > 1:
            window = float(self._config(gate_id).get("window_seconds") or 120)
            arrivals = self._arrivals.setdefault(gate_id, {})
            arrivals[_edge_key(edge)] = event.occurred_at
            if not all(arrivals.get(_edge_key(e)) is not None
                       and event.occurred_at - arrivals[_edge_key(e)] <= window
                       for e in event_edges):
                self._diag("flow_trigger", "blocked", bodies={"output": {
                    "gate": gate_id, "reason": "waiting", "window": window,
                    "arrived": len(arrivals), "expected": len(event_edges)}})
                return
            self._arrivals[gate_id] = {}

        # 条件输入按三值求值
        values: list[bool | str] = []
        for e in cond_edges:
            source_node = self.definition.nodes[e.source]
            source_type = self.node_types[e.source]
            values.append(await self._eval_condition(source_node, source_type, event))
        verdict = gate_and(values)
        if verdict == "error":
            self._diag("flow_trigger", "blocked", bodies={"output": {
                "gate": gate_id, "reason": "condition_error", "edge": _edge_key(edge)}})
            return
        if verdict is False:
            self._diag("flow_trigger", "blocked", bodies={"output": {
                "gate": gate_id, "reason": "condition_false", "edge": _edge_key(edge)}})
            return
        await self._emit(gate_id, "out", event, depth + 1)

    async def _when_ok(self, tile_id: str, event: FlowEvent) -> bool:
        """磁贴/节点的 when 口: 多个条件按 AND 计算, 不是真就不发"""
        values: list[bool | str] = []
        for e in self.definition.edges:
            if e.target != tile_id or e.target_port != "when":
                continue
            source_node = self.definition.nodes[e.source]
            source_type = self.node_types[e.source]
            if source_type is None:
                continue
            values.append(await self._eval_condition(source_node, source_type, event))
        if not values:
            return True
        return gate_and(values) is True

    async def _eval_condition(
        self, node: FlowNodeInstance, node_type: FlowNodeType, event: FlowEvent,
    ) -> bool | str:
        """对一个条件节点求值 (内置或插件), 带 needs 上下文与 state 输入"""
        if node_type.needs and node_type.needs != event.kind:
            return "error"   # needs 要求的上下文不存在
        state_view: StateView | None = None
        for e in self.definition.edges:
            if e.target == node.id and e.target_port == "state":
                state_view = self._resolve_state(e.source)
        config = self._config(node.id)
        fn = self._resolve_condition(node) if node_type.plugin else None
        try:
            evaluator = self._evaluator
            if isinstance(evaluator, ConditionEvaluator):
                result = await evaluator.evaluate(node, node_type, fn, config, event, state_view,
                                                  calls_used=self._calls_used(node.id))
            else:
                result = await evaluator(node, node_type, config, event, state_view, self._calls_used(node.id))
            if result not in (True, False, "error"):
                return "error"
            return result
        except Exception as error:
            logger.warning(f"[流程引擎] 条件 {node.id} 求值异常: {error}")
            return "error"

    def _edge_type(self, edge: FlowEdge) -> str:
        """连线的实际类型; 逻辑门按输入推导"""
        source_type = self.node_types.get(edge.source)
        if source_type is None:
            return "condition"
        port = source_type.output_port(edge.source_port)
        if port is None:
            return "condition"
        if port.derived:
            ins = [e for e in self.definition.edges if e.target == edge.source]
            for e in ins:
                if self._edge_type(e) == "event":
                    return "event"
            return "condition"
        return port.types[0]

    def _config(self, node_id: str) -> dict[str, Any]:
        """节点的生效配置 (调用方注入已解析值; 这里只取实例 config, 实际生效值由 session 层合成)"""
        node = self.definition.nodes.get(node_id)
        return dict(node.config) if node is not None else {}

    def _title(self, node_id: str) -> str:
        """节点显示名"""
        node = self.definition.nodes.get(node_id)
        return node.title if node is not None else node_id

    async def _trigger(self, wf_id: str, event: FlowEvent) -> None:
        """按 on_busy 策略调度 WF"""
        state = self._wf.get(wf_id)
        if state is None:
            return
        wf_node = self.definition.nodes[wf_id]
        on_busy = str(self._config(wf_id).get("on_busy") or "queue")
        if not state.busy:
            state.busy = True
            self._diag("flow_trigger", "fire", bodies={"output": {
                "wf": wf_id, "why": event.why, "event_id": event.event_id}})
            for handler in self._handlers:
                result = handler(wf_id, event)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            return
        if on_busy == "drop":
            self._diag("flow_trigger", "blocked", bodies={"output": {
                "wf": wf_id, "reason": "busy_drop", "why": event.why}})
            return
        if on_busy == "merge":
            previous = state.merged
            state.merged = event if previous is None else FlowEvent(
                event_id=event.event_id, kind=event.kind, tile=event.tile,
                why="合并重跑", message=event.message or previous.message,
                batch=event.batch or previous.batch,
                batch_from=min(previous.batch_from or 0, event.batch_from or 0) if event.batch or previous.batch else 0,
                batch_to=max(previous.batch_to or 0, event.batch_to or 0) if event.batch or previous.batch else 0,
                payload=event.payload or previous.payload,
                source_wf=event.source_wf or previous.source_wf,
            )
            return
        state.queued.append(event)

    def running_workflows(self) -> list[str]:
        """当前正忙的 WF 节点 id 列表, 供运行状态接口读"""
        return [wf_id for wf_id, state in self._wf.items() if state.busy]

    async def finish(self, wf_id: str, event: FlowEvent) -> FlowEvent | None:
        """
        WF 本次运行结束后取下一个事件 (merged 优先, 否则 queue 首项)

        参数:
        - wf_id: 完成的 WF 节点 id
        - event: 刚结束的事件

        返回:
        - 下一个应触发的事件; 队列空则 None
        """
        state = self._wf.get(wf_id)
        if state is None:
            return None
        if state.merged is not None:
            next_event, state.merged = state.merged, None
        elif state.queued:
            next_event = state.queued.pop(0)
        else:
            state.busy = False
            return None
        if isinstance(self._evaluator, ConditionEvaluator):
            self._evaluator.forget(event.event_id)
        return next_event


def _edge_key(e: FlowEdge) -> str:
    """连线标识"""
    return f"{e.source}.{e.source_port}->{e.target}.{e.target_port}"
