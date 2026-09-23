from unittest.mock import AsyncMock
from typing import Any
import asyncio

from aiocqhttp.exceptions import ActionFailed
import pytest

from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.platform.onebot.outbound import OutboundTurns, split_components
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.components import BaseMessageComponent, Plain, Image, At
from satrap.core.platform.event import MessageChain
from satrap.core.platform import PlatformConfig


def _texts(chunks: list[list[BaseMessageComponent]]) -> list[str]:
    return ["".join(c.text for c in chunk if isinstance(c, Plain)) for chunk in chunks]


def test_split_prefers_paragraph_then_newline_and_keeps_all_characters():
    text = "第一段\n\n第二段很长很长\n第三段"
    chunks = split_components([Plain(text)], 8)
    assert "".join(_texts(chunks)) == text
    assert _texts(chunks)[0] == "第一段\n\n"
    assert all(len(t) <= 8 for t in _texts(chunks))


def test_split_hard_cuts_single_long_paragraph():
    chunks = split_components([Plain("a" * 25)], 10)
    assert _texts(chunks) == ["a" * 10, "a" * 10, "a" * 5]


def test_split_keeps_non_text_components_in_order_and_intact():
    image = Image(file="http://x/1.png")
    at = At(qq="1")
    chunks = split_components([Plain("x" * 6), image, Plain("y" * 6), at], 8)
    flat = [c for chunk in chunks for c in chunk]
    assert [type(c) for c in flat] == [Plain, Image, Plain, Plain, At]
    assert flat[1] is image and flat[4] is at
    assert "".join(_texts(chunks)) == "x" * 6 + "y" * 6


def test_split_short_message_is_single_chunk():
    components: list[BaseMessageComponent] = [Plain("短"), Image(file="http://x/1.png")]
    chunks = split_components(components, 2000)
    assert len(chunks) == 1 and chunks[0][1] is components[1]
    assert components == [components[0], components[1]]


def test_split_rejects_non_positive_limit():
    with pytest.raises(ValueError):
        split_components([Plain("a")], 0)


@pytest.mark.parametrize("value", [63, 32001, "2000", 2000.5, True])
def test_message_text_limit_validation_rejects_out_of_range(value: object):
    with pytest.raises(ValueError, match="message_text_limit"):
        validate_wake_policy({"message_text_limit": value})


def test_message_text_limit_validation_accepts_bounds():
    validate_wake_policy({"message_text_limit": 64})
    validate_wake_policy({"message_text_limit": 32000})
    validate_wake_policy({})


def _adapter(**settings: object) -> OneBotAdapter:
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=settings))
    adapter._bot = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_long_message_sends_chunks_in_order_and_aggregates_ids():
    adapter = _adapter(message_text_limit=64)
    adapter._bot.send_group_msg.side_effect = [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]
    receipt = await adapter.send_message("group%456", MessageChain.from_text("甲" * 150))
    assert receipt.status == "success"
    assert receipt.message_ids == ("1", "2", "3")
    sent = [call.kwargs["message"][0]["data"]["text"] for call in adapter._bot.send_group_msg.await_args_list]
    assert "".join(sent) == "甲" * 150
    assert all(len(s) <= 64 for s in sent)


@pytest.mark.asyncio
async def test_empty_message_returns_failed_receipt_without_calling_platform():
    adapter = _adapter()
    receipt = await adapter.send_message("group%456", MessageChain.from_text(""))
    assert receipt.status == "failed" and receipt.reason == "empty_message"
    adapter._bot.send_group_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminate_then_restart_can_send_again():
    adapter = _adapter()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    await adapter.terminate()
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 2}
    receipt = await adapter.send_message("group%456", MessageChain.from_text("回滚后"))
    assert receipt.status == "success" and receipt.message_ids == ("2",)


@pytest.mark.asyncio
async def test_long_message_stops_after_first_failed_chunk_without_resend():
    adapter = _adapter(message_text_limit=64)
    adapter._bot.send_group_msg.side_effect = [{"message_id": 1}, ActionFailed({"retcode": 100}), {"message_id": 3}]
    receipt = await adapter.send_message("group%456", MessageChain.from_text("甲" * 150))
    assert receipt.status == "partial"
    assert receipt.message_ids == ("1",)
    assert receipt.failed_index == 1
    assert adapter._bot.send_group_msg.await_count == 2


@pytest.mark.asyncio
async def test_long_message_unknown_chunk_result_blocks_further_chunks():
    adapter = _adapter(message_text_limit=64)
    adapter._bot.send_group_msg.side_effect = [{"message_id": 1}, None, {"message_id": 3}]
    receipt = await adapter.send_message("group%456", MessageChain.from_text("甲" * 150))
    assert receipt.status == "unknown"
    assert receipt.message_ids == ("1",)
    assert adapter._bot.send_group_msg.await_count == 2


@pytest.mark.asyncio
async def test_sends_to_same_target_are_serialized_and_other_targets_are_not_blocked():
    adapter = _adapter()
    started: list[str] = []
    gate = asyncio.Event()

    async def slow_send(**kwargs: Any) -> dict[str, int]:
        started.append(str(kwargs.get("group_id") or kwargs.get("user_id")))
        if len(started) == 1:
            await gate.wait()
        return {"message_id": len(started)}

    adapter._bot.send_group_msg.side_effect = slow_send
    adapter._bot.send_private_msg.side_effect = slow_send
    first = asyncio.create_task(adapter.send_text("group%456", "一"))
    await asyncio.sleep(0)
    second = asyncio.create_task(adapter.send_text("group%456", "二"))
    other = asyncio.create_task(adapter.send_text("private%789", "三"))
    await asyncio.sleep(0.05)
    assert started == ["456", "789"]
    gate.set()
    receipts = await asyncio.gather(first, second, other)
    assert [r.status for r in receipts] == ["success"] * 3
    assert started == ["456", "789", "456"]


