"""平台动作, 发送回执, 手动唤醒与群管理工具的日志覆盖 (审计批次 3)"""
from unittest.mock import AsyncMock
from typing import Any
import asyncio
import logging

from aiocqhttp.exceptions import ActionFailed
import pytest

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.onebot.admin import AdminActionRejected, AdminActionUnconfirmed, UnsupportedAdminAction
from satrap.core.platform.onebot.request_registry import flag_digest
from satrap.core.platform.onebot.outbound import OutboundTurns
from satrap.core.platform.event import MessageChain
from satrap.core.platform import PlatformConfig


def _adapter(**settings: object) -> OneBotAdapter:
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    adapter._bot = AsyncMock()
    return adapter


def _messages(caplog: pytest.LogCaptureFixture, level: int | None = None) -> list[str]:
    """项目 logger 同时挂 std 与 file 两个 handler, 只取其一避免重复计数"""
    return [record.getMessage() for record in caplog.records
            if (level is None or record.levelno == level) and not record.name.endswith("_file")]


@pytest.mark.asyncio
async def test_admin_call_logs_each_failure_kind_and_write_audit(caplog: pytest.LogCaptureFixture):
    adapter = _adapter()
    admin = adapter.admin
    with caplog.at_level(logging.DEBUG):
        adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 1200})
        with pytest.raises(AdminActionRejected):
            await admin.kick_group_member("456", "123")
        adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 1404})
        with pytest.raises(UnsupportedAdminAction):
            await admin.kick_group_member("456", "123")
        adapter._bot.set_group_kick.side_effect = RuntimeError("net")
        with pytest.raises(AdminActionUnconfirmed):
            await admin.kick_group_member("456", "123")
        adapter._bot.set_group_kick.side_effect = None
        adapter._bot.set_group_kick.return_value = {}
        await admin.kick_group_member("456", "123")
        adapter._bot.get_group_info.return_value = {"group_id": 456}
        await admin.get_group_info("456")
    warnings = _messages(caplog, logging.WARNING)
    assert any("set_group_kick 被平台拒绝 retcode=1200" in m for m in warnings)
    assert any("set_group_kick 结果未知: RuntimeError" in m for m in warnings)
    assert any("不支持动作 set_group_kick" in m for m in _messages(caplog, logging.DEBUG))
    infos = _messages(caplog, logging.INFO)
    assert any("写动作已执行 set_group_kick" in m and "'group_id': 456" in m for m in infos)
    assert not any("get_group_info" in m for m in infos)


@pytest.mark.asyncio
async def test_approval_audit_log_records_digest_not_raw_flag(caplog: pytest.LogCaptureFixture):
    """审批与匿名禁言的审计日志不得回显原始 flag, 审批只留与账本同域的摘要"""
    adapter = _adapter(self_id="10000")
    await adapter.request_flags.register("friend", "SYNTHETIC_AUDIT_FLAG", self_id="10000", user_id="99")
    await adapter.request_flags.register("friend", "SYNTHETIC_AUDIT_FLAG_2", self_id="10000", user_id="99")
    with caplog.at_level(logging.INFO):
        await adapter.admin.handle_friend_request("SYNTHETIC_AUDIT_FLAG", True, "")
        adapter._bot.set_friend_add_request.side_effect = ActionFailed({"retcode": 1200})
        with pytest.raises(AdminActionRejected):
            await adapter.admin.handle_friend_request("SYNTHETIC_AUDIT_FLAG_2", True, "")
        adapter._bot.set_group_anonymous_ban.return_value = {}
        await adapter.admin.ban_anonymous("456", "ANONYMOUS_FLAG", 60)
    messages = _messages(caplog)
    digest = flag_digest("friend", "10000", "SYNTHETIC_AUDIT_FLAG")[:8]
    assert any("写动作已执行 set_friend_add_request" in m and f"flag_digest': '{digest}'" in m for m in messages)
    assert any("adapter=ob" in m and "self_id=10000" in m for m in messages)
    assert not any("SYNTHETIC_AUDIT_FLAG" in m for m in messages)
    assert not any("ANONYMOUS_FLAG" in m for m in messages)
    # 匿名禁言不属于账本域, 只保留群范围
    assert any("写动作已执行 set_group_anonymous_ban" in m and "'group_id': 456" in m for m in messages)


@pytest.mark.asyncio
async def test_send_failures_are_logged_with_reason(caplog: pytest.LogCaptureFixture):
    adapter = _adapter()
    adapter._bot.send_group_msg.side_effect = ActionFailed({"retcode": 100})
    with caplog.at_level(logging.WARNING):
        receipt = await adapter.send_message("group%456", MessageChain.from_text("hi"))
    assert receipt.status == "failed" and receipt.reason == "action_rejected"
    assert any("发送失败 action=message session=group%456 reason=action_rejected ActionFailed" in m for m in _messages(caplog))
    adapter._bot.send_group_msg.side_effect = RuntimeError("socket")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        receipt = await adapter.send_message("group%456", MessageChain.from_text("hi"))
    assert receipt.status == "unknown"
    assert any("reason=action_unconfirmed RuntimeError" in m for m in _messages(caplog))


