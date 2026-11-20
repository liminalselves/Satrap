"""流程图事实分析: WF 拉取链, 触发来源, 后台判定与条件上下文"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from satrap.core.type.flow.definition import FlowDefinition, FlowEdge
from satrap.core.type.flow.ports import PortType
from satrap.core.type.flow.validation import Analysis, Origin, WfInfo
from satrap.edictum.flow.graph import edge_key, output_type

if TYPE_CHECKING:
    from satrap.edictum.flow.registry import FlowNodeRegistry


_SIGNAL: tuple[PortType, ...] = ("event", "condition", "handoff")


def analyze(
    definition: FlowDefinition,
    registry: FlowNodeRegistry,
    resolved_values: dict[str, dict[str, Any]],
) -> Analysis:
    """
    对流程做静态分析: WF 拉取链, 触发来源, 后台判定, 条件上下文

    参数:
    - definition: 流程定义
    - registry: 节点类型注册表
    - resolved_values: {节点 id: 生效配置}

    返回:
    - Analysis: 供 validate 与运行时复用
    """
    nodes = definition.nodes
    edges = definition.edges
    get_type = registry.get
    edge_types: dict[str, PortType] = {}
    for edge in edges:
        edge_type = output_type(edge.source, edge.source_port, nodes, edges, get_type, set())
        if edge_type is not None:
            edge_types[edge_key(edge)] = edge_type

    def in_edges(node_id: str, port: str | None = None) -> list[FlowEdge]:
        return [edge for edge in edges if edge.target == node_id and (port is None or edge.target_port == port)]

    def out_edges(node_id: str, port: str | None = None) -> list[FlowEdge]:
        return [edge for edge in edges if edge.source == node_id and (port is None or edge.source_port == port)]

    def node_role(node_id: str) -> str:
        node = nodes.get(node_id)
        node_type = get_type(node.type) if node else None
        return node_type.role if node_type else ""

    def trace_events(node_id: str, port: str | None, via: tuple[str, ...], seen: set[str]) -> list[Origin]:
        """从触发输入口向上追溯触发来源"""
        out: list[Origin] = []
        for edge in in_edges(node_id, port):
            if edge_types.get(edge_key(edge)) != "event":
                continue
            if edge.source in seen:
                continue
            node_type = get_type(nodes[edge.source].type)
            if node_type is None:
                continue
            if node_type.role == "trigger":
                out.append(Origin(kind="tile", id=edge.source, via=via))
            elif node_type.role == "workflow" and edge.source_port == "done":
                out.append(Origin(kind="done", id=edge.source, via=via))
            elif node_type.role == "logic":
                out.extend(trace_events(edge.source, None, via + (edge.source,), seen | {edge.source}))
        return out

    wfs: dict[str, WfInfo] = {}
    pulled_by: dict[str, set[str]] = {}
    for node in nodes.values():
        node_type = get_type(node.type)
        if node_type is None or node_type.role != "workflow":
            continue
        pulls: set[str] = set()
        queue = [edge.source for edge in in_edges(node.id, "input")]
        while queue:
            node_id = queue.pop(0)
            if node_id in pulls or node_role(node_id) == "workflow":
                continue
            pulls.add(node_id)
            if node_role(node_id) == "state":
                continue
            for edge in in_edges(node_id):
                edge_type = edge_types.get(edge_key(edge))
                if edge_type is None or edge_type not in _SIGNAL:
                    queue.append(edge.source)
        for node_id in pulls:
            pulled_by.setdefault(node_id, set()).add(node.id)
        wfs[node.id] = WfInfo(
            id=node.id,
            values=resolved_values.get(node.id, {}),
            pulls=pulls,
            origins=trace_events(node.id, "trigger", (), set()),
            writes_state=[edge.target for edge in out_edges(node.id, "text") if node_role(edge.target) == "state"],
            replies_directly=any(node_role(edge.target) == "sink" for edge in out_edges(node.id, "text")),
        )

    def tool_name(tile_id: str) -> str:
        values = resolved_values.get(tile_id, {})
        return str(values.get("tool_name") or f"handoff_{tile_id}")

    for node in nodes.values():
        if node.type != "tool":
            continue
        for edge in in_edges(node.id, "caller"):
            if edge.source in wfs:
                wfs[edge.source].handoff_tools.append({"tool": tool_name(node.id), "tile": node.id})

    def callers_of(tile: str) -> list[str]:
        return [edge.source for edge in in_edges(tile, "caller") if edge.source in wfs]

    def roots_of(wf: str, seen: set[str]) -> set[str]:
        result: set[str] = set()
        if wf in seen:
            return result
        seen.add(wf)
        for origin in wfs[wf].origins:
            if origin.kind == "done":
                result.update(roots_of(origin.id, seen))
                continue
            node_type = nodes[origin.id].type
            if node_type in ("wake", "threshold"):
                result.add(node_type)
            elif node_type == "tool":
                for caller in callers_of(origin.id):
                    result.update(roots_of(caller, seen))
        return result

    for wf in wfs.values():
        wf.roots = roots_of(wf.id, set())

    def background_of(wf: str, seen: set[str]) -> bool:
        if wf in seen:
            return True
        seen.add(wf)
        workflow = wfs[wf]
        if not workflow.origins:
            return False
        for origin in workflow.origins:
            if origin.kind == "done":
                if not background_of(origin.id, seen):
                    return False
                continue
            node_type = nodes[origin.id].type
            if node_type == "threshold":
                continue
            if node_type == "tool":
                callers = callers_of(origin.id)
                if callers and all(background_of(caller, seen) for caller in callers):
                    continue
                return False
            return False
        return True

    for wf in wfs.values():
        wf.background = background_of(wf.id, set())

    drives: dict[str, set[str]] = {}
    for wf in wfs.values():
        for origin in wf.origins:
            if origin.kind == "tile":
                drives.setdefault(origin.id, set()).add(wf.id)

    # 条件求值上下文: 接磁贴 when 用磁贴 emits; 接逻辑门用该门全部触发输入的上下文
    cond_contexts: dict[str, set[str]] = {}

    def event_ctx(node_id: str, port: str | None, seen: set[str]) -> set[str]:
        result: set[str] = set()
        for edge in in_edges(node_id, port):
            if edge_types.get(edge_key(edge)) != "event" or edge.source in seen:
                continue
            node_type = get_type(nodes[edge.source].type)
            if node_type is None:
                continue
            if node_type.role == "trigger" and node_type.emits:
                result.add(node_type.emits)
            elif node_type.role == "workflow":
                result.add("call")
            elif node_type.role == "logic":
                result.update(event_ctx(edge.source, None, seen | {edge.source}))
        return result

    def ctx_of_consumer(node_id: str, seen: set[str]) -> set[str]:
        node_type = get_type(nodes[node_id].type)
        if node_type is None or node_id in seen:
            return set()
        seen.add(node_id)
        if node_type.role == "trigger" and node_type.emits:
            return {node_type.emits}
        if node_type.role == "logic":
            own = event_ctx(node_id, None, set())
            if own:
                return own
            result: set[str] = set()
            for edge in out_edges(node_id):
                result.update(ctx_of_consumer(edge.target, seen))
            return result
        return {"message", "batch", "call"}

    for node in nodes.values():
        node_type = get_type(node.type)
        if node_type is None or node_type.role != "condition":
            continue
        result: set[str] = set()
        for edge in out_edges(node.id):
            result.update(ctx_of_consumer(edge.target, set()))
        cond_contexts[node.id] = result

    return Analysis(wfs=wfs, pulled_by=pulled_by, edge_types=edge_types,
                    drives=drives, cond_contexts=cond_contexts)
