"""唤醒决策/限流拒绝记录: 环形容量, 决策点与限流点采集, 平台停止清理与查询路由"""
from __future__ import annotations

from unittest.mock import AsyncMock
from typing import Any, cast

import pytest

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.pipeline.request_diagnostics import REJECTION_STAGES, RequestDiagnostic, RequestDiagnosticLog
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.framework.providers import BindingState, BindingStatus


class _RunnableRegistry:
    """绑定判定恒为可运行的会话定义注册表替身"""

    @staticmethod
    def binding_status(*_args: object) -> BindingStatus:
        """
        恒定答复可运行

        返回:
        - BindingStatus: 可运行
        """
        return BindingStatus(BindingState.RUNNABLE)


def _rejection(adapter: str, reason: str, at: float, request_id: str = "r") -> RequestDiagnostic:
    return RequestDiagnostic(
        adapter_id=adapter, session_id="group%20", actor_id="30", stage="wake_decision",
        decision="dropped", reason=reason, recorded_at=at, message_id="1", request_id=request_id,
    )


def runtime(settings: dict[str, object]) -> tuple[BackendManager, OneBotAdapter, Any, PipelineScheduler]:
    """构造带调度器但不连接外部平台的运行时"""
    backend = BackendManager()
    backend._running = True
    manager = AsyncMock()
    manager.provider_registry = _RunnableRegistry()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    backend._scheduler = scheduler
    backend._adapter_mgr = PlatformAdapterManager()
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10", **settings}))
    adapter.started = True
    backend._adapter_mgr._adapters["bot"] = adapter
    return backend, adapter, manager, scheduler


def _scheduler(backend: BackendManager) -> PipelineScheduler:
    scheduler = backend._scheduler
    if scheduler is None:
        raise AssertionError("运行时装配缺失调度器")
    return scheduler


async def _feed(adapter: OneBotAdapter, text: str, message_id: str = "1") -> None:
    """经真实 OneBot 入站构造事件并出队"""
    await adapter._handle_group_message({
        "self_id": 10, "group_id": 20, "user_id": 30, "message_id": message_id,
        "message_type": "group", "message": [{"type": "text", "data": {"text": text}}],
    })


class TestRejectionLog:
    def test_capacity_order_filter_and_clear(self):
        log = RequestDiagnosticLog(per_adapter=3)
        # 容量按请求计数: 每条记录都是独立请求, 同请求的同阶段记录会被合并
        for index in range(5):
            log.record(_rejection("bot", f"r{index}", 1_700_000_000.0 + index, request_id=f"req{index}"))
        log.record(_rejection("other", "x", 1_700_000_100.0))
        records = log.list("bot")
        # 环形容量 3: 只留最新三条, 最新在前
        assert [item["reason"] for item in records] == ["r4", "r3", "r2"]
        assert all(item["adapter_id"] == "bot" for item in records)
        merged = log.list()
        assert merged[0]["adapter_id"] == "other"
        limited = log.list("bot", limit=2)
        assert len(limited) == 2
        clamped = log.list("bot", limit=9999)
        assert len(clamped) == 3
        log.clear_adapter("bot")
        assert log.list("bot") == []
        assert len(log.list("other")) == 1

    def test_record_fields_and_iso_time(self):
        log = RequestDiagnosticLog()
        log.record(_rejection("bot", "no_match: 未命中", 1_700_000_000.0))
        record = log.list()[0]
        assert record["stage"] == "wake_decision" and record["decision"] == "dropped"
        assert record["session_id"] == "group%20" and record["actor_id"] == "30"
        assert record["message_id"] == "1" and record["request_id"] == "r"
        assert "2023" in str(record["recorded_at"])


class TestSchedulerCollection:
    @pytest.mark.asyncio
    async def test_dropped_at_wake_decision_recorded_without_projection(self):
        backend, adapter, manager, scheduler = runtime({})
        await _feed(adapter, "普通的群消息")
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        # 验收关键案例: 消息未执行投影/未调用模型, 仍能查到拒绝原因
        manager.handle_call_async.assert_not_called()
        assert event.get_extra("input_projection") is None
        records = scheduler.request_diagnostics.list("bot")
        assert len(records) == 1
        record = records[0]
        assert record["stage"] == "wake_decision" and record["decision"] == "dropped"
        assert "no_match" in str(record["reason"])
        assert record["session_id"] == "group%20" and record["request_id"]

    @pytest.mark.asyncio
    async def test_woken_message_not_recorded(self):
        backend, adapter, manager, scheduler = runtime({"wake_words": ["助手"]})
        await _feed(adapter, "助手在吗")
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        manager.handle_call_async.assert_called_once()
        # 已唤醒请求不产生拒绝记录, 只留执行阶段诊断
        assert scheduler.request_diagnostics.list("bot", stages=REJECTION_STAGES) == []
        stages = {item["stage"] for item in scheduler.request_diagnostics.list("bot")}
        assert {"model", "send"} <= stages

    @pytest.mark.asyncio
    async def test_rate_limit_rejection_records_wait_and_send_status(self):
        class _DenyAll:
            async def check(self, key: str) -> tuple[bool, float]:
                return False, 7.5

        backend, adapter, manager, scheduler = runtime({"wake_words": ["助手"]})
        scheduler.rate_limiter = cast(RateLimiter, _DenyAll())

        class _OkBot:
            async def send_group_msg(self, **kwargs: Any) -> dict[str, Any]:
                return {"message_id": 9}

        adapter._bot = _OkBot()
        await _feed(adapter, "助手在吗")
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        manager.handle_call_async.assert_not_called()
        records = scheduler.request_diagnostics.list("bot", stages=REJECTION_STAGES)
        assert len(records) == 1
        record = records[0]
        assert record["stage"] == "rate_limit" and "7.5" in str(record["reason"])
        assert record["send_status"] == "success"

    @pytest.mark.asyncio
    async def test_clear_manual_wakes_also_clears_rejections(self):
        backend, adapter, manager, scheduler = runtime({})
        await _feed(adapter, "普通消息")
        await scheduler.execute(adapter._event_queue.get_nowait())
        assert len(scheduler.request_diagnostics.list("bot")) == 1
        await scheduler.clear_manual_wakes("bot")
        assert scheduler.request_diagnostics.list("bot") == []


class TestRejectionRoute:
    @pytest.mark.asyncio
    async def test_rejections_query_route(self):
        backend, adapter, manager, scheduler = runtime({})
        await _feed(adapter, "普通消息")
        await scheduler.execute(adapter._event_queue.get_nowait())
        server = BackendHTTPServer(backend, port=0)
        response = await server._route("GET", "/api/platforms/wake/rejections?adapter_id=bot&limit=10", b"")
        assert response[0] == 200
        records = cast(list[Any], response[1]["records"])
        assert len(records) == 1
        empty = await server._route("GET", "/api/platforms/wake/rejections?adapter_id=other", b"")
        assert empty[0] == 200 and empty[1]["records"] == []
        bad = await server._route("GET", "/api/platforms/wake/rejections?limit=abc", b"")
        assert bad[0] == 400
        # 不与手动唤醒状态查询路由混淆
        status = await server._route("GET", "/api/platforms/wake/some-request-id", b"")
        assert status[0] in (404, 503)