@pytest.mark.asyncio
async def test_outbound_turns_rejects_when_full_and_after_close():
    turns = OutboundTurns()
    release = asyncio.Event()
    entered = asyncio.Event()
    outcomes: list[str] = []

    async def hold(target: str):
        async def operation() -> None:
            entered.set()
            await release.wait()
        try:
            await turns.run(target, operation)
        except RuntimeError:
            outcomes.append("closed")
        outcomes.append("caller_survived")

    holders = [asyncio.create_task(hold(f"t{i}")) for i in range(64)]
    await entered.wait()
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError):
        await turns.run("extra", lambda: asyncio.sleep(0))
    await turns.close()
    await asyncio.gather(*holders)
    # 关闭只取消发送子任务, 调用方任务自身不被取消
    assert outcomes.count("caller_survived") == 64 and outcomes.count("closed") == 64
    with pytest.raises(RuntimeError):
        await turns.run("t0", lambda: asyncio.sleep(0))
    assert not turns.locks


@pytest.mark.asyncio
async def test_outbound_turns_external_cancel_keeps_cancelled_error_and_child_failure_keeps_cause():
    turns = OutboundTurns()
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(10)

    caller = asyncio.create_task(turns.run("t", slow))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not turns.tasks and not turns.locks

    async def failing() -> None:
        raise ValueError("platform said no")

    with pytest.raises(ValueError, match="platform said no"):
        await turns.run("t", failing)


@pytest.mark.asyncio
async def test_cancel_waits_for_cleanup_before_same_target_proceeds():
    """调用方取消后, 同目标新发送必须等旧任务清理完成"""
    turns = OutboundTurns()
    log: list[str] = []
    cleanup_release = asyncio.Event()

    async def first_op() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            log.append("first_cleanup_started")
            await cleanup_release.wait()
            log.append("first_cleanup_done")

    async def second_op() -> None:
        log.append("second_started")

    caller = asyncio.create_task(turns.run("t", first_op))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(turns.run("t", second_op))
    caller.cancel()
    await asyncio.sleep(0.05)
    assert log == ["first_cleanup_started"]
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await second
    assert log == ["first_cleanup_started", "first_cleanup_done", "second_started"]
    assert not turns.tasks and not turns.locks


@pytest.mark.asyncio
async def test_cancel_settle_timeout_keeps_registry_until_child_finishes(monkeypatch: pytest.MonkeyPatch):
    """清理超过有界等待时调用方先退出, 登记保留且同目标不并发"""
    monkeypatch.setattr("satrap.core.platform.onebot.outbound.CANCEL_SETTLE_TIMEOUT", 0.2)
    turns = OutboundTurns()
    log: list[str] = []
    cleanup_release = asyncio.Event()

    async def slow_cleanup() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            log.append("cleanup_started")
            await cleanup_release.wait()
            log.append("cleanup_done")

    async def second_op() -> None:
        log.append("second_started")

    caller = asyncio.create_task(turns.run("t", slow_cleanup))
    await asyncio.sleep(0.05)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    # 调用方已退出, 子任务仍在清理: 登记保留
    assert turns.tasks and turns.locks.get("t") is not None
    second = asyncio.create_task(turns.run("t", second_op))
    await asyncio.sleep(0.1)
    assert "second_started" not in log
    cleanup_release.set()
    await second
    assert log == ["cleanup_started", "cleanup_done", "second_started"]
    assert not turns.tasks and not turns.locks


@pytest.mark.asyncio
async def test_double_cancel_during_settle_keeps_cancel_semantics(monkeypatch: pytest.MonkeyPatch):
    """有界等待期间重复取消不改变取消语义与登记时机"""
    monkeypatch.setattr("satrap.core.platform.onebot.outbound.CANCEL_SETTLE_TIMEOUT", 5.0)
    turns = OutboundTurns()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()

    async def slow_cleanup() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            await cleanup_release.wait()
            cleaned.set()

    caller = asyncio.create_task(turns.run("t", slow_cleanup))
    await asyncio.sleep(0.05)
    caller.cancel()
    await asyncio.sleep(0.05)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    # 重复取消让调用方立即退出, 但登记保持到子任务清理完成
    assert turns.tasks
    assert not cleaned.is_set()
    cleanup_release.set()
    await asyncio.sleep(0.05)
    assert cleaned.is_set()
    assert not turns.tasks


@pytest.mark.asyncio
async def test_close_waits_for_cleaning_task_and_reports_timeout(monkeypatch: pytest.MonkeyPatch):
    """close 覆盖清理中的任务; 超时明确报告未终态"""
    monkeypatch.setattr("satrap.core.platform.onebot.outbound.CANCEL_SETTLE_TIMEOUT", 0.2)
    turns = OutboundTurns()
    cleanup_release = asyncio.Event()

    async def slow_cleanup() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            await cleanup_release.wait()

    caller = asyncio.create_task(turns.run("t", slow_cleanup))
    await asyncio.sleep(0.05)
    closer = asyncio.create_task(turns.close())
    await asyncio.sleep(0.6)
    assert closer.done()
    assert turns.tasks, "超时后未终态任务必须保持登记"
    cleanup_release.set()
    await asyncio.sleep(0.1)
    assert not turns.tasks
    with pytest.raises(RuntimeError, match="已关闭"):
        await caller
