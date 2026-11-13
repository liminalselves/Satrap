"""流程节点装饰器。"""
from __future__ import annotations
from typing import Any, Callable

def node(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    为节点函数指定节点名, 用于函数名不能用作节点名的场合

    参数:
    - name: meta.yaml flow_nodes 里声明的节点名

    返回:
    - 装饰器: 给函数打上 __flow_node_name__ 标记
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, "__flow_node_name__", name)
        return func
    return decorator
