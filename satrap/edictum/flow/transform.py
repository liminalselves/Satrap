"""
transform 求值: 拉取调度, 同一事件内共享, 等待者计数取消, 输入输出校验

WF 运行开始时按拓扑顺序拉取上游数据节点; 同一触发事件驱动的多个 WF
拉取同一 transform 只求值一次, 结果在事件内共享; 不同事件各求值一次;
全部等待者取消才中断计算
"""
from __future__ import annotations

from satrap.core.utils.async_worker import SESSION_WORKERS, WorkerBusyError
from satrap.core.log.debug.hooks import record_debug
from satrap.edictum.flow.definition import FlowDefinition, FlowNodeInstance
from satrap.edictum.flow.registry import DEFAULT_SOURCE_MAX_MESSAGES, FlowNodeType
from satrap.edictum.flow.types import ContextBlock, FlowMessage, StateView

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class _EvalCell:
    """一次共享求值: 任务, 等待者与结果"""

    task: asyncio.Task[Any] | None = None
    waiters: dict[str, float | None] = field(default_factory=dict)
    """等待它的 WF run_id -> 该运行剩余超时秒数 (None 表示不限)"""
    result: Any = None
    error: BaseException | None = None
    done: bool = False

    def deadline(self, default: float) -> float:
        """等待者里剩余时间最长的超时; 没有限制信息时用宿主默认"""
        values = [v for v in self.waiters.values() if v is not None]
        return max(values, default=default) if values else default


