"""
平台发送回执

区分平台确认, 部分完成, 明确失败和结果未知,
为事件回复去重提供依据, 不将本地调用结束等同于平台确认
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SendReceipt:
    """一次逻辑发送的不可变结果, 平台消息 ID 与内部请求 ID 分离"""

    status: Literal["success", "partial", "failed", "unknown"]
    message_ids: tuple[str, ...] = ()
    failed_index: int | None = None
    reason: str = ""

    @property
    def suppress_fallback(self) -> bool:
        """
        判断是否应阻止兜底重发全文

        返回:
        - bool: 已确认任何内容或结果不明时返回 True
        """
        return self.status != "failed" or bool(self.message_ids)


def combine_receipts(receipts: list[SendReceipt]) -> SendReceipt:
    """
    汇总按顺序执行的分块回执

    参数:
    - receipts: 已尝试的分块回执, 失败后不应继续发送后续块

    返回:
    - SendReceipt: 保留所有已确认 ID 和首个失败位置, 空列表为明确失败
    """
    ids = tuple(message_id for receipt in receipts for message_id in receipt.message_ids)
    index = 0
    for receipt in receipts:
        if receipt.status != "success":
            status = "unknown" if receipt.status == "unknown" else "partial" if ids else "failed"
            return SendReceipt(status, ids, index + (receipt.failed_index or 0), receipt.reason)
        index += max(1, len(receipt.message_ids))
    return SendReceipt("success", ids) if receipts else SendReceipt("failed", reason="empty_message")
