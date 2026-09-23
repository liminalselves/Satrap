"""手动唤醒状态存储: 接受事务, 重启降级, 容量归档, 发送尝试记录与状态查询路由"""
from __future__ import annotations

from unittest.mock import AsyncMock
from pathlib import Path
from typing import Any, cast
import asyncio
import json

import pytest

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.pipeline.manual_wake_store import ManualWakeStore, ManualWakeStoreError
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from satrap.core.platform.event import MessageChain
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.receipt import SendReceipt
import satrap.core.pipeline.manual_wake_store as store_module


def runtime(tmp_path: Path) -> tuple[BackendManager, OneBotAdapter, Any, ManualWakeStore]:
    """构造带持久化存储但不连接外部平台的运行时"""
    backend = BackendManager()
    backend._running = True
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    store = ManualWakeStore(tmp_path / "manual_wake_store.json")
    scheduler.manual_wake_store = store
    backend._scheduler = scheduler
    backend._manual_wake_store = store
    backend._adapter_mgr = PlatformAdapterManager()
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10"}))
    adapter.started = True
    backend._adapter_mgr._adapters["bot"] = adapter
    return backend, adapter, manager, store


def _scheduler(backend: BackendManager) -> PipelineScheduler:
    scheduler = backend._scheduler
    if scheduler is None:
        raise AssertionError("运行时装配缺失调度器")
    return scheduler


def _payload(request_id: str) -> dict[str, object]:
    return {"adapter_id": "bot", "group_id": "20", "user_id": "30", "prompt": "手动处理", "request_id": request_id}