@pytest.mark.asyncio
async def test_send_fallback_branches_keep_error_diagnostics(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch):
    """E5: 三个发送兜底分支都保留原因码, 同时记录异常类型与目标会话"""
    from satrap.core.components import File, Node, Plain
    from satrap.core.platform.receipt import SendReceipt

    async def _boom_chunk(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("chunk boom")

    async def _boom_forward(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("forward boom")

    async def _boom_file(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("file boom")

    adapter = _adapter()
    monkeypatch.setattr(adapter, "_send_chunk", _boom_chunk)
    monkeypatch.setattr(adapter, "_send_forward", _boom_forward)
    monkeypatch.setattr(adapter, "_send_file", _boom_file)
    with caplog.at_level(logging.WARNING):
        chunk = await adapter.send_message("group%456", MessageChain.from_text("hi"))
        forwarded = await adapter.send_message("group%456", MessageChain([Node(Plain("hi"), name="n", uin="10000")]))
        filed = await adapter.send_message("group%456", MessageChain([File(name="x.bin", url="https://example.invalid/x.bin")]))
    assert (chunk.status, chunk.reason) == ("failed", "message_conversion_failed")
    assert (forwarded.status, forwarded.reason) == ("failed", "message_conversion_failed")
    assert (filed.status, filed.reason) == ("failed", "message_conversion_failed")
    messages = _messages(caplog, logging.WARNING)
    assert any("action=message session=group%456 reason=message_conversion_failed RuntimeError" in m for m in messages)
    assert any("action=forward session=group%456 reason=message_conversion_failed ValueError" in m for m in messages)
    assert any("action=file session=group%456 reason=message_conversion_failed RuntimeError" in m for m in messages)


@pytest.mark.asyncio
async def test_client_unavailable_warning_is_rate_limited(caplog: pytest.LogCaptureFixture):
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            await adapter.send_message("group%456", MessageChain.from_text("hi"))
    warnings = [m for m in _messages(caplog, logging.WARNING) if "客户端未初始化" in m]
    debugs = [m for m in _messages(caplog, logging.DEBUG) if "客户端未初始化" in m]
    assert len(warnings) == 1 and len(debugs) == 2


@pytest.mark.asyncio
async def test_self_id_binding_logged_once(caplog: pytest.LogCaptureFixture):
    adapter = _adapter()
    payload: dict[str, Any] = {"self_id": 10000, "user_id": 1, "group_id": 456, "message_id": 1, "message_type": "group",
               "message": [{"type": "text", "data": {"text": "x"}}]}
    with caplog.at_level(logging.INFO):
        await adapter._handle_group_message(payload)
        await adapter._handle_group_message({**payload, "message_id": 2})
    assert sum("OneBot 客户端已连接 adapter=ob self_id=10000" in m for m in _messages(caplog)) == 1


@pytest.mark.asyncio
async def test_lookup_failures_logged_at_debug(caplog: pytest.LogCaptureFixture):
    adapter = _adapter()
    adapter.bot_self_id = "10000"
    adapter._bot.get_msg.side_effect = RuntimeError("gone")
    adapter._bot.get_forward_msg.side_effect = RuntimeError("gone")
    with caplog.at_level(logging.DEBUG):
        assert await adapter.fetch_quoted_message("77", "group%456") is None
        assert await adapter.fetch_forward_message("f1", "group%456") is None
    debugs = _messages(caplog, logging.DEBUG)
    assert any("引用回源失败 message_id=77: RuntimeError" in m for m in debugs)
    assert any("转发回源失败 forward_id=f1: RuntimeError" in m for m in debugs)


@pytest.mark.asyncio
async def test_outbound_turns_logs_full_and_close(caplog: pytest.LogCaptureFixture):
    turns = OutboundTurns()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold() -> None:
        entered.set()
        await release.wait()

    holders = [asyncio.create_task(turns.run(f"t{i}", hold)) for i in range(64)]
    await entered.wait()
    await asyncio.sleep(0)
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError):
            await turns.run("extra", lambda: asyncio.sleep(0))
        await turns.close()
    await asyncio.gather(*holders, return_exceptions=True)
    assert any("发送队列不可用 closed=False inflight=64 target=extra" in m for m in _messages(caplog, logging.WARNING))
    assert any("关闭发送队列, 取消 64 个在途发送" in m for m in _messages(caplog, logging.INFO))
