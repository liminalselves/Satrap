"""
调用预算条件节点 (示例 F)

out: condition 唯一输出 → 宿主视为条件节点; 计数由宿主维护,
本节点只在触发前软判断, 真正保证上限用 params.flow.budget 或
WF 的 max_calls_per_run 硬限制
"""
from typing import Any


def within(ctx: Any) -> bool:
    """
    窗口内已用调用数低于上限时为真

    参数:
    - ctx: 宿主给的只读上下文 (config, calls)

    返回:
    - 预算内为 True
    """
    used = ctx.calls(
        kind=ctx.config["count"],
        scope=ctx.config["scope"],
        window_seconds=ctx.config["window_minutes"] * 60,
    )
    return used < ctx.config["max_calls"]