class TestRequestStore:
    def test_accept_lookup_and_terminal_guard(self, tmp_path: Path):
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        record = store.lookup_request("r1", "bot")
        assert record is not None
        assert record["status"] == "accepted" and record["operator"] == "op" and record["target"] == "group:20"
        with pytest.raises(ManualWakeStoreError, match="已存在"):
            store.accept_request("bot", "r1", "fp2", "group:20", "op")
        assert store.update_request("bot", "r1", "executing") is True
        assert store.update_request("bot", "r1", "sent") is True
        assert store.update_request("bot", "r1", "failed", "late") is False
        final = store.lookup_request("r1", "bot")
        assert final is not None and final["status"] == "sent"

    def test_restart_marks_unconfirmed_unknown(self, tmp_path: Path):
        path = tmp_path / "store.json"
        store = ManualWakeStore(path)
        store.accept_request("bot", "queued", "fp", "group:20", "op")
        store.accept_request("bot", "running", "fp", "group:20", "op")
        store.update_request("bot", "running", "executing")
        store.accept_request("bot", "done", "fp", "group:20", "op")
        store.update_request("bot", "done", "sent")
        assert store.record_send_attempt(
            "t1", "bot", "group%20", "running", [{"index": 0, "kind": "chunk", "chars": 2, "digest": "ab"}],
        ) is True
        # 模拟进程崩溃重启: 新实例接管同一文件, 未确认的一律 unknown 且不重发
        restarted = ManualWakeStore(path)
        queued = restarted.lookup_request("queued", "bot")
        running = restarted.lookup_request("running", "bot")
        done = restarted.lookup_request("done", "bot")
        assert queued is not None and queued["status"] == "unknown" and queued["detail"] == "restart_unconfirmed"
        assert running is not None and running["status"] == "unknown"
        assert done is not None and done["status"] == "sent"
        attempt = restarted._attempts["t1"]
        assert attempt["status"] == "unknown" and attempt["segments"][0]["status"] == "unknown"

    def test_corrupt_file_quarantined_and_degraded(self, tmp_path: Path):
        path = tmp_path / "store.json"
        path.write_text("not-json{", encoding="utf-8")
        store = ManualWakeStore(path)
        assert store.degraded is True
        quarantined = list(tmp_path.glob("store.json.corrupt-*"))
        assert len(quarantined) == 1 and quarantined[0].read_text(encoding="utf-8") == "not-json{"
        assert path.exists() is False
        with pytest.raises(ManualWakeStoreError) as exc:
            store.accept_request("bot", "r", "fp", "group:20", "op")
        assert exc.value.reason == "degraded"
        assert store.lookup_request("r") is None
        assert store.record_send_attempt("t", "bot", "group%20", "", [{"index": 0}]) is False
        # 其余功能照常: 同一路径可重建全新存储
        fresh = ManualWakeStore(path)
        assert fresh.degraded is False

    def test_capacity_rotation_archive_and_pending_never_evicted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(store_module, "REQUEST_CAPACITY", 3)
        monkeypatch.setattr(store_module, "ARCHIVE_REQUEST_CAPACITY", 2)
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "p1", "fp", "group:20", "op")
        store.accept_request("bot", "p2", "fp", "group:20", "op")
        store.accept_request("bot", "s1", "fp", "group:20", "op")
        store.update_request("bot", "s1", "sent")
        # 满容量插入: settled 记录轮转归档一代, 未决记录保留在当前文件
        store.accept_request("bot", "p3", "fp", "group:20", "op")
        archived = store.lookup_request("s1", "bot")
        assert archived is not None and archived["status"] == "sent"
        assert store.pending_counts()["requests_total"] == 3
        store.update_request("bot", "p2", "sent")
        store.accept_request("bot", "s2", "fp", "group:20", "op")
        # 归档已满: 拒绝新记录且不动未决记录
        store.update_request("bot", "p3", "sent")
        with pytest.raises(ManualWakeStoreError) as exc:
            store.accept_request("bot", "s3", "fp", "group:20", "op")
        assert exc.value.reason == "capacity"
        kept = store.lookup_request("p3", "bot")
        assert kept is not None and kept["status"] == "sent"
        oldest = store.lookup_request("p1", "bot")
        assert oldest is not None and oldest["status"] == "accepted"

    def test_pending_only_capacity_rejects(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(store_module, "REQUEST_CAPACITY", 2)
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "p1", "fp", "group:20", "op")
        store.accept_request("bot", "p2", "fp", "group:20", "op")
        with pytest.raises(ManualWakeStoreError) as exc:
            store.accept_request("bot", "p3", "fp", "group:20", "op")
        assert exc.value.reason == "capacity"

    def test_retention_sweep_drops_only_settled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(store_module, "REQUEST_CAPACITY", 2)
        monkeypatch.setattr(store_module, "RETENTION_SECONDS", -1)
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "old", "fp", "group:20", "op")
        store.update_request("bot", "old", "failed", "boom")
        store.accept_request("bot", "pending", "fp", "group:20", "op")
        # 超保留期的 settled 记录被清理腾出容量, 未决记录不受影响
        store.accept_request("bot", "new", "fp", "group:20", "op")
        assert store.lookup_request("old", "bot") is None
        assert store.lookup_request("pending", "bot") is not None

    def test_send_attempt_partial_segments(self, tmp_path: Path):
        store = ManualWakeStore(tmp_path / "store.json")
        segments: list[dict[str, object]] = [
            {"index": 0, "kind": "chunk", "chars": 3, "digest": "aa"},
            {"index": 1, "kind": "chunk", "chars": 3, "digest": "bb"},
            {"index": 2, "kind": "chunk", "chars": 3, "digest": "cc"},
        ]
        assert store.record_send_attempt("t1", "bot", "group%20", "rq", segments) is True
        assert store.complete_send_attempt("t1", ["sent", "failed", "skipped"], "partial", "action_rejected") is True
        attempt = store._attempts["t1"]
        assert attempt["status"] == "partial" and attempt["detail"] == "action_rejected"
        assert [segment["status"] for segment in attempt["segments"]] == ["sent", "failed", "skipped"]
        # 终态不重复更新
        assert store.complete_send_attempt("t1", ["sent", "sent", "sent"], "sent") is False
        assert store._attempts["t1"]["status"] == "partial"

    def test_adapter_stopped_semantics(self, tmp_path: Path):
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "queued", "fp", "group:20", "op")
        store.accept_request("bot", "running", "fp", "group:20", "op")
        store.update_request("bot", "running", "executing")
        store.accept_request("other", "keep", "fp", "group:21", "op")
        store.record_send_attempt("t1", "bot", "group%20", "running", [{"index": 0}])
        store.adapter_stopped("bot")
        queued = store.lookup_request("queued", "bot")
        running = store.lookup_request("running", "bot")
        keep = store.lookup_request("keep", "other")
        assert queued is not None and queued["status"] == "failed" and queued["detail"] == "platform_stopped"
        assert running is not None and running["status"] == "unknown" and running["detail"] == "stopped_unconfirmed"
        assert keep is not None and keep["status"] == "accepted"
        assert store._attempts["t1"]["status"] == "unknown"


