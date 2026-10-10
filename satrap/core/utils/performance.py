"""使用单调时钟采集分阶段耗时, 不记录配置内容或调用凭据"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import time


class PhaseTimings:
    """累计阶段耗时, 失败及取消路径也完成计时"""

    def __init__(self) -> None:
        """记录操作起点并建立独立阶段表"""
        self._started = time.perf_counter()
        self._phases: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """
        累计一个同步或异步代码区段的耗时

        参数:
        - name: 固定的阶段名称, 不包含配置值

        返回:
        - 可包围 await 的上下文管理器, 不吞掉区段异常
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self._phases[name] = self._phases.get(name, 0) + (time.perf_counter() - started) * 1000

    def to_payload(self) -> dict[str, float]:
        """
        返回毫秒计时副本

        返回:
        - 阶段累计值及独立的总耗时, 嵌套阶段不能相加当作总耗时
        """
        return {**{name: round(value, 3) for name, value in self._phases.items()},
                'total_ms': round((time.perf_counter() - self._started) * 1000, 3)}
