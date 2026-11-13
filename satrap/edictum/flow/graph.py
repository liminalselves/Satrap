"""流程定义的无状态图工具, 供静态分析、校验和运行引擎共用"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from satrap.core.type.flow.definition import FlowEdge
from satrap.core.type.flow.ports import PortType
from satrap.core.type.flow.registry import FlowNodeType


def edge_key(edge: FlowEdge) -> str:
    """返回连线在诊断、事件到达和分析结果中的稳定标识"""
    return f"{edge.source}.{edge.source_port}->{edge.target}.{edge.target_port}"


def output_type(
    node_id: str,
    port: str,
    nodes: dict[str, Any],
    edges: list[FlowEdge],
    get_type: Callable[[str], FlowNodeType | None],
    seen: set[str],
) -> PortType | None:
    """
    推导一个输出端口的实际类型; 逻辑门按输入推导 (有触发输入时输出触发, 否则条件)

    参数:
    - node_id: 节点 id
    - port: 输出端口名
    - nodes: {id: FlowNodeInstance}
    - edges: 连线列表
    - get_type: 按类型名查契约
    - seen: 递归去重集合

    返回:
    - PortType | None: 推导不出返回 None
    """
    node = nodes.get(node_id)
    node_type = get_type(node.type) if node else None
    if node_type is None:
        return None
    port_def = node_type.output_port(port)
    if port_def is None:
        return None
    if not port_def.derived:
        return port_def.types[0]
    if node_id in seen:
        return "condition"
    seen.add(node_id)
    ins = [edge for edge in edges if edge.target == node_id]
    for edge in ins:
        edge_type = output_type(edge.source, edge.source_port, nodes, edges, get_type, seen)
        if edge_type == "event":
            return "event"
    return "condition"
