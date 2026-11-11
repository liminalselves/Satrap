"""
免打扰时段条件节点 (示例 A)

out: condition 唯一输出 → 宿主视为条件节点;
条件节点必须同步, 经 FLOW_CONDITION_WORKERS 有界线程池调用
"""
from typing import Any


def in_hours(ctx: Any) -> bool:
    """
    判断触发时刻是否在所配时段内

    参数:
    - ctx: 宿主给的只读上下文 (config, local_time_between)

    返回:
    - 时段内为 True; 跨零点时段 (end < start) 由 local_time_between 处理
    """
    return ctx.local_time_between(ctx.config["start"], ctx.config["end"])
