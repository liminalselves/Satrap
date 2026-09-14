"""
编程工具的会话绑定与替换项校验

供同步和异步工具共用执行前置条件, 仅处理类型收窄和内存数据,
文件读写与用户审批由各自的工具入口负责
"""

from __future__ import annotations
from collections.abc import Sequence
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from satrap.edictum import AsyncSimpleSession, SimpleSession

SessionT = TypeVar("SessionT", "SimpleSession", "AsyncSimpleSession")
ReplacementPair = tuple[str, str, bool]


def require_bound_session(session: SessionT | None, tool_name: str) -> SessionT:
    """
    获取已绑定会话, 未绑定时报告工具名称

    参数:
    - session: 工具当前会话, None 表示尚未绑定
    - tool_name: 用于错误定位的工具名

    返回:
    - 已绑定的原会话对象, 未绑定时抛出 RuntimeError
    """
    if session is None:
        raise RuntimeError(f"{tool_name} 未绑定会话")
    return session


def prepare_replacements(
    content: str, replacements: Sequence[object] | None
) -> list[ReplacementPair] | str:
    """
    基于原始文件内容校验全部替换项

    参数:
    - content: 替换前的完整原文
    - replacements: 替换项集合, None 或空集合按既有行为返回空列表

    返回:
    - 校验通过时返回有序替换列表, 否则返回首个格式或匹配错误
    """
    pairs: list[ReplacementPair] = []
    for i, item in enumerate(replacements or [], 1):
        if not isinstance(item, dict):
            return f"错误: 第 {i} 个替换项格式无效 (需 {{old, new, replace_all?}})"
        rep = cast(dict[str, object], item)
        # 工具参数来自动态 JSON, 在校验边界收窄字典字段类型
        if not str(rep.get("old") or ""):
            return f"错误: 第 {i} 个替换项格式无效 (需 {{old, new, replace_all?}})"
        old = str(rep["old"])
        if old not in content:
            return f"错误: 第 {i} 个替换项未找到匹配: {old[:80]}"
        pairs.append((old, str(rep.get("new") or ""), bool(rep.get("replace_all"))))
    # 先校验全部 old 均存在再返回执行计划, 避免部分替换后才发现输入错误
    return pairs
