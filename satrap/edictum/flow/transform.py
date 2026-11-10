"""
transform 求值: 拉取调度, 同一事件内共享, 等待者计数取消, 输入输出校验

WF 运行开始时按拓扑顺序拉取上游数据节点; 同一触发事件驱动的多个 WF
拉取同一 transform 只求值一次, 结果在事件内共享; 插件 transform 同步
函数进工作线程, 异步函数直接 await
"""
from __future__ import annotations

from satrap.core.utils.async_worker import SESSION_WORKERS, WorkerBusyError
from satrap.edictum.flow.definition import FlowDefinition, FlowNodeInstance
from satrap.edictum.flow.registry import FlowNodeType
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
    waiters: set[str] = field(default_factory=set)
    """等待它的 WF run_id"""
    result: Any = None
    error: BaseException | None = None
    done: bool = False


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
        timeout: float = 60.0,
    ) -> None:
        """
        初始化求值调度器

        参数:
        - definition: 流程定义
        - node_types: {节点 id: 节点契约}
        - resolve_transform: 插件节点函数解析回调 {node: fn}; 内置节点用内置实现
        - resolve_state: state 槽读取回调
        - timeout: 节点求值默认超时
        """
        self.definition = definition
        self.node_types = node_types
        self._resolve_transform = resolve_transform or (lambda node, run_id: None)
        self._resolve_state = resolve_state or (lambda state_id: StateView.empty())
        self.timeout = timeout
        self._cells: dict[tuple[str, str], _EvalCell] = {}
        """(event_id, 节点 id) -> 共享求值"""
        self._in_edges: dict[str, list[Any]] = {}
        for e in definition.edges:
            self._in_edges.setdefault(e.target, []).append(e)

    async def pull(self, wf_id: str, node_id: str, run_id: str, event: Any) -> Any:
        """
        拉取一个数据节点, 同一事件内共享求值结果

        参数:
        - wf_id: 拉取方 WF 节点 id
        - node_id: 要拉取的数据节点 id
        - run_id: 本次 WF 运行 id (等待者计数用)
        - event: 触发事件

        返回:
        - 节点输出值; 求值出错抛出同一异常给全部等待者
        """
        node = self.definition.nodes.get(node_id)
        node_type = self.node_types.get(node_id)
        if node is None or node_type is None:
            raise ValueError(f"数据节点不存在: {node_id}")
        if node_type.role == "state":
            return self._resolve_state(node_id)
        key = (event.event_id, node_id)
        cell = self._cells.get(key)
        if cell is None:
            cell = _EvalCell()
            self._cells[key] = cell
        cell.waiters.add(run_id)
        if cell.done:
            cell.waiters.discard(run_id)
            if cell.error is not None:
                raise cell.error
            return cell.result
        if cell.task is None:
            cell.task = asyncio.create_task(self._evaluate(node, node_type, run_id, event))
        try:
            result = await asyncio.wait_for(asyncio.shield(cell.task), timeout=self.timeout)
            cell.waiters.discard(run_id)
            return result
        except asyncio.CancelledError:
            cell.waiters.discard(run_id)
            # 全部等待者取消才中断计算
            if not cell.waiters and cell.task is not None:
                cell.task.cancel()
            raise
        except asyncio.TimeoutError as error:
            cell.waiters.discard(run_id)
            cell.error = error
            cell.done = True
            if not cell.waiters and cell.task is not None:
                cell.task.cancel()
            raise
        except BaseException as error:
            cell.waiters.discard(run_id)
            cell.error = error
            cell.done = True
            raise

    async def _evaluate(self, node: FlowNodeInstance, node_type: FlowNodeType, run_id: str, event: Any) -> Any:
        """实际求值一个节点; 完成时把结果写回 cell"""
        key = (event.event_id, node.id)
        cell = self._cells[key]
        try:
            result = await self._call(node, node_type, run_id, event)
            cell.result = result
            cell.done = True
            return result
        except BaseException as error:
            cell.error = error
            cell.done = True
            raise

    async def _call(self, node: FlowNodeInstance, node_type: FlowNodeType, run_id: str, event: Any) -> Any:
        """调用节点函数: 内置节点走内置实现, 插件节点按输入组装参数"""
        if node.type == "bucket":
            return await self._eval_bucket(node, event)
        if node.type == "source":
            raise ValueError(f"source 节点由宿主直接供消息, 不应走 transform 求值")
        if node.type == "assemble":
            return await self._eval_assemble(node, run_id, event)
        fn = self._resolve_transform(node, run_id)
        if fn is None:
            raise ValueError(f"transform 节点 {node.id} 没有可调用实现")
        args = await self._transform_args(node, node_type, run_id, event)
        if inspect.iscoroutinefunction(fn):
            return await fn(*args)
        return await SESSION_WORKERS.run(fn, *args)

    async def _transform_args(
        self, node: FlowNodeInstance, node_type: FlowNodeType, run_id: str, event: Any,
    ) -> tuple[Any, ...]:
        """按输入端口声明组装节点函数的实参"""
        from types import SimpleNamespace
        ctx = SimpleNamespace(config=dict(node.config), node_id=node.id, scope=None,
                              now=time.time(), diagnostic=lambda *a, **kw: None,
                              resources={}, service=lambda *a, **kw: None,
                              replace_ignore_case=_replace_ignore_case)
        args: list[Any] = [ctx]
        for port in node_type.inputs:
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

    async def _eval_bucket(self, node: FlowNodeInstance, event: Any) -> tuple[FlowMessage, ...]:
        """桶: 档案的只读视图, include_outbound 决定是否含机器人发言"""
        source_messages = await self._pull_input(node, "in", event) or ()
        include_outbound = bool(node.config.get("include_outbound", True))
        return tuple(m for m in source_messages if include_outbound or not m.is_bot)

    async def _eval_assemble(self, node: FlowNodeInstance, run_id: str, event: Any) -> tuple[ContextBlock, ...]:
        """拼装: 快照覆盖后 + 最新 n 条 + 去重"""
        snapshot = await self._pull_input(node, "snapshot", event)
        messages: tuple[FlowMessage, ...] = await self._pull_input(node, "messages", event) or ()
        blocks: list[ContextBlock] = []
        if isinstance(snapshot, StateView) and snapshot.content:
            blocks.append(ContextBlock(kind="snapshot", text=snapshot.content, source_node=node.id))
        if node.config.get("after_coverage", True):
            covers_to = snapshot.covers_to if isinstance(snapshot, StateView) else 0
            pending = [m for m in messages if m.position > covers_to]
            max_pending = int(node.config.get("max_pending") or 80)
            max_chars = int(node.config.get("max_pending_chars") or 8000)
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
        latest_n = int(node.config.get("latest_n") or 0)
        if latest_n > 0 and messages:
            dedupe = bool(node.config.get("dedupe", True))
            seen = {b.text for b in blocks}
            extra = [m for m in messages[-latest_n:] if not dedupe or m.text not in seen]
            if extra:
                blocks.append(ContextBlock(kind="messages",
                                           text="\n".join(m.text for m in extra), source_node=node.id))
        return tuple(blocks)

    async def _pull_input(self, node: FlowNodeInstance, port: str, event: Any) -> Any:
        """取一个输入口的值; 未连线返回 None"""
        for e in self._in_edges.get(node.id, []):
            if e.target_port != port:
                continue
            return await self.pull(node.id, e.source, "", event)
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
