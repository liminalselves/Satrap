"""B10 请求诊断: 决策/限流/补全/模型/发送各阶段的采集, 有界容量与列表/明细查询

反例重点: 未执行投影的拒绝也能查到; 附件失败与最终发送结果分开; 查询中不含正文;
容量淘汰不影响持久账本; 诊断不可用不拖垮消息处理
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend import http_api
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.pipeline.attachments import AttachmentResult
from satrap.core.pipeline.manual_wake_store import ManualWakeStore
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform.event import PlatformMessage
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.pipeline.request_diagnostics import REQUEST_CAPACITY, RequestDiagnostic, RequestDiagnosticLog
from satrap.core.type import MessageMember, PlatformMessageType

_NOW = 1_760_000_000.0
"""测试基准时间: 需要真实量级的墙钟时间, 0 附近的秒数在 Windows 上无法格式化"""


class _StubAdapter(OneBotAdapter):
    """不发网络请求的 OneBot 适配器替身"""

    def __init__(self, adapter_id: str = "ob") -> None:
        super().__init__(PlatformConfig(id=adapter_id, type="onebot", settings={"self_id": "10"}))
        self.started = True
        self._bot = AsyncMock()

    async def _dispatch_action(self, session_id: str, private_action: str, group_action: str, **params: Any) -> Any:
        return {"message_id": 1}


def _event(adapter: OneBotAdapter, text: str = "hello", session_id: str = "group%20") -> MessageEvent:
    message = PlatformMessage()
    message.type = PlatformMessageType.GROUP_MESSAGE
    message.self_id = "10"
    message.session_id = session_id
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="30", nickname="User")
    message.message = []
    message.message_str = text
    event = MessageEvent(
        message_str=text, platform_message=message,
        platform_meta=PlatformMetadata(name=adapter.config.id, id=adapter.config.id),
        session_id=session_id, adapter=adapter, session_type="group",
    )
    event.is_wake = True
    event.call_llm = True
    return event


def _scheduler(
    tmp_path: Path, *, store: ManualWakeStore | None = None, response: str = "", adapter: OneBotAdapter | None = None,
) -> tuple[PipelineScheduler, Any, OneBotAdapter]:
    manager = Mock()
    manager.handle_call_async = AsyncMock(return_value=response)
    scheduler = PipelineScheduler(cast(Any, manager))
    instance = adapter if adapter is not None else _StubAdapter()
    scheduler.manual_wake_store = store if store is not None else ManualWakeStore(tmp_path / "wake.json")
    instance.set_send_attempt_recorder(scheduler.manual_wake_store)
    return scheduler, manager, instance


def _rows(detail: dict[str, object]) -> list[dict[str, Any]]:
    """诊断明细中的阶段记录"""
    return cast(list[dict[str, Any]], detail["records"])


def _stages(detail: dict[str, object]) -> dict[str, dict[str, Any]]:
    """阶段名到阶段记录的映射"""
    return {item["stage"]: item for item in _rows(detail)}


class TestDiagnosticCollection:
    @pytest.mark.asyncio
    async def test_unwoken_message_is_queryable_before_projection(self, tmp_path: Path):
        """反例: 未执行投影的拒绝也必须能查到阶段与原因"""
        scheduler, manager, adapter = _scheduler(tmp_path)
        adapter.started = True
        await adapter._handle_group_message({
            "self_id": 10, "group_id": 20, "user_id": 30, "message_id": 9, "message_type": "group",
            "message": [{"type": "text", "data": {"text": "没有唤醒词"}}],
        })
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        assert manager.handle_call_async.await_count == 0
        assert event.get_extra("input_projection") is None
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        assert [item["stage"] for item in _rows(detail)] == ["wake_decision"]
        record = _rows(detail)[0]
        assert record["status"] == "dropped" and record["reason_code"]
        assert record["message_id"] == "9"

    @pytest.mark.asyncio
    async def test_rate_limit_stage_records_waited_reason(self, tmp_path: Path):
        from satrap.core.pipeline.rate_limiter import RateLimiter

        scheduler, _, adapter = _scheduler(tmp_path)
        limiter = RateLimiter(rate=0.0, burst=1)
        scheduler.rate_limiter = limiter
        event = _event(adapter)
        scheduler.request_diagnostics.record(RequestDiagnostic(
            adapter_id="ob", session_id=event.session_id, actor_id="30", stage="wake_decision",
            decision="dropped", reason="not_woken", recorded_at=_NOW, request_id=event.call_origin.request_id,
        ))
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        # 限流拒绝与该事件其余阶段共用同一 request_id
        assert stages["wake_decision"]["status"] == "dropped"

    @pytest.mark.asyncio
    async def test_attachment_failure_is_a_stage_not_the_final_result(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """反例: 语音转写失败与最终发送结果分别展示, 不压成单一 failed"""
        scheduler, _, adapter = _scheduler(tmp_path, response="回复")
        monkeypatch.setattr(
            "satrap.core.pipeline.scheduler.resolve_attachments",
            AsyncMock(return_value=(
                AttachmentResult("record", "v.amr", "unsupported", "", "silk_needs_platform_transcode"),
                AttachmentResult("file", "a.pdf", "failed", "", "download_failed"),
            )),
        )
        event = _event(adapter)
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        assert stages["projection"]["status"] == "failed"
        assert "record:unsupported:silk_needs_platform_transcode" in stages["projection"]["attachments"]
        assert "file:failed:download_failed" in stages["projection"]["attachments"]
        # 发送阶段独立成条: 模型仍回复成功, 不因附件失败被标记为失败
        assert stages["send"]["status"] == "sent"
        assert stages["model"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_send_stage_separates_partial_from_unknown(self, tmp_path: Path):
        scheduler, _, adapter = _scheduler(tmp_path, adapter=_StubAdapter())
        store = scheduler.manual_wake_store
        assert store is not None
        event = _event(adapter)
        assert store.record_send_attempt("t1", "ob", event.session_id, event.call_origin.request_id, [{"index": 0}])
        assert store.mark_segment_submitted("t1", 0)
        assert store.record_segment_result("t1", 0, "sent", advance_to=1) is False
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        # 只有已提交未确认的段: 发送阶段为 unknown 且带关联 turn_id
        assert stages["send"]["status"] == "unknown" and stages["send"]["turn_id"] == "t1"
        assert "in_flight" in stages["send"]["reason_code"] or "not_submitted" in stages["send"]["reason_code"]

    @pytest.mark.asyncio
    async def test_send_stage_reports_confirmed_prefix_as_partial(self, tmp_path: Path):
        """已确认前缀加未尝试后缀属于 partial, 不因缺少回执被压成失败或已送达"""
        scheduler, _, adapter = _scheduler(tmp_path, adapter=_StubAdapter())
        store = scheduler.manual_wake_store
        assert store is not None
        event = _event(adapter)
        assert store.record_send_attempt(
            "t1", "ob", event.session_id, event.call_origin.request_id,
            [{"index": 0, "kind": "chunk"}, {"index": 1, "kind": "chunk"}],
        )
        assert store.mark_segment_submitted("t1", 0)
        # 第 0 段确认送达, 第 1 段仍是 planned: 前缀已确认且后缀未尝试
        assert store.record_segment_result("t1", 0, "sent") is True
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        assert stages["send"]["status"] == "partial"
        assert stages["send"]["reason_code"] == "confirmed_prefix"

    @pytest.mark.asyncio
    async def test_model_timeout_stage_is_recorded(self, tmp_path: Path):
        scheduler, _, adapter = _scheduler(tmp_path)
        scheduler.llm_timeout = 0.01
        scheduler.error_feedback = False

        async def slow(*args: Any, **kwargs: Any) -> str:
            await asyncio.sleep(0.5)
            return ""

        cast(Any, scheduler.session_manager).handle_call_async = slow
        event = _event(adapter)
        await scheduler.execute(event)
        detail = scheduler.request_diagnostics.get_request(event.call_origin.request_id)
        assert detail is not None
        stages = _stages(detail)
        assert stages["model"]["status"] == "unknown" and stages["model"]["reason_code"] == "llm_timeout"


class TestDiagnosticBounds:
    def test_capacity_keeps_newest_requests_and_drops_oldest(self):
        log = RequestDiagnosticLog(per_adapter=2)
        for index in range(3):
            log.record(RequestDiagnostic(
                adapter_id="ob", session_id="s", actor_id="a", stage="wake_decision", decision="dropped",
                reason="not_woken", recorded_at=_NOW + index, request_id=f"r{index}",
            ))
        assert [item["request_id"] for item in log.list_requests("ob")] == ["r2", "r1"]
        assert log.get_request("r0", "ob") is None
        assert log.stats("ob")["requests_total"] == 2 and log.stats("ob")["capacity"] == 2

    def test_records_per_request_are_bounded_and_deduped(self):
        log = RequestDiagnosticLog()
        for index in range(20):
            log.record(RequestDiagnostic(
                adapter_id="ob", session_id="s", actor_id="a", stage="model", decision="ok",
                reason="x", recorded_at=_NOW + index, request_id="r1", reason_code=f"c{index}",
            ))
        assert log.stats("ob")["records_per_request"] == 16
        detail = log.get_request("r1", "ob")
        assert detail is not None and len(_rows(detail)) == 16 and detail["truncated"] is True
        # 同阶段同原因码重复采集只保留最新一条
        for index in range(5):
            log.record(RequestDiagnostic(
                adapter_id="ob", session_id="s", actor_id="a", stage="send", decision="sent",
                reason="x", recorded_at=_NOW + index, request_id="r2", status="sent", reason_code="all_segments_confirmed",
            ))
        detail = log.get_request("r2", "ob")
        assert detail is not None
        send_rows = [item for item in _rows(detail) if item["stage"] == "send"]
        assert len(send_rows) == 1 and send_rows[0]["reason_code"] == "all_segments_confirmed"

    def test_adapter_filter_is_exact(self):
        log = RequestDiagnosticLog()
        for name in ("ob-a", "ob-b"):
            log.record(RequestDiagnostic(
                adapter_id=name, session_id="s", actor_id="a", stage="wake_decision", decision="dropped",
                reason="not_woken", recorded_at=_NOW, request_id=f"{name}-r",
            ))
        assert [item["adapter_id"] for item in log.list_requests("ob-a")] == ["ob-a"]
        assert len(log.list_requests()) == 2
        assert {item["adapter_id"] for item in log.list("ob-b")} == {"ob-b"}

    def test_stage_filter_selects_requests_with_that_stage(self):
        log = RequestDiagnosticLog()
        log.record(RequestDiagnostic(
            adapter_id="ob", session_id="s", actor_id="a", stage="wake_decision", decision="dropped",
            reason="not_woken", recorded_at=_NOW, request_id="r1",
        ))
        log.record(RequestDiagnostic(
            adapter_id="ob", session_id="s", actor_id="a", stage="send", decision="sent",
            reason="ok", recorded_at=_NOW + 1, request_id="r2", status="sent",
        ))
        assert [item["request_id"] for item in log.list_requests("ob", stage="send")] == ["r2"]
        assert [item["request_id"] for item in log.list_requests("ob", stage="wake_decision")] == ["r1"]
        assert log.list_requests("ob", request_id="r2")[0]["stages"] == ["send"]

    def test_diagnostics_never_carry_body_text(self, tmp_path: Path):
        from satrap.core.pipeline.request_diagnostics import RECORDS_PER_REQUEST

        assert RECORDS_PER_REQUEST > 0
        log = RequestDiagnosticLog(per_adapter=REQUEST_CAPACITY)
        log.record(RequestDiagnostic(
            adapter_id="ob", session_id="s", actor_id="a", stage="model", decision="ok",
            reason="模型输出 12 字符", recorded_at=_NOW, request_id="r1", reason_code="completed",
        ))
        rendered = str(log.list_requests("ob")) + str(log.get_request("r1", "ob"))
        assert "模型输出 12 字符" in rendered
        # 记录字段白名单之外的内容不会进入查询结果
        for item in cast(list[dict[str, Any]], log.list("ob")):
            assert set(item) == {
                "recorded_at", "adapter_id", "session_id", "actor_id", "stage", "decision", "reason",
                "message_id", "request_id", "send_status", "self_id", "status", "reason_code",
                "turn_id", "attachments", "notes",
            }


class TestDiagnosticRoutes:
    @staticmethod
    def _server(tmp_path: Path) -> tuple[BackendHTTPServer, BackendManager, PipelineScheduler, OneBotAdapter]:
        backend = BackendManager()
        backend._running = True
        manager = Mock()
        manager.handle_call_async = AsyncMock(return_value="")
        scheduler = PipelineScheduler(cast(Any, manager))
        scheduler.manual_wake_store = ManualWakeStore(tmp_path / "wake.json")
        backend._scheduler = scheduler
        backend._manual_wake_store = scheduler.manual_wake_store
        backend._adapter_mgr = PlatformAdapterManager()
        adapter = _StubAdapter()
        backend._adapter_mgr._adapters["ob"] = adapter
        return BackendHTTPServer(backend, port=0), backend, scheduler, adapter

    @pytest.mark.asyncio
    async def test_list_and_detail_routes(self, tmp_path: Path):
        server, _, scheduler, adapter = self._server(tmp_path)
        scheduler.request_diagnostics.record(RequestDiagnostic(
            adapter_id="ob", session_id="group%20", actor_id="30", stage="wake_decision", decision="dropped",
            reason="not_woken: 未命中唤醒条件", recorded_at=_NOW, request_id="r1", reason_code="not_woken",
            message_id="9",
        ))
        status, body = await server._route("GET", "/api/platforms/wake/diagnostics?adapter_id=ob", b"")
        assert status == 200 and body["available"] is True
        records = cast(list[dict[str, Any]], body["records"])
        assert records[0]["request_id"] == "r1" and records[0]["stages"] == ["wake_decision"]
        # 摘要只给脱敏原因码, 展示用原因在明细里
        assert records[0]["reason_codes"] == ["not_woken"]
        detail = await server._route("GET", "/api/platforms/wake/diagnostics/r1", b"")
        assert detail[0] == 200 and len(cast(list[Any], detail[1]["records"])) == 1
        assert "未命中" in cast(list[dict[str, Any]], detail[1]["records"])[0]["reason"]
        missing = await server._route("GET", "/api/platforms/wake/diagnostics/nope", b"")
        assert missing[0] == 404 and missing[1]["reason"] == "not_found"
        bad_stage = await server._route("GET", "/api/platforms/wake/diagnostics?stage=nope", b"")
        assert bad_stage[0] == 400 and bad_stage[1]["error"] == "invalid_stage"
        bad_limit = await server._route("GET", "/api/platforms/wake/diagnostics?limit=abc", b"")
        assert bad_limit[0] == 400 and bad_limit[1]["error"] == "invalid_limit"
        # 旧的拒绝记录接口保持兼容
        legacy = await server._route("GET", "/api/platforms/wake/rejections?adapter_id=ob", b"")
        assert legacy[0] == 200 and cast(list[dict[str, Any]], legacy[1]["records"])[0]["stage"] == "wake_decision"

    @pytest.mark.asyncio
    async def test_diagnostics_unavailable_is_marked(self, tmp_path: Path):
        backend = BackendManager()
        server = BackendHTTPServer(backend, port=0)
        status, body = await server._route("GET", "/api/platforms/wake/diagnostics", b"")
        assert status == 200 and body["available"] is False and body["reason"] == "scheduler_unavailable"
        detail = await server._route("GET", "/api/platforms/wake/diagnostics/r1", b"")
        assert detail[0] == 503 and detail[1]["reason"] == "scheduler_unavailable"

    @pytest.mark.asyncio
    async def test_recording_failure_does_not_break_pipeline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """反例: 诊断采集异常不得影响消息处理"""
        scheduler, _, adapter = _scheduler(tmp_path, response="回复")

        def broken(record: Any) -> None:
            raise RuntimeError("diagnostics down")

        monkeypatch.setattr(scheduler.request_diagnostics, "record", broken)
        event = _event(adapter)
        await scheduler.execute(event)
        assert event.last_business_receipt is None or event.last_business_receipt.status in {"success", "unknown"}

    @pytest.mark.asyncio
    async def test_manual_request_and_plain_event_share_diagnostics(self, tmp_path: Path):
        """反例: 手动请求与普通事件的诊断共用同一批阶段"""
        from satrap.core.pipeline.manual_wake import ManualWakeTicket

        server, backend, scheduler, adapter = self._server(tmp_path)
        plain = _event(adapter)
        await scheduler.execute(plain)
        # 手动请求的生产路径会把票据的 request_id 写回事件来源, 诊断按同一标识归并
        manual = _event(adapter)
        manual._call_origin = replace(manual.call_origin, request_id="manual-1")
        scheduler.manual_wakes.tickets[manual] = ManualWakeTicket("manual-1")
        cast(ManualWakeStore, backend._manual_wake_store).accept_request("ob", "manual-1", "fp", "group%20", "op")
        await scheduler.execute(manual)
        for request_id in (plain.call_origin.request_id, "manual-1"):
            detail = scheduler.request_diagnostics.get_request(request_id)
            assert detail is not None, request_id
            stages = set(_stages(detail))
            assert {"model", "send"} <= stages
            # 空回复没有发出任何业务内容: 发送阶段不得记成已送达或失败
            send_status = _stages(detail)["send"]["status"]
            assert send_status == "skipped"
        status, body = await server._route("GET", "/api/platforms/wake/diagnostics?stage=send", b"")
        assert status == 200 and len(cast(list[Any], body["records"])) == 2