class TransformEvaluator:
    """
    transform 求值调度器: 以触发事件为共享边界

    同一事件内, 多个 WF 拉取同一节点只求值一次并共享结果;
    不同事件各求值一次; 全部等待者取消才中断计算
    """

    def __init__(
        self,
        definition: FlowDefinition,
        node_types: dict[str, FlowNodeType],
        *,
        resolve_transform: Callable[[FlowNodeInstance, str], Callable[..., Any] | None] | None = None,
        resolve_state: Callable[[str], StateView] | None = None,
        release_ref: Callable[[str, Any], Any] | None = None,
        resolve_source: Callable[[], tuple[FlowMessage, ...]] | None = None,
        resolve_config: Callable[[str], dict[str, Any]] | None = None,
        build_ctx: Callable[[FlowNodeInstance, dict[str, Any]], Any] | None = None,
        diagnostic: Callable[..., Any] = record_debug,
        timeout: float = 60.0,
    ) -> None:
        """
        初始化求值调度器

        参数:
        - definition: 流程定义
        - node_types: {节点 id: 节点契约}
        - resolve_transform: 插件节点函数解析回调 {node: fn}; 内置节点用内置实现
        - resolve_state: state 槽读取回调
        - release_ref: 归还实例引用的回调 (node_id, instance); 来自 FlowNodeInstancePool
        - resolve_source: source 节点的消息供给回调, 返回当前档案视图
        - resolve_config: 节点生效配置回调 {node_id: 合并后配置}; 缺省用实例 config
        - build_ctx: 插件节点 ctx 构造回调 (node, 生效配置) -> ctx; 缺省用最小 ctx
        - timeout: 节点求值默认超时
        """
        self.definition = definition
        self.node_types = node_types
        self._resolve_transform = resolve_transform or (lambda node, run_id: None)
        self._resolve_state = resolve_state or (lambda state_id: StateView.empty())
        self._release_ref = release_ref
        self._resolve_source = resolve_source or (lambda: ())
        self._resolve_config = resolve_config
        self._build_ctx = build_ctx
        self._diag = diagnostic
        self.timeout = timeout
        self._cells: dict[tuple[str, str], _EvalCell] = {}
        """(event_id, 节点 id) -> 共享求值"""
        self._in_edges: dict[str, list[Any]] = {}
        for e in definition.edges:
            self._in_edges.setdefault(e.target, []).append(e)

    def _config(self, node_id: str) -> dict[str, Any]:
        """节点生效配置: 宿主注入四层合并结果时用它, 否则退回实例 config"""
        if self._resolve_config is not None:
            return self._resolve_config(node_id)
        node = self.definition.nodes.get(node_id)
        return dict(node.config) if node is not None else {}

    def _direct_source_query(self, node: FlowNodeInstance) -> dict[str, Any]:
        """直连视图只多读一条用于识别截断, 不把整个保留期载入内存。"""
        limit = int(self._config(node.id).get("max_messages") or DEFAULT_SOURCE_MAX_MESSAGES)
        return {"after": 0, "pending_limit": 0, "latest_n": limit + 1,
                "truncate_to": limit, "source_node": node.id}

    def _truncate_direct_messages(
        self, node: FlowNodeInstance, messages: tuple[FlowMessage, ...], query: dict[str, Any],
    ) -> tuple[FlowMessage, ...]:
        """最终输出再次执行硬上限, 兼容不接收查询参数的旧 source resolver。"""
        limit = int(query.get("truncate_to") or 0)
        if limit <= 0 or len(messages) <= limit:
            return messages
        self._diag("flow_source", "truncated", metadata={
            "node": node.id, "node_type": node.type, "max_messages": limit,
            "read_messages": len(messages), "dropped_messages": len(messages) - limit,
        })
        return tuple(messages[-limit:])

    def forget(self, event_id: str) -> None:
        """事件相关的共享求值全部结束后清掉缓存, 避免跨事件累积"""
        for key in [key for key, cell in self._cells.items()
                    if key[0] == event_id and (cell.done or cell.task is None)]:
            del self._cells[key]

    def forget_run(self, run_id: str) -> None:
        """一个 WF 运行结束: 把它从全部等待者中移除, 空等待者且未开始的 cell 取消"""
        for key, cell in list(self._cells.items()):
            cell.waiters.pop(run_id, None)
            if not cell.waiters and cell.task is not None and not cell.done:
                cell.task.cancel()
            # 已完成结果由事件最后一个 WF 引用结束时统一 forget(event_id),
            # 不能在首个 WF 完成时删掉其他 WF 尚未拉取的共享结果

    async def pull(
        self, wf_id: str, node_id: str, run_id: str, event: Any,
        timeout: float | None = None,
        source_query: dict[str, Any] | None = None,
    ) -> Any:
        """
        拉取一个数据节点, 同一事件内共享求值结果

        参数:
        - wf_id: 拉取方 WF 节点 id
        - node_id: 要拉取的数据节点 id
        - run_id: 本次 WF 运行 id (等待者计数用)
        - event: 触发事件
        - timeout: 本次运行的剩余超时; cell 超时取全部等待者中最大者

        返回:
        - 节点输出值; 求值出错抛出同一异常给全部等待者
        """
        node = self.definition.nodes.get(node_id)
        node_type = self.node_types.get(node_id)
        if node is None or node_type is None:
            raise ValueError(f"数据节点不存在: {node_id}")
        if node_type.role == "state":
            value = self._resolve_state(node_id)
            return await value if inspect.isawaitable(value) else value
        if node.type == "source":
            # source 由宿主直接供档案视图, 不走求值缓存。assemble 传自己的覆盖窗口;
            # 直连 WF/自定义 transform 使用节点 max_messages, 多读一条用于诊断截断。
            bounded = source_query is None
            if bounded:
                source_query = self._direct_source_query(node)
            try:
                accepts_query = len(inspect.signature(self._resolve_source).parameters) > 0
            except (TypeError, ValueError):
                accepts_query = True
            value = self._resolve_source(source_query) if accepts_query else self._resolve_source()
            messages = await value if inspect.isawaitable(value) else value
            result = tuple(messages or ())
            return self._truncate_direct_messages(node, result, source_query) if bounded else result
        if node.type == "bucket" and source_query is not None:
            # 不同 assemble 的覆盖位置/窗口可能不同, 有界桶查询不能复用另一个请求的 cell。
            return await self._eval_bucket(node, run_id, event, source_query)
        key = (event.event_id, node_id)
        cell = self._cells.get(key)
        if cell is None:
            cell = _EvalCell()
            self._cells[key] = cell
        cell.waiters[run_id] = timeout
        if cell.done:
            cell.waiters.pop(run_id, None)
            if cell.error is not None:
                raise cell.error
            return cell.result
        if cell.task is None:
            cell.task = asyncio.create_task(self._evaluate(node, node_type, run_id, event))
        try:
            # 求值上限取当前等待者中剩余时间最长的一个 (运行时契约 3.4),
            # 但每个等待者只按自己的剩余时间等; 超时只移出它自己, cell 对剩余等待者继续
            wait = min(timeout if timeout is not None else self.timeout, cell.deadline(self.timeout))
            result = await asyncio.wait_for(asyncio.shield(cell.task), timeout=wait)
            cell.waiters.pop(run_id, None)
            return result
        except asyncio.CancelledError:
            cell.waiters.pop(run_id, None)
            # 全部等待者取消才中断计算
            if not cell.waiters and cell.task is not None:
                cell.task.cancel()
            raise
        except asyncio.TimeoutError as error:
            cell.waiters.pop(run_id, None)
            # 只有最后一个等待者超时才把整个 cell 判死
            if not cell.waiters:
                cell.error = error
                cell.done = True
                if cell.task is not None:
                    cell.task.cancel()
            raise
        except BaseException as error:
            cell.waiters.pop(run_id, None)
            cell.error = error
            cell.done = True
            raise

    async def _evaluate(self, node: FlowNodeInstance, node_type: FlowNodeType, run_id: str, event: Any) -> Any:
        """实际求值一个节点; 完成时把结果写回 cell"""
        key = (event.event_id, node.id)
        cell = self._cells[key]
        started_at = time.monotonic()
        self._diag("flow_node", "start", metadata={
            "node": node.id, "node_type": node.type, "event_id": event.event_id, "run_id": run_id})
        try:
            result = await self._call(node, node_type, run_id, event)
            cell.result = result
            cell.done = True
            self._diag("flow_node", "end", status="completed", metadata={
                "node": node.id, "node_type": node.type, "event_id": event.event_id, "run_id": run_id,
                "duration_ms": round((time.monotonic() - started_at) * 1000, 3)})
            return result
        except BaseException as error:
            cell.error = error
            cell.done = True
            self._diag("flow_node", "end", status="failed", metadata={
                "node": node.id, "node_type": node.type, "event_id": event.event_id, "run_id": run_id,
                "error_type": type(error).__name__,
                "duration_ms": round((time.monotonic() - started_at) * 1000, 3)})
            raise

    async def _call(self, node: FlowNodeInstance, node_type: FlowNodeType, run_id: str, event: Any) -> Any:
        """调用节点函数: 内置节点走内置实现, 插件节点按输入组装参数"""
        if node.type == "bucket":
            return await self._eval_bucket(node, run_id, event)
        if node.type == "assemble":
            return await self._eval_assemble(node, run_id, event)
        if node_type.role == "trigger" or node_type.role == "logic" or node_type.role == "condition":
            raise ValueError(f"节点 {node.id} ({node.type}) 不是数据节点, 不能拉取")
        fn = self._resolve_transform(node, run_id)
        if fn is None:
            raise ValueError(f"transform 节点 {node.id} 没有可调用实现")
        try:
            args = await self._transform_args(node, node_type, run_id, event)
            if inspect.iscoroutinefunction(fn):
                return await fn(*args)
            if inspect.isclass(fn):
                raise ValueError(f"transform 节点 {node.id} 的实现是未实例化的类")
            return await SESSION_WORKERS.run(fn, *args)
        finally:
            # 经实例池取得的调用要归还引用 (P2-2), 使进行中的求值完成后旧实例才 close
            if self._release_ref is not None:
                result = self._release_ref(node.id, fn)
                if inspect.isawaitable(result):
                    await result

    async def _transform_args(
        self, node: FlowNodeInstance, node_type: FlowNodeType, run_id: str, event: Any,
    ) -> tuple[Any, ...]:
        """按输入端口声明组装节点函数的实参"""
        resolved = self._config(node.id)
        if self._build_ctx is not None:
            ctx = self._build_ctx(node, resolved)
        else:
            from types import SimpleNamespace
            ctx = SimpleNamespace(config=resolved, node_id=node.id, scope=None,
                                  now=time.time(), diagnostic=lambda *a, **kw: None,
                                  resources={}, service=lambda *a, **kw: None,
                                  replace_ignore_case=_replace_ignore_case)
        args: list[Any] = [ctx]
        for port in node_type.inputs or ():
            if port.name == "when":
                continue
            edges = [e for e in self._in_edges.get(node.id, []) if e.target_port == port.name]
            if not edges:
                args.append(None if not port.required else _empty_for(port.types[0]))
                continue
            value = await self.pull(node.id, edges[0].source, run_id, event)
            args.append(_empty_for(port.types[0]) if value is None and port.required else value)
        return tuple(args)

    # ---------- 内置 transform ----------

    async def _eval_bucket(
        self, node: FlowNodeInstance, run_id: str, event: Any,
        source_query: dict[str, Any] | None = None,
    ) -> tuple[FlowMessage, ...]:
        """桶: 档案的只读视图, include_outbound 决定是否含机器人发言"""
        include_outbound = bool(self._config(node.id).get("include_outbound", True))
        bounded = source_query is None
        if bounded:
            source_query = self._direct_source_query(node)
        source_query = {**source_query, "include_outbound": include_outbound}
        source_messages = await self._pull_input(
            node, "in", run_id, event, source_query=source_query) or ()
        messages = tuple(m for m in source_messages if include_outbound or not m.is_bot)
        return self._truncate_direct_messages(node, messages, source_query) if bounded else messages

    async def _eval_assemble(self, node: FlowNodeInstance, run_id: str, event: Any) -> tuple[ContextBlock, ...]:
        """拼装: 快照覆盖后 + 最新 n 条 + 去重"""
        config = self._config(node.id)
        snapshot = await self._pull_input(node, "snapshot", run_id, event)
        covers_to = snapshot.covers_to if isinstance(snapshot, StateView) else 0
        source_query = {
            "after": covers_to if config.get("after_coverage", True) else 0,
            "pending_limit": int(config.get("max_pending") or 80)
                if config.get("after_coverage", True) else 0,
            "latest_n": int(config.get("latest_n") or 0),
        }
        messages: tuple[FlowMessage, ...] = await self._pull_input(
            node, "messages", run_id, event, source_query=source_query) or ()
        blocks: list[ContextBlock] = []
        if isinstance(snapshot, StateView) and snapshot.content:
            blocks.append(ContextBlock(kind="snapshot", text=snapshot.content, source_node=node.id))
        if config.get("after_coverage", True):
            pending = [m for m in messages if m.position > covers_to]
            max_pending = int(config.get("max_pending") or 80)
            max_chars = int(config.get("max_pending_chars") or 8000)
            selected: list[FlowMessage] = []
            chars = 0
            for m in reversed(pending[-max_pending:]):
                if chars + len(m.text) > max_chars:
                    break
                selected.append(m)
                chars += len(m.text)
            pending = list(reversed(selected))
            if pending:
                blocks.append(ContextBlock(kind="messages",
                                           text="\n".join(m.text for m in pending), source_node=node.id))
        latest_n = int(config.get("latest_n") or 0)
        if latest_n > 0 and messages:
            dedupe = bool(config.get("dedupe", True))
            covered = {m.text for b in blocks for m in [b]}
            extra = [m for m in messages[-latest_n:]
                     if not dedupe or m.text not in covered]
            if extra:
                blocks.append(ContextBlock(kind="messages",
                                           text="\n".join(m.text for m in extra), source_node=node.id))
        return tuple(blocks)

    async def _pull_input(
        self, node: FlowNodeInstance, port: str, run_id: str, event: Any,
        source_query: dict[str, Any] | None = None,
    ) -> Any:
        """取一个输入口的值; 未连线返回 None"""
        for e in self._in_edges.get(node.id, []):
            if e.target_port != port:
                continue
            return await self.pull(node.id, e.source, run_id, event, source_query=source_query)
        return None


def _empty_for(port_type: str) -> Any:
    """端口类型的空值"""
    if port_type == "state":
        return StateView.empty()
    if port_type in ("messages", "context"):
        return ()
    return ""


def _replace_ignore_case(text: str, old: str, new: str) -> str:
    """大小写不敏感的文本替换, 供插件节点共用"""
    import re
    return re.sub(re.escape(old), new, text, flags=re.IGNORECASE)
