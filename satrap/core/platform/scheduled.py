"""平台无关的后台固定消息发送契约, 不借用入站事件身份"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
import asyncio
import traceback

from satrap.core.config.platform_messages import MessageScope
from satrap.core.log import logger
from satrap.core.platform.receipt import SendReceipt, combine_receipts


@dataclass(frozen=True)
class ScheduledTarget:
    """宿主在到期时冻结的目标和当前授权, 不保存旧 CallOrigin"""

    scope: MessageScope
    connection_token: tuple[object, int]
    policy_revision: str
    reminder_id: str
    attempt_id: str
    guard: Callable[[], Awaitable[bool]]


class ScheduledRecorder(Protocol):
    """后台发送使用严格证据记录, 任一必要记录失败必须停止后续发送"""

    def plan(self, target: ScheduledTarget, segments: list[dict[str, Any]]) -> bool:
        """
        原子落盘计划并取得任务发送权

        参数:
        - target: 本次冻结的目标
        - segments: 适配器按实际分段生成的有界计划

        返回:
        - 计划与发送权同时落盘后返回 True, 竞争失败返回 False
        """
        ...

    def submitted(self, index: int) -> bool:
        """
        网络提交前持久化该段的提交标记

        参数:
        - index: 实际分段序号

        返回:
        - 标记成功返回 True, 失败时不能发送该段
        """
        ...

    def result(self, index: int, receipt: SendReceipt) -> bool:
        """
        立即保存实际分段回执

        参数:
        - index: 实际分段序号
        - receipt: 平台真实回执

        返回:
        - 证据保存成功返回 True
        """
        ...

    def complete(self, receipt: SendReceipt) -> bool:
        """
        保存整体回执并结算任务, 未尝试段标 skipped

        参数:
        - receipt: 所有已尝试段的真实汇总

        返回:
        - 发送尝试和任务同时结算后返回 True
        """
        ...


async def execute_scheduled_segments(
    target: ScheduledTarget, recorder: ScheduledRecorder,
    plan: list[dict[str, Any]], senders: Sequence[Callable[[], Awaitable[SendReceipt]]],
) -> SendReceipt:
    """
    在已取得平台队列的位置执行严格分段发送, 不自动重试未知结果

    参数:
    - target: 当前账号, 连接和策略组成的冻结目标
    - recorder: 本次任务独享的严格记录器
    - plan: 不保存正文的实际分段计划
    - senders: 与计划一一对应的原生发送操作

    返回:
    - 实际汇总回执, 落盘失败时停止并保留未知依据
    """
    receipts: list[SendReceipt] = []
    claimed = False
    submitting = False
    evidence_uncertain = False
    outcome = SendReceipt("failed", reason="empty_message")
    try:
        if not plan or len(plan) != len(senders):
            return outcome
        if not await target.guard():
            return SendReceipt("failed", reason="scheduled_target_changed")
        claimed = await asyncio.to_thread(recorder.plan, target, plan)
        if not claimed:
            return SendReceipt("failed", reason="scheduled_claim_unavailable")
        for index, sender in enumerate(senders):
            if not await target.guard():
                receipts.append(SendReceipt("failed", reason="scheduled_target_changed"))
                break
            if not await asyncio.to_thread(recorder.submitted, index):
                receipts.append(SendReceipt("failed", reason="scheduled_tracking_unavailable"))
                break
            if not await target.guard():
                receipts.append(SendReceipt("failed", reason="scheduled_target_changed"))
                break
            submitting = True
            receipt = await sender()
            if receipt.status == "success" and not receipt.message_ids:
                receipt = SendReceipt("unknown", reason="scheduled_confirmation_missing")
            submitting = False
            evidence_uncertain = True
            receipts.append(receipt)
            if not await asyncio.to_thread(recorder.result, index, receipt):
                receipts[-1] = SendReceipt("unknown", receipt.message_ids, index, "scheduled_tracking_incomplete")
                break
            evidence_uncertain = False
            if receipt.status != "success":
                break
        outcome = combine_receipts(receipts)
    except asyncio.CancelledError:
        receipts.append(SendReceipt("unknown" if submitting or evidence_uncertain else "failed", reason="scheduled_send_interrupted"))
        outcome = combine_receipts(receipts)
        raise
    except Exception:
        logger.error("[提醒发送] 发送或证据记录失败" + "\n" + traceback.format_exc())
        receipts.append(SendReceipt("unknown" if submitting or evidence_uncertain else "failed", reason="scheduled_send_exception"))
        outcome = combine_receipts(receipts)
    finally:
        if claimed:
            try:
                completed = await asyncio.wait_for(asyncio.shield(asyncio.to_thread(recorder.complete, outcome)), 2)
                if not completed:
                    logger.error(f"[提醒发送] 收尾证据未落盘, 任务={target.reminder_id}")
                    outcome = SendReceipt("unknown", outcome.message_ids, outcome.failed_index, "scheduled_finalize_unavailable")
            except (Exception, asyncio.CancelledError):
                logger.error(f"[提醒发送] 收尾失败, 任务={target.reminder_id}" + "\n" + traceback.format_exc())
                outcome = SendReceipt("unknown", outcome.message_ids, outcome.failed_index, "scheduled_finalize_unavailable")
    return outcome
