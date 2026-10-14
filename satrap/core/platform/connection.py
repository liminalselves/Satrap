"""平台只读通信探测的结果与可公开错误"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectionProbeResult:
    """一次平台请求的通信结果, 不包含令牌或原始响应"""

    ok: bool
    detail: str
    elapsed_ms: int


class ConnectionProbeError(RuntimeError):
    """可直接向管理界面展示的通信探测错误"""
