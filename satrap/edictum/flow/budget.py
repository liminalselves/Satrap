"""
模型调用计量与预算 (P2-1)

每个流程会话按群维护一份调用记录; WF 与插件节点的每次模型调用前
原子检查并扣减, 超限时抛 BudgetExceeded; 计数只存内存, 按窗口滚动
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Literal
import threading
import time


class BudgetExceeded(RuntimeError):
    """硬限制拦下的模型调用: 这次调用不发出, WF 本次运行失败"""


@dataclass(frozen=True)
class CallRecord:
    """一次模型调用"""

    source: Literal["wf", "plugin"]
    node_id: str
    event_id: str
    run_id: str
    """仅 source=='wf' 时有值"""
    at: float


class FlowCallBudget:
    """按流程会话维护的模型调用计数与硬限制"""

    def __init__(
        self,
        *,
        max_calls: int = 0,
        window_minutes: int = 60,
        count: Literal["all", "wf"] = "all",
    ) -> None:
        """
        初始化预算

        参数:
        - max_calls: 窗口内允许的模型调用总数, 0 表示不限
        - window_minutes: 统计窗口分钟数
        - count: all 计 WF 和插件节点调用; wf 只算 WF
        """
        self.max_calls = max_calls
        self.window_seconds = window_minutes * 60
        self.count = count
        self._lock = threading.Lock()
        self._calls: Deque[CallRecord] = deque()
        self._run_calls: dict[str, int] = {}
        """{run_id: 本次运行已用调用数}"""

    def _prune(self, now: float) -> None:
        """滑出窗口的旧记录"""
        cutoff = now - self.window_seconds
        while self._calls and self._calls[0].at < cutoff:
            self._calls.popleft()

    def check_and_spend(
        self,
        *,
        source: Literal["wf", "plugin"],
        node_id: str,
        event_id: str = "",
        run_id: str = "",
        per_run_limit: int = 0,
    ) -> None:
        """
        调用前原子检查并扣减

        参数:
        - source: 发起方类型
        - node_id: 发起调用的节点 id
        - event_id: 所属触发事件
        - run_id: 所属 WF 运行 (插件节点调用留空)
        - per_run_limit: 单次运行上限 (WF 的 max_calls_per_run), 0 不限

        返回:
        - 无; 超限抛 BudgetExceeded
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            if per_run_limit > 0 and self._run_calls.get(run_id, 0) >= per_run_limit:
                raise BudgetExceeded(f"单次运行模型调用上限 {per_run_limit} 已用完")
            counted = [c for c in self._calls if self.count == "all" or c.source == "wf"]
            if self.max_calls > 0 and len(counted) >= self.max_calls:
                raise BudgetExceeded(f"流程模型调用预算 {self.max_calls}/{self.window_seconds // 60} 分钟已用完")
            self._calls.append(CallRecord(source=source, node_id=node_id, event_id=event_id, run_id=run_id, at=now))
            if run_id:
                self._run_calls[run_id] = self._run_calls.get(run_id, 0) + 1

    def calls(
        self,
        *,
        kind: Literal["all", "wf"] = "all",
        scope: Literal["flow", "target"] = "flow",
        target: str = "",
        window_seconds: float = 3600,
    ) -> int:
        """
        条件节点读取的已用调用数

        参数:
        - kind: all 计全部, wf 只算 WF
        - scope: flow 整个流程, target 指定节点
        - target: scope=target 时的节点 id
        - window_seconds: 统计窗口

        返回:
        - 窗口内的调用次数
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            cutoff = now - window_seconds
            return sum(
                1 for c in self._calls
                if c.at >= cutoff
                and (kind == "all" or c.source == "wf")
                and (scope == "flow" or c.node_id == target)
            )

    def end_run(self, run_id: str) -> None:
        """WF 运行结束, 本次用量结算 (调用计数保留在窗口内)"""
        with self._lock:
            self._run_calls.pop(run_id, None)
