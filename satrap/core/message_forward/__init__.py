"""平台无关的原消息转发接缝"""
from __future__ import annotations


class ForwardError(RuntimeError):
    """携带稳定错误码的转发错误"""

    def __init__(self, code: str, message: str) -> None:
        """
        保存公开错误原因

        参数:
        - code: 稳定错误码
        - message: 可展示的失败说明
        """
        super().__init__(message)
        self.code = code
