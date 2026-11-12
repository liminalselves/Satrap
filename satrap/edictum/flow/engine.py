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


def _release_completed(future: Any) -> None:
    """接收工作线程归还任务的结果, 防止异常静默丢失"""
    if future.cancelled():
        return
    error = future.exception()
    if error is not None:
        logger.warning(f"[流程引擎] 条件实例异步归还失败: {error}")


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
    source_event: Any = None
    """触发它的平台消息事件; 前台运行的回复轮次与身份来源, 后台触发为 None"""
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
        diagnostic: Callable[..., Any] = record_debug,
    ) -> None:
        """
        初始化求值器

        参数:
        - timeout: 条件函数限时, 默认 150 毫秒
        - release_ref: 归还实例引用回调 (node_id, instance); 来自 FlowNodeInstancePool
        """
        self.timeout = timeout
        self._release_ref = release_ref
        self._diag = diagnostic
        self._cache: dict[tuple[str, str], bool | str] = {}
        """(event_id, 节点 id) -> 结果缓存; 每个事件对每个条件节点最多求值一次"""

    def cached(self, event_id: str, node_id: str) -> bool:
        """是否已有当前事件的条件结果; 命中时调用方不得再 acquire 实例"""
        return (event_id, node_id) in self._cache

    async def evaluate(
        self,
        node: FlowNodeInstance,
        node_type: FlowNodeType,
        fn: Callable[..., Any] | None,
        config: dict[str, Any],
        event: FlowEvent,
        state_view: StateView | None = None,
        calls_used: Callable[..., int] | None = None,
        targets: tuple[str, ...] = (),
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
        - calls_used: 宿主调用计数查询 (kind, scope, window_seconds, target), 按需调用

        返回:
        - True / False / "error"; 每个事件最多求值一次, 结果在事件内复用
        """
        key = (event.event_id, node.id)
        if key in self._cache:
            return self._cache[key]
        started_at = time.monotonic()
        self._diag("flow_node", "start", metadata={
            "node": node.id, "node_type": node.type, "event_id": event.event_id,
            "targets": list(targets),
        })
        try:
            result = await self._evaluate_once(
                node, node_type, fn, config, event, state_view, calls_used, targets)
        except BaseException as error:
            self._diag("flow_node", "end", status="failed", metadata={
                "node": node.id, "node_type": node.type, "event_id": event.event_id,
                "result": "error", "error_type": type(error).__name__,
                "duration_ms": round((time.monotonic() - started_at) * 1000, 3),
            })
            raise
        self._cache[key] = result
        self._diag("flow_node", "end", status="failed" if result == "error" else "completed", metadata={
            "node": node.id, "node_type": node.type, "event_id": event.event_id,
            "result": "error" if result == "error" else str(result).lower(),
            "duration_ms": round((time.monotonic() - started_at) * 1000, 3),
        })
        return result

    async def _evaluate_once(
        self, node: FlowNodeInstance, node_type: FlowNodeType,
        fn: Callable[..., Any] | None, config: dict[str, Any],
        event: FlowEvent, state_view: StateView | None, calls_used: Callable[..., int] | None,
        targets: tuple[str, ...] = (),
    ) -> bool | str:
        """实际求值一次; 插件条件进线程池, 内置在事件循环里"""
        if node_type.plugin:
            if fn is None:
                # setup 失败/实例缺失/被标记 failed: 判错误而不是落到内置表判真
                self._diag("flow_trigger", "blocked", bodies={"output": {
                    "node": node.id, "reason": "condition_impl_missing"}})
                logger.warning(f"[流程引擎] 条件节点 {node.id} 没有可用实现, 判为错误")
                return "error"
            # 插件条件函数: 同步函数进有界线程池, 超时/池满/异常判错误
            loop = asyncio.get_running_loop()
            started = False

            async def release() -> None:
                if self._release_ref is not None:
                    result = self._release_ref(node.id, fn)
                    if inspect.isawaitable(result):
                        await result

            def guarded(*args: Any) -> Any:
                nonlocal started
                started = True
                try:
                    return fn(*args)
                finally:
                    # 工作线程不得反向等待事件循环: loop 停止时 .result() 会永久挂住。
                    # 运行中的 loop 异步归还; 已停止/关闭或调度竞态失败时在线程内限时兜底。
                    scheduled = False
                    if loop.is_running():
                        try:
                            future = asyncio.run_coroutine_threadsafe(release(), loop)
                            future.add_done_callback(_release_completed)
                            scheduled = True
                        except RuntimeError:
                            pass
                    if not scheduled:
                        try:
                            asyncio.run(asyncio.wait_for(release(), timeout=1.0))
                        except Exception as error:
                            logger.warning(f"[流程引擎] 条件实例后台归还失败: {error}")

            try:
                ctx = self._build_ctx(node_type, config, event, state_view, calls_used, targets)
                args = self._cond_args(node_type, ctx, state_view)
                return await asyncio.wait_for(
                    FLOW_CONDITION_WORKERS.run(guarded, *args, wait_on_cancel=False),
                    timeout=self.timeout,
                )
            except (WorkerBusyError, asyncio.TimeoutError, Exception) as error:
                if isinstance(error, WorkerBusyError) or (not started and not isinstance(error, asyncio.TimeoutError)):
                    await release()
                self._diag("flow_trigger", "blocked", bodies={"output": {
                    "node": node.id, "reason": type(error).__name__}})
                logger.warning(f"[流程引擎] 条件节点 {node.id} 求值失败 ({type(error).__name__}), 判为错误")
                return "error"
        # 内置条件节点直接在事件循环求值
        try:
            return self._builtin(node, node_type, config, event, state_view)
        except Exception as error:
            logger.warning(f"[流程引擎] 内置条件 {node.id} 求值异常: {error}")
            return "error"

    def _build_ctx(
        self, node_type: FlowNodeType, config: dict[str, Any],
        event: FlowEvent, state_view: StateView | None, calls_used: Callable[..., int] | None,
        targets: tuple[str, ...] = (),
    ) -> Any:
        """组装条件函数的只读 ctx; 默认 target 预算是本次传播可达的全部 WF"""
        from types import SimpleNamespace

        def default_calls(
            kind: str = "all", scope: str = "flow", window_seconds: float = 3600, target: str = "",
        ) -> int:
            if calls_used is None:
                return 0
            if scope != "target" or target:
                return calls_used(kind=kind, scope=scope, window_seconds=window_seconds, target=target)
            # 多目标取最大已用量, 使 within 等阈值条件采用最严格结果。
            return max((calls_used(
                kind=kind, scope=scope, window_seconds=window_seconds, target=candidate,
            ) for candidate in targets), default=0)

        ns = SimpleNamespace(
            config=config,
            now=time.time(),
            kind=event.kind,
            message=event.message,
            batch=event.batch,
            calls=default_calls,
            target=targets[0] if len(targets) == 1 else "",
            targets=targets,
            local_time_between=lambda start, end: _local_time_between(start, end, ns.now),
        )
        return ns

    @staticmethod
    def _cond_args(node_type: FlowNodeType, ctx: Any, state_view: StateView | None) -> tuple[Any, ...]:
        """按输入端口顺序组装调用参数; ctx 固定第一, state 输入给 StateView"""
        args: list[Any] = [ctx]
        for port in node_type.inputs or ():
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
            if check == "version":
                return state_view.version >= int(config.get("min_version") or 1)
            return "error"
        # 未知内置类型不能默认放行; not/or 逻辑门不会走到这里, 走到说明接线错误
        logger.warning(f"[流程引擎] 未识别的内置条件类型 {node.type}, 判为错误")
        return "error"

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
    last_batch_to: int = 0
    """已下发未完成的批次事件最大 batch_to; 防止同一批重复入队"""


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
        calls_used: Callable[..., int] | None = None,
        resolve_condition: Callable[[FlowNodeInstance], Callable[..., Any] | None] | None = None,
        release_ref: Callable[[str, Any], Any] | None = None,
        resolve_config: Callable[[str], dict[str, Any]] | None = None,
        forget_event: Callable[[str], Any] | None = None,
    ) -> None:
        """
        初始化引擎

        参数:
        - definition: 已迁移的流程定义
        - node_types: {节点 id: 节点契约}
        - evaluate_condition: 插件条件求值回调 (async), 缺省用内置 ConditionEvaluator
        - resolve_state: state 槽读取回调 {state_node_id: StateView}
        - calls_used: 调用计数查询回调 (kind, scope, window_seconds, target)
        - resolve_condition: 插件条件实现解析解析回调 {node: fn}; 来自 FlowNodeInstancePool.acquire
        - release_ref: 归还实例引用回调 {node_id, instance}; 与 resolve_condition 成对
        - resolve_config: 节点生效配置回调 {node_id: 合并后配置}; 缺省用节点实例 config
        - forget_event: 事件最后一个 WF 引用结束时清理共享 transform 缓存
        """
        self.definition = definition
        self.node_types = node_types
        self._resolve_state = resolve_state or (lambda state_id: StateView.empty())
        self._calls_used = calls_used or (lambda *args, **kwargs: 0)
        self._resolve_condition = resolve_condition or (lambda node: None)
        self._release_ref = release_ref
        self._resolve_config = resolve_config
        self._forget_event = forget_event
        self._evaluator = evaluate_condition or ConditionEvaluator(release_ref=release_ref)
        self._wf: dict[str, _WfState] = {n.id: _WfState() for n in definition.nodes.values()
                                         if (t := node_types.get(n.id)) and t.role == "workflow"}
        self._handlers: list[Callable[[str, FlowEvent], Any]] = []
        self._arrivals: dict[str, dict[str, float]] = {}
        """AND 门时间窗: {gate_id: {edge_key: 到达时刻}}"""
        self._event_refs: dict[str, int] = {}
        """事件在 WF 在飞/队列中的引用数; 归零后清理条件与 transform 缓存"""
        self._tasks: set[asyncio.Task[Any]] = set()
        self._task_wfs: dict[asyncio.Task[Any], str] = {}
        """进行中的 WF 触发任务; create_task 引用由引擎持有, 关闭时可取消"""
        self._diag = record_debug

    def on_trigger(self, handler: Callable[[str, FlowEvent], Any]) -> None:
        """登记 WF 触发回调 handler(wf_id, event); 重设时先清空"""
        self._handlers = [handler]

    # ---------- 对外入口 ----------

    async def _cleanup_event(self, event_id: str) -> None:
        """仅在事件没有 WF 引用时清理事件级共享缓存"""
        if self._event_refs.get(event_id, 0) > 0:
            return
        if isinstance(self._evaluator, ConditionEvaluator):
            self._evaluator.forget(event_id)
        if self._forget_event is not None:
            result = self._forget_event(event_id)
            if inspect.isawaitable(result):
                await result

    async def _release_event(self, event_id: str) -> None:
        """运行结束释放一个 WF 引用, 最后一个引用负责清理"""
        remaining = self._event_refs.get(event_id, 0) - 1
        if remaining > 0:
            self._event_refs[event_id] = remaining
            return
        self._event_refs.pop(event_id, None)
        await self._cleanup_event(event_id)

    async def fire(
        self, tile_id: str, event: FlowEvent,
        targets: frozenset[str] | set[str] | None = None,
    ) -> None:
        """
        让一个磁贴/工具节点发出事件, 经 when 条件和逻辑门传播

        参数:
        - tile_id: 磁贴节点 id
        - event: 触发事件
        - targets: 允许接收本事件的 WF 节点 id 集; None 不限制.
          阈值磁贴按每个 WF 的已提交位置分别发批次时, 用 targets 只触发满足的那个
        """
        node = self.definition.nodes.get(tile_id)
        node_type = self.node_types.get(tile_id)
        if node is None or node_type is None or node_type.role != "trigger":
            return
        verdict = await self._when_ok(tile_id, event, targets)
        if verdict is not True:
            reason = "when_error" if verdict == "error" else "when_false"
            self._diag("flow_trigger", "blocked", bodies={"output": {"tile": tile_id, "reason": reason}})
            await self._cleanup_event(event.event_id)
            return
        await self._emit(tile_id, "event", event, depth=0, targets=targets)
        await self._cleanup_event(event.event_id)

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
            source_event=getattr(event, "source_event", None),
        )
        await self._emit(wf_id, "done", done, depth=0)
        await self._cleanup_event(done.event_id)

    async def handoff(self, tool_tile: str, event: FlowEvent) -> bool:
        """
        交接工具被调用: 检查 when 条件, 满足则发出 call 事件

        参数:
        - tool_tile: 交接工具磁贴节点 id
        - event: 调用方 WF 带来的 call 事件

        返回:
        - True 表示已交给下游 WF; False 表示条件为假未触发
        """
        verdict = await self._when_ok(tool_tile, event)
        if verdict is not True:
            reason = "when_error" if verdict == "error" else "when_false"
            self._diag("flow_trigger", "blocked", bodies={"output": {"tile": tool_tile, "reason": reason}})
            await self._cleanup_event(event.event_id)
            return False
        await self._emit(tool_tile, "event", event, depth=0)
        await self._cleanup_event(event.event_id)
        return True

    # ---------- 事件传播 ----------

    async def _emit(
        self, source: str, port: str, event: FlowEvent, depth: int,
        targets: frozenset[str] | set[str] | None = None,
    ) -> None:
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
                if targets is not None and e.target not in targets:
                    continue
                await self._trigger(e.target, event)
            elif target_type.type == "or":
                # OR 直通: 任一输入都放行 (条件是条件, 事件是事件)
                await self._emit(e.target, "out", event, depth + 1, targets)
            elif target_type.type == "and":
                await self._gate_and(e.target, e, event, depth, targets)

    async def _gate_and(
        self, gate_id: str, edge: FlowEdge, event: FlowEvent, depth: int,
        targets: frozenset[str] | set[str] | None = None,
    ) -> None:
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

        # 条件输入按三值求值; targets 参数是允许 WF 过滤器, 先解析成实际候选集合
        candidates = self._candidate_targets(gate_id, targets)
        values: list[bool | str] = []
        for e in cond_edges:
            source_node = self.definition.nodes[e.source]
            source_type = self.node_types[e.source]
            values.append(await self._eval_condition(source_node, source_type, event, targets=candidates))
        verdict = gate_and(values)
        if verdict == "error":
            self._diag("flow_trigger", "blocked", bodies={"output": {
                "gate": gate_id, "reason": "condition_error", "edge": _edge_key(edge)}})
            return
        if verdict is False:
            self._diag("flow_trigger", "blocked", bodies={"output": {
                "gate": gate_id, "reason": "condition_false", "edge": _edge_key(edge)}})
            return
        await self._emit(gate_id, "out", event, depth + 1, targets)

    def _candidate_targets(
        self, tile_id: str, allowed: frozenset[str] | set[str] | None,
    ) -> tuple[str, ...]:
        """求本次事件沿触发图可达的 WF; 条件默认 target 以此集合为准"""
        found: set[str] = set()
        pending = [tile_id]
        seen: set[str] = set()
        while pending:
            source = pending.pop()
            if source in seen:
                continue
            seen.add(source)
            for edge in self.definition.edges:
                if edge.source != source:
                    continue
                node_type = self.node_types.get(edge.target)
                if node_type is None:
                    continue
                if node_type.role == "workflow" and edge.target_port == "trigger":
                    if allowed is None or edge.target in allowed:
                        found.add(edge.target)
                elif node_type.role != "workflow":
                    pending.append(edge.target)
        return tuple(sorted(found))

    async def _when_ok(
        self, tile_id: str, event: FlowEvent,
        targets: frozenset[str] | set[str] | None = None,
    ) -> bool | str:
        """磁贴/节点的 when 口: 多个条件按 AND 计算, 保留 false/error 区别"""
        candidates = self._candidate_targets(tile_id, targets)
        values: list[bool | str] = []
        for e in self.definition.edges:
            if e.target != tile_id or e.target_port != "when":
                continue
            source_node = self.definition.nodes[e.source]
            source_type = self.node_types[e.source]
            if source_type is None:
                continue
            values.append(await self._eval_condition(
                source_node, source_type, event, targets=candidates))
        if not values:
            return True
        return gate_and(values)

    async def _eval_condition(
        self, node: FlowNodeInstance, node_type: FlowNodeType, event: FlowEvent,
        _seen: frozenset[str] = frozenset(), *, targets: tuple[str, ...] = (),
    ) -> bool | str:
        """对一个条件节点求值 (内置/插件/逻辑门), 带 needs 上下文与 state 输入"""
        # 逻辑门作为条件输入时按三值规则递归求值; 环防御返回错误
        if node_type.role == "logic":
            if node.id in _seen:
                return "error"
            seen = _seen | {node.id}
            values: list[bool | str] = []
            for e in self.definition.edges:
                if e.target != node.id or self._edge_type(e) != "condition":
                    continue
                source_node = self.definition.nodes[e.source]
                source_type = self.node_types[e.source]
                if source_type is None:
                    continue
                values.append(await self._eval_condition(source_node, source_type, event, seen, targets=targets))
            if node.type == "not":
                return gate_not(values[0]) if len(values) == 1 else "error"
            if node.type == "or":
                return gate_or(values)
            if node.type == "and":
                return gate_and(values)
            return "error"
        if node_type.needs and node_type.needs != event.kind:
            return "error"   # needs 要求的上下文不存在
        state_view: StateView | None = None
        state_ports = {p.name for p in node_type.inputs or () if p.types == ("state",)}
        for e in self.definition.edges:
            if e.target == node.id and e.target_port in state_ports:
                state_view = self._resolve_state(e.source)
                if inspect.isawaitable(state_view):
                    state_view = await state_view
        config = self._config(node.id)
        try:
            evaluator = self._evaluator
            cached = isinstance(evaluator, ConditionEvaluator) and evaluator.cached(event.event_id, node.id)
            fn = self._resolve_condition(node) if node_type.plugin and not cached else None
            if isinstance(evaluator, ConditionEvaluator):
                result = await evaluator.evaluate(node, node_type, fn, config, event, state_view,
                                                  calls_used=self._calls_used, targets=targets)
            else:
                result = await evaluator(node, node_type, config, event, state_view, self._calls_used)
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
        """节点的生效配置: 宿主注入四层合并结果时用它, 否则退回节点实例 config"""
        if self._resolve_config is not None:
            return self._resolve_config(node_id)
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
            if event.kind == "batch":
                state.last_batch_to = max(state.last_batch_to, event.batch_to)
            state.busy = True
            self._event_refs[event.event_id] = self._event_refs.get(event.event_id, 0) + 1
            self._diag("flow_trigger", "fire", bodies={"output": {
                "wf": wf_id, "why": event.why, "event_id": event.event_id}})
            for handler in self._handlers:
                result = handler(wf_id, event)
                if inspect.isawaitable(result):
                    # 直接调度 handler, 避免任务尚未启动就取消时包装协程遗留未 await 的参数
                    task = asyncio.create_task(result)
                    self._tasks.add(task)
                    self._task_wfs[task] = wf_id
                    task.add_done_callback(
                        lambda done, wid=wf_id: self._handler_done(done, wid))
            return
        if on_busy == "drop" or event.kind == "batch" and event.batch_to <= state.last_batch_to:
            # drop 策略丢弃; 或: 已下发过的批次 (batch_to 不超过在飞上限) 不重复入队,
            # 失败重试按未推进的 committed 位置重新读批, batch_to 会变 (新到的消息),
            # 同一批数据不会再排第二遍
            reason = "busy_drop" if on_busy == "drop" else "batch_already_pending"
            self._diag("flow_trigger", "blocked", bodies={"output": {
                "wf": wf_id, "reason": reason, "why": event.why}})
            return
        if on_busy == "merge":
            previous = state.merged
            self._event_refs[event.event_id] = self._event_refs.get(event.event_id, 0) + 1
            state.merged = event if previous is None else FlowEvent(
                event_id=event.event_id, kind=event.kind, tile=event.tile,
                why="合并重跑", message=event.message or previous.message,
                batch=event.batch or previous.batch,
                batch_from=min(previous.batch_from or 0, event.batch_from or 0) if event.batch or previous.batch else 0,
                batch_to=max(previous.batch_to or 0, event.batch_to or 0) if event.batch or previous.batch else 0,
                payload=event.payload or previous.payload,
                source_wf=event.source_wf or previous.source_wf,
                source_event=event.source_event or previous.source_event,
            )
            if event.kind == "batch":
                state.last_batch_to = max(state.last_batch_to, event.batch_to)
            if previous is not None:
                await self._release_event(previous.event_id)
            return
        if event.kind == "batch":
            state.last_batch_to = max(state.last_batch_to, event.batch_to)
        self._event_refs[event.event_id] = self._event_refs.get(event.event_id, 0) + 1
        state.queued.append(event)

    def _handler_done(self, task: asyncio.Task[Any], wf_id: str) -> None:
        """接收 handler 异常并兜底复位; 正常结束由 session._run_workflow 的 finish 负责"""
        self._tasks.discard(task)
        self._task_wfs.pop(task, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        logger.error(f"[流程引擎] WF {wf_id} 运行任务未处理异常: {error}")
        state = self._wf.get(wf_id)
        if state is not None:
            state.busy = False
            state.last_batch_to = 0

    def running_workflows(self) -> list[str]:
        """当前正忙的 WF 节点 id 列表, 供运行状态接口读"""
        return [wf_id for wf_id, state in self._wf.items() if state.busy]

    def pending_batch_max(self, wf_id: str, tile_id: str) -> int:
        """该 WF 排队/合并中尚未运行的批次事件的最大 batch_to (在飞上限)"""
        state = self._wf.get(wf_id)
        if state is None:
            return 0
        out = 0
        for e in (*state.queued, state.merged):
            if e is not None and e.kind == "batch" and e.tile == tile_id:
                out = max(out, e.batch_to)
        return out

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
        await self._release_event(event.event_id)
        if state.merged is not None:
            next_event, state.merged = state.merged, None
        elif state.queued:
            next_event = state.queued.pop(0)
        else:
            state.busy = False
            # 空闲时在飞上限回落: 已推进位置覆盖或等下次检查重新读批
            state.last_batch_to = 0
            return None
        return next_event

    async def cancel_workflow(self, wf_id: str) -> None:
        """取消指定 WF 的在飞与排队运行, 用于 /new drain 超时后禁止晚提交/晚发送"""
        state = self._wf.get(wf_id)
        pending_events: list[str] = []
        if state is not None:
            pending_events.extend(event.event_id for event in state.queued)
            if state.merged is not None:
                pending_events.append(state.merged.event_id)
            state.queued.clear()
            state.merged = None
        tasks = [task for task, owner in self._task_wfs.items() if owner == wf_id]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for event_id in pending_events:
            await self._release_event(event_id)
        if state is not None:
            state.busy = False
            state.last_batch_to = 0

    async def shutdown(self) -> None:
        """会话释放: 取消全部进行中的 WF 触发任务并清理 AND 时间窗"""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._task_wfs.clear()
        self._arrivals.clear()


def _edge_key(e: FlowEdge) -> str:
    """连线标识"""
    return f"{e.source}.{e.source_port}->{e.target}.{e.target_port}"