class TestAcceptTransaction:
    @pytest.mark.asyncio
    async def test_persisted_dedupe_survives_restart(self, tmp_path: Path):
        backend, adapter, manager, store = runtime(tmp_path)
        assert (await backend.wake_platform(_payload("one"), operator="management"))["status"] == "accepted"
        record = store.lookup_request("one", "bot")
        assert record is not None and record["status"] == "accepted" and record["target"] == "group:20"
        again = await backend.wake_platform(_payload("one"), operator="management")
        assert again["status"] == "already_pending"
        # 模拟重启: 内存登记与调度器全部换新, 存储文件接管去重
        restarted = ManualWakeStore(tmp_path / "manual_wake_store.json")
        backend._manual_wake_store = restarted
        fresh_scheduler = PipelineScheduler(manager)
        fresh_scheduler.manual_wake_store = restarted
        backend._scheduler = fresh_scheduler
        assert adapter._event_queue.qsize() == 1
        dup = await backend.wake_platform(_payload("one"), operator="management")
        assert dup["status"] == "already_pending" and dup["state"] == "unknown"
        conflict = await backend.wake_platform({**_payload("one"), "prompt": "另一个请求"}, operator="management")
        assert conflict["status"] == "rejected" and conflict["reason"] == "request_id_conflict"
        assert adapter._event_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_enqueue_failure_marks_failed_and_frees_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backend, adapter, _, store = runtime(tmp_path)

        def fail_commit(event: object) -> bool:
            return False

        monkeypatch.setattr(adapter, "commit_event", fail_commit)
        result = await backend.wake_platform(_payload("enq"), operator="management")
        assert result["status"] == "rejected" and result["reason"] == "queue_full"
        record = store.lookup_request("enq", "bot")
        assert record is not None and record["status"] == "failed" and record["detail"] == "queue_full"
        # 同指纹重试返回原状态, 不遗留假 accepted
        retry = await backend.wake_platform(_payload("enq"), operator="management")
        assert retry["status"] == "already_pending" and retry["state"] == "failed"
        assert store.pending_counts()["requests_pending"] == 0

    @pytest.mark.asyncio
    async def test_persist_failure_rejects_without_placeholder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backend, adapter, _, store = runtime(tmp_path)

        def broken_save() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "_save_current_locked", broken_save)
        result = await backend.wake_platform(_payload("io"), operator="management")
        assert result["status"] == "rejected" and result["reason"] == "store_unavailable"
        # 占位已回滚: 同 request_id 再走一遍仍按落盘失败拒绝, 而非 already_pending
        again = await backend.wake_platform(_payload("io"), operator="management")
        assert again["status"] == "rejected" and again["reason"] == "store_unavailable"
        assert adapter._event_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_single_winner(self, tmp_path: Path):
        backend, adapter, _, store = runtime(tmp_path)
        results = await asyncio.gather(*(backend.wake_platform(_payload("dup"), operator="management") for _ in range(3)))
        statuses = sorted(result["status"] for result in results)
        assert statuses == ["accepted", "already_pending", "already_pending"]
        assert adapter._event_queue.qsize() == 1
        assert store.pending_counts()["requests_total"] == 1


