"""业务链路场景与集中边界契约"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import asyncio
import pytest
from satrap.core.platform.notices import NoticePayload, PlatformEventHub, build_onebot_notice, notice_attachment
from satrap.core.platform.notices import set_current_hub
from satrap.core.components.message import File
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig, PlatformEvent
from unittest.mock import AsyncMock
import logging
from aiocqhttp.exceptions import ActionFailed
from satrap.core.platform.onebot.admin import AdminActionRejected, AdminActionUnconfirmed, UnsupportedAdminAction
from satrap.core.platform.onebot.request_registry import flag_digest
from satrap.core.platform.event import MessageChain
from typing import cast
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.log.reader import read_log_increment
from satrap.core.platform import EventDispatcher

@pytest.fixture()
def _reset_current_hub():
    """每个用例前后复位进程级通知 hub, 隔离其他测试文件的 BackendManager 残留"""
    set_current_hub(None)
    yield
    set_current_hub(None)

def raw_notice(**extra: object) -> dict[str, Any]:
    return {"post_type": "notice", "notice_type": "group_increase", "sub_type": "approve", "self_id": 10000,
            "group_id": 456, "user_id": 321, "operator_id": 111, "time": 1700000000, **extra}

@pytest.mark.usefixtures('_reset_current_hub')
def test_build_onebot_notice_narrows_fields_and_rejects_foreign_account():
    payload = build_onebot_notice(raw_notice(file={"id": "f1", "name": "a.txt", "size": 3, "secret": "x"}), "10000")
    assert payload is not None
    assert (payload.category, payload.kind, payload.sub_type, payload.group_id, payload.operator_id) == ("notice", "group_increase", "approve", "456", "111")
    assert payload.file == {"id": "f1", "name": "a.txt", "size": 3}
    assert build_onebot_notice(raw_notice(self_id=2), "10000") is None
    assert build_onebot_notice({"post_type": "message"}, "10000") is None
    request = build_onebot_notice({"post_type": "request", "request_type": "friend", "user_id": 5, "flag": "abc", "comment": "hi"}, "")
    assert request is not None and (request.kind, request.flag, request.comment) == ("friend", "abc", "hi")

@pytest.mark.usefixtures('_reset_current_hub')
@pytest.mark.asyncio
async def test_hub_dispatches_once_per_event_and_deduplicates(monkeypatch: pytest.MonkeyPatch):
    hub = PlatformEventHub(dedup_ttl=10)
    seen: list[str] = []

    async def on_increase(event: PlatformEvent) -> None:
        seen.append(f"exact:{event.group_id}")

    def on_any(event: PlatformEvent) -> None:
        seen.append(f"any:{event.event_type}")

    unsubscribe = hub.subscribe("notice.group_increase", on_increase)
    hub.subscribe("*", on_any)
    payload = build_onebot_notice(raw_notice(), "10000")
    event = PlatformEvent("ob", "onebot", "notice.group_increase", group_id="456", extras={"payload": payload})
    hub(event)
    hub(event)
    await asyncio.sleep(0.02)
    assert seen == ["exact:456", "any:notice.group_increase"]
    assert hub.stats["duplicate"] == 1 and hub.stats["dispatched"] == 2
    unsubscribe()
    unsubscribe()
    other = PlatformEvent("ob", "onebot", "notice.group_increase", group_id="457", extras={"payload": build_onebot_notice(raw_notice(group_id=457), "10000")})
    hub(other)
    await asyncio.sleep(0.02)
    assert seen[-1] == "any:notice.group_increase" and len(seen) == 3

@pytest.mark.usefixtures('_reset_current_hub')
@pytest.mark.asyncio
async def test_hub_without_subscribers_counts_and_handler_errors_are_isolated():
    hub = PlatformEventHub()
    hub(PlatformEvent("ob", "onebot", "notice.group_recall"))
    assert hub.stats["unsubscribed"] == 1

    def boom(event: PlatformEvent) -> None:
        raise RuntimeError("bad")

    calls: list[str] = []
    hub.subscribe("request.friend", boom)
    hub.subscribe("request.friend", lambda event: calls.append(event.user_id))
    hub(PlatformEvent("ob", "onebot", "request.friend", user_id="5"))
    await asyncio.sleep(0.02)
    assert calls == ["5"] and hub.stats["failed"] == 1 and hub.stats["dispatched"] == 1

@pytest.mark.usefixtures('_reset_current_hub')
@pytest.mark.asyncio
async def test_hub_inflight_bound_and_close_cancels_tasks():
    hub = PlatformEventHub(max_inflight=2)
    gate = asyncio.Event()

    async def slow(event: PlatformEvent) -> None:
        await gate.wait()

    hub.subscribe("*", slow)
    for i in range(3):
        hub(PlatformEvent("ob", "onebot", "notice.x", extras={"payload": NoticePayload("notice", "x", message_id=str(i))}))
    await asyncio.sleep(0)
    assert hub.stats["dropped"] == 1 and len(hub._tasks) == 2
    await hub.close()
    assert not hub._tasks
    hub(PlatformEvent("ob", "onebot", "notice.x"))
    assert hub.stats["dropped"] == 2

@pytest.mark.usefixtures('_reset_current_hub')
@pytest.mark.asyncio
async def test_onebot_emits_typed_platform_event_and_respects_filters():
    received: list[PlatformEvent] = []
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}), event_handler=received.append)
    adapter.bot_self_id = "10000"
    await adapter._handle_notice(raw_notice())
    assert len(received) == 1
    event = received[0]
    assert event.event_type == "notice.group_increase" and event.session_id == "group%456" and event.group_id == "456"
    assert isinstance(event.extras["payload"], NoticePayload)
    assert adapter._event_queue.empty()

    await adapter._handle_request({"post_type": "request", "request_type": "group", "sub_type": "add", "self_id": 10000, "group_id": 456, "user_id": 7, "flag": "f"})
    assert received[-1].event_type == "request.group"

    adapter.config.settings["group_whitelist"] = ["789"]
    await adapter._handle_notice(raw_notice())
    assert len(received) == 2
    await adapter._handle_request({"post_type": "request", "request_type": "friend", "self_id": 10000, "user_id": 7, "flag": "f"})
    assert received[-1].event_type == "request.friend" and received[-1].session_id == "private%7"

    adapter.config.settings.pop("group_whitelist")
    adapter.config.settings["notice_types"] = ["notice.group_recall", "request"]
    await adapter._handle_notice(raw_notice())
    assert len(received) == 3
    await adapter._handle_notice(raw_notice(notice_type="group_recall", message_id=9))
    assert received[-1].event_type == "notice.group_recall"
    await adapter._handle_notice(raw_notice(self_id=2))
    assert adapter.get_stats()["ingress"]["account"] == 1

@pytest.mark.usefixtures('_reset_current_hub')
@pytest.mark.parametrize("value", [["message"], ["notice.Group"], "notice", ["notice"] * 65])
def test_notice_types_validation(value: object):
    with pytest.raises(ValueError, match="notice_types"):
        validate_wake_policy({"notice_types": value})


@pytest.mark.usefixtures('_reset_current_hub')
@pytest.mark.asyncio
async def test_backend_registers_hub_for_plugins_and_reports_stats(tmp_path: Path):
    import json
    from satrap.core.platform.notices import current_hub
    from satrap.core.backend.BackendManager import BackendManager
    from satrap.core.config.loader import ConfigLoader

    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps({"data_root": str(tmp_path / "data"), "platforms": [
        {"id": "bot", "type": "onebot", "settings": {"port": 0}, "enable": False}]}), encoding="utf-8")
    manager = BackendManager(ConfigLoader.from_json(config_path))
    assert current_hub() is None
    await manager._init_platforms()
    try:
        assert current_hub() is manager.platform_events
        assert manager._adapter_mgr is not None
        adapter = manager._adapter_mgr.get_adapter("bot")
        assert adapter is not None and adapter.event_handler is manager.platform_events
        health = await manager.health()
        assert health["platform_events"]["received"] == 0
    finally:
        await manager.stop()
    assert current_hub() is None
    closed_hub = manager.platform_events
    assert closed_hub.closed
    await manager._init_platforms()
    try:
        assert manager.platform_events is not closed_hub and not manager.platform_events.closed
        assert current_hub() is manager.platform_events
    finally:
        await manager.stop()

def upload_notice(**extra: object) -> dict[str, Any]:
    return {"post_type": "notice", "notice_type": "group_upload", "self_id": 10000, "group_id": 456,
            "user_id": 321, "time": 1700000000,
            "file": {"id": "/file/abc", "name": "报表.xlsx", "size": 2048, "busid": 102, "url": "https://dl.example.com/f"},
            **extra}

@pytest.mark.usefixtures('_reset_current_hub')
def test_notice_attachment_normalizes_group_upload_only():
    payload = build_onebot_notice(upload_notice(), "10000")
    assert payload is not None
    attachment = notice_attachment(payload)
    assert isinstance(attachment, File)
    assert attachment.name == "报表.xlsx" and attachment.file_ == "/file/abc"
    assert attachment.url == "https://dl.example.com/f"
    assert payload.file.get("size") == 2048 and payload.file.get("busid") == 102

    other = build_onebot_notice(raw_notice(), "10000")
    assert other is not None and notice_attachment(other) is None
    request = build_onebot_notice({"post_type": "request", "request_type": "friend", "user_id": 5, "flag": "f"}, "10000")
    assert request is not None and notice_attachment(request) is None

@pytest.mark.usefixtures('_reset_current_hub')
def test_notice_attachment_requires_remote_handle_and_tolerates_missing_fields():
    no_file = build_onebot_notice(upload_notice(file=None), "10000")
    assert no_file is not None and no_file.file == {}
    assert notice_attachment(no_file) is None
    no_handle = build_onebot_notice(upload_notice(file={"name": "x.txt", "size": 1}), "10000")
    assert no_handle is not None and notice_attachment(no_handle) is None
    id_only = build_onebot_notice(upload_notice(file={"id": 42}), "10000")
    assert id_only is not None
    attachment = notice_attachment(id_only)
    assert attachment is not None and attachment.name == "file" and attachment.file_ == "42" and not attachment.url

@pytest.mark.usefixtures('_reset_current_hub')
@pytest.mark.asyncio
async def test_group_upload_notice_carries_attachment_component():
    received: list[PlatformEvent] = []
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}), event_handler=received.append)
    adapter.bot_self_id = "10000"
    await adapter._handle_notice(upload_notice())
    assert len(received) == 1
    event = received[0]
    assert event.event_type == "notice.group_upload" and event.session_id == "group%456"
    attachment = event.extras.get("attachment")
    assert isinstance(attachment, File) and attachment.name == "报表.xlsx"
    assert adapter._event_queue.empty()

    await adapter._handle_notice(raw_notice())
    assert "attachment" not in received[-1].extras

@pytest.mark.usefixtures('_reset_current_hub')
@pytest.mark.asyncio
async def test_dispatch_done_callback_retrieves_base_exception(caplog: pytest.LogCaptureFixture):
    """处理任务以 BaseException 结束时由 done callback 取回并记录, 不留 GC 警告"""
    import logging

    hub = PlatformEventHub()

    async def fail() -> None:
        raise RuntimeError("boom")

    task = asyncio.get_running_loop().create_task(fail())
    while not task.done():
        await asyncio.sleep(0)
    with caplog.at_level(logging.ERROR):
        hub._on_dispatch_done(task)
    assert any("处理任务异常退出" in record.getMessage() and "RuntimeError" in record.getMessage() for record in caplog.records)
    assert task not in hub._tasks

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

    async def attempt() -> None:
        """驱动一次失败调用产生审计日志, 具体归一化异常类型由 onebot_admin 侧断言"""
        try:
            await admin.kick_group_member("456", "123")
        except (AdminActionRejected, UnsupportedAdminAction, AdminActionUnconfirmed):
            pass

    with caplog.at_level(logging.DEBUG):
        adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 1200})
        await attempt()
        adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 1404})
        await attempt()
        adapter._bot.set_group_kick.side_effect = RuntimeError("net")
        await attempt()
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
async def test_send_failures_are_logged_with_reason(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
):
    """E5: 平台拒绝, 结果未知与三个发送兜底分支都保留原因码, 异常类型与目标会话"""
    from satrap.core.components import File, Node, Plain

    async def _boom_chunk(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("chunk boom")

    async def _boom_forward(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("forward boom")

    async def _boom_file(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("file boom")

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

    monkeypatch.setattr(adapter, "_send_chunk", _boom_chunk)
    monkeypatch.setattr(adapter, "_send_forward", _boom_forward)
    monkeypatch.setattr(adapter, "_send_file", _boom_file)
    caplog.clear()
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

class _RestartingDispatcher:
    """首次失败后保持运行的事件分发器替身"""

    def __init__(self) -> None:
        """初始化调用计数与重启通知"""
        self.calls = 0
        self.restarted = asyncio.Event()
        self.keep_running = asyncio.Event()

    async def dispatch_loop(self) -> None:
        """首次调用抛错, 第二次调用等待测试结束"""
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("dispatch failed")
        self.restarted.set()
        await self.keep_running.wait()

@pytest.mark.asyncio
async def test_dispatch_supervisor_restarts_failed_loop_and_reports_health() -> None:
    """事件分发异常后应自动重启并在健康状态中保留故障记录"""
    backend = BackendManager()
    dispatcher = _RestartingDispatcher()
    backend._dispatcher = cast(EventDispatcher, dispatcher)
    backend._dispatch_restart_base_delay = 0
    backend._running = True
    backend._dispatch_task = asyncio.create_task(backend._dispatch_loop())

    await asyncio.wait_for(dispatcher.restarted.wait(), timeout=1)
    health = await backend.health()

    assert dispatcher.calls == 2
    assert health["healthy"] is True
    assert health["dispatch"] == {
        "status": "running",
        "healthy": True,
        "restart_count": 1,
        "last_error": "dispatch failed",
    }

    backend._running = False
    backend._dispatch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await backend._dispatch_task

@pytest.mark.asyncio
async def test_health_endpoint_returns_503_while_dispatch_is_restarting() -> None:
    """事件分发处于重启退避时健康端点应明确返回降级状态"""
    backend = BackendManager()
    backend._running = True
    backend._dispatcher = cast(EventDispatcher, _RestartingDispatcher())
    backend._dispatch_state = "restarting"
    backend._dispatch_task = asyncio.current_task()
    server = BackendHTTPServer(backend)

    status, payload = await server._route("GET", "/api/health", b"")

    assert status == 503
    assert payload["running"] is True
    assert payload["healthy"] is False
    assert payload["dispatch"]["status"] == "restarting"

def test_read_log_increment_keeps_cached_lines_when_no_new_data(tmp_path: Path):
    """没有新增日志时仍显示已有缓冲内容"""
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("first\nsecond\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 0, [], 10, paused=False)
    assert display == ["first\n", "second\n"]

    position, cached, display = read_log_increment(log_file, position, cached, 10, paused=False)
    assert display == ["first\n", "second\n"]

def test_read_log_increment_paused_does_not_advance_position(tmp_path: Path):
    """暂停时不推进读取位置, 也不清空缓冲"""
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("first\nsecond\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 0, ["old\n"], 10, paused=True)

    assert position == 0
    assert cached == ["old\n"]
    assert display == ["old\n"]

def test_read_log_increment_resets_after_truncate(tmp_path: Path):
    """日志截断后应从新文件开头读取"""
    log_file = tmp_path / "satrap.log"
    log_file.write_bytes("new\n".encode("utf-8"))

    position, cached, display = read_log_increment(log_file, 100, ["old\n"], 10, paused=False)

    assert position == len("new\n")
    assert cached == ["new\n"]
    assert display == ["new\n"]
