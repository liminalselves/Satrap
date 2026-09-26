"""
平台发送回执

区分平台确认, 部分完成, 明确失败和结果未知,
为事件回复去重提供依据, 不将本地调用结束等同于平台确认
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, Sequence


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
    if len(receipts) == 1:
        # 单段发送不引入位置语义, 保留原始回执形状 (failed_index 不补 0)
        only = receipts[0]
        if only.status == "success":
            return SendReceipt("success", only.message_ids)
        status = "unknown" if only.status == "unknown" else "partial" if only.message_ids else "failed"
        return SendReceipt(status, only.message_ids, only.failed_index, only.reason)
    ids = tuple(message_id for receipt in receipts for message_id in receipt.message_ids)
    index = 0
    for receipt in receipts:
        if receipt.status != "success":
            status = "unknown" if receipt.status == "unknown" else "partial" if ids else "failed"
            return SendReceipt(status, ids, index + (receipt.failed_index or 0), receipt.reason)
        index += max(1, len(receipt.message_ids))
    return SendReceipt("success", ids) if receipts else SendReceipt("failed", reason="empty_message")


class SendAttemptRecorder(Protocol):
    """发送尝试持久化记录器, 由管线层状态存储实现, 平台层只依赖该结构协议"""

    @property
    def degraded(self) -> bool:
        """存储降级时调用方跳过记录, 发送功能不受影响"""
        ...

    def record_send_attempt(
        self,
        turn_id: str,
        adapter_id: str,
        target: str,
        request_id: str,
        segments: list[dict[str, Any]],
        purpose: str = "business",
    ) -> bool:
        """在发送 I/O 之前持久化计划 (段状态 planned), 返回是否已落盘"""
        ...

    def mark_segment_submitted(self, turn_id: str, index: int) -> bool:
        """该段 I/O 之前把它从 planned 推进 submitted, 返回是否已落盘"""
        ...

    def record_segment_result(self, turn_id: str, index: int, status: str, *, advance_to: int | None = None) -> bool:
        """段结果确认后立即落盘该段状态, 并可同时把下一段推进 submitted"""
        ...

    def complete_send_attempt(
        self, turn_id: str, status: str, detail: str = "", untracked: Sequence[int] = (),
    ) -> bool:
        """整轮收尾: 未尝试段标 skipped 并按段证据落终态 (sent/partial/failed/unknown)"""
        ...