class TestPipelineAndSendAttempts:
    @pytest.mark.asyncio
    async def test_full_pipeline_transitions_accepted_executing_sent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backend, adapter, manager, store = runtime(tmp_path)
        manager.handle_call_async.return_value = "收到"
        seen_during_send: list[str] = []

        async def fake_send(session_id: str, message: object, *, request_id: str = "") -> SendReceipt:
            current = store.lookup_request("pipe", "bot")
            seen_during_send.append(current["status"] if current is not None else "missing")
            return SendReceipt("success", ("m1",))

        monkeypatch.setattr(adapter, "send_message", fake_send)
        assert (await backend.wake_platform(_payload("pipe"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        await _scheduler(backend).execute(event)
        record = store.lookup_request("pipe", "bot")
        assert record is not None and record["status"] == "sent"
        assert seen_during_send == ["executing"]

    @pytest.mark.asyncio
    async def test_pipeline_error_marks_failed_not_sent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backend, adapter, manager, store = runtime(tmp_path)
        manager.handle_call_async.side_effect = RuntimeError("llm down")
        _scheduler(backend).error_feedback = False

        async def fake_send(session_id: str, message: object, *, request_id: str = "") -> SendReceipt:
            raise AssertionError("失败路径不应发送")

        monkeypatch.setattr(adapter, "send_message", fake_send)
        assert (await backend.wake_platform(_payload("err"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        await _scheduler(backend).execute(event)
        record = store.lookup_request("err", "bot")
        assert record is not None and record["status"] == "failed" and record["detail"] == "pipeline_error:RuntimeError"

    @pytest.mark.asyncio
    async def test_attempt_recorded_before_io_and_partial(self, tmp_path: Path):
        from aiocqhttp.exceptions import ActionFailed

        store = ManualWakeStore(tmp_path / "store.json")
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10", "message_text_limit": 64}))
        adapter.set_send_attempt_recorder(store)
        path = tmp_path / "store.json"

        class _FlakyBot:
            def __init__(self) -> None:
                self.calls = 0
                self.seen_submitted = False

            async def send_group_msg(self, **kwargs: Any) -> dict[str, Any]:
                self.calls += 1
                payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                raw_attempts: object = payload.get("attempts", {})
                attempts: dict[str, Any] = cast(dict[str, Any], raw_attempts) if isinstance(raw_attempts, dict) else {}
                if any(isinstance(item, dict) and cast(dict[str, Any], item).get("status") == "submitted" for item in attempts.values()):
                    self.seen_submitted = True
                if self.calls == 2:
                    raise ActionFailed({"retcode": 1200})
                return {"message_id": self.calls}

        bot = _FlakyBot()
        adapter._bot = bot
        receipt = await adapter.send_message("group%20", MessageChain.from_text("x" * 140), request_id="rq9")
        assert receipt.status == "partial"
        # 首次发送 I/O 时尝试记录已落盘为 submitted
        assert bot.seen_submitted is True
        assert len(store._attempts) == 1
        attempt = next(iter(store._attempts.values()))
        assert attempt["status"] == "partial" and attempt["request_id"] == "rq9"
        assert attempt["target"] == "group%20"
        assert [segment["status"] for segment in attempt["segments"]] == ["sent", "failed", "skipped"]
        assert [segment["kind"] for segment in attempt["segments"]] == ["chunk", "chunk", "chunk"]
        # 重启后已终结尝试不再改写
        restarted = ManualWakeStore(path)
        assert restarted._attempts[attempt["turn_id"]]["status"] == "partial"

    @pytest.mark.asyncio
    async def test_degraded_store_rejects_new_but_sends_continue(self, tmp_path: Path):
        path = tmp_path / "manual_wake_store.json"
        path.write_text("{broken", encoding="utf-8")
        backend, adapter, _, store = runtime(tmp_path)
        assert store.degraded is True
        result = await backend.wake_platform(_payload("deg"), operator="management")
        assert result["status"] == "rejected" and result["reason"] == "store_unavailable"
        status = await backend.manual_wake_status("deg", "bot")
        assert status["reason"] == "store_degraded"
        # 发送功能照常, 仅跳过尝试记录
        adapter.set_send_attempt_recorder(store)

        class _OkBot:
            async def send_group_msg(self, **kwargs: Any) -> dict[str, Any]:
                return {"message_id": 3}

        adapter._bot = _OkBot()
        receipt = await adapter.send_message("group%20", MessageChain.from_text("照常发送"))
        assert receipt.status == "success"
        assert store._attempts == {}


class TestStatusRoute:
    @pytest.mark.asyncio
    async def test_wake_status_query_route(self, tmp_path: Path):
        backend, _, _, store = runtime(tmp_path)
        assert (await backend.wake_platform(_payload("route"), operator="management"))["status"] == "accepted"
        server = BackendHTTPServer(backend, port=0)
        found = await server._route("GET", "/api/platforms/wake/route?adapter_id=bot", b"")
        assert found[0] == 200 and found[1]["status"] == "accepted" and found[1]["adapter_id"] == "bot"
        missing = await server._route("GET", "/api/platforms/wake/nope", b"")
        assert missing[0] == 404 and missing[1]["reason"] == "not_found"
        store.degraded = True
        degraded = await server._route("GET", "/api/platforms/wake/route?adapter_id=bot", b"")
        assert degraded[0] == 503 and degraded[1]["reason"] == "store_degraded"
        bad = await server._route("GET", "/api/platforms/wake/" + "x" * 129, b"")
        assert bad[0] == 400
