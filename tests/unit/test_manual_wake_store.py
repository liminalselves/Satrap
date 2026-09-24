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
        # 降级状态已随清单持久化: 重启与隔离文件都不等于恢复去重能力
        assert (tmp_path / "store.manifest.json").is_file()
        fresh = ManualWakeStore(path)
        assert fresh.degraded is True
        assert fresh.degraded_reason == "manifest_missing_with_data"
        with pytest.raises(ManualWakeStoreError) as exc:
            fresh.accept_request("bot", "r", "fp", "group:20", "op")
        assert exc.value.reason == "degraded"

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


class TestPersistentDegradation:
    """B2 反例: 主文件与归档的降级状态必须持久, 不能靠重启或隔离文件恢复去重能力"""

    def test_corrupt_main_stays_degraded_across_restarts(self, tmp_path: Path):
        path = tmp_path / "store.json"
        store = ManualWakeStore(path)
        store.accept_request("bot", "keep", "fp", "group:20", "op")
        store.update_request("bot", "keep", "sent")
        path.write_text("{broken", encoding="utf-8")
        first = ManualWakeStore(path)
        assert first.degraded is True and first.degraded_reason == "main_corrupt"
        assert list(tmp_path.glob("store.json.corrupt-*"))
        for _ in range(2):
            again = ManualWakeStore(path)
            assert again.degraded is True and again.degraded_reason != ""
            assert again.lookup_request("keep", "bot") is None
            with pytest.raises(ManualWakeStoreError) as exc:
                again.accept_request("bot", "keep", "fp", "group:20", "op")
            assert exc.value.reason == "degraded"

    def test_archive_corruption_degrades_without_empty_fallback(self, tmp_path: Path):
        path = tmp_path / "store.json"
        store = ManualWakeStore(path)
        store.accept_request("bot", "archived", "fp", "group:20", "op")
        store.update_request("bot", "archived", "sent")
        archive = tmp_path / "store.json.1"
        archived_record = store.lookup_request("archived", "bot")
        assert archived_record is not None
        store._load_archive_locked()
        assert store._archive_requests is not None
        store._archive_requests["bot\narchived"] = archived_record
        store._save_archive_locked()
        assert archive.is_file()
        archive.write_text("boom{", encoding="utf-8")
        restarted = ManualWakeStore(path)
        assert restarted.degraded is True and restarted.degraded_reason == "archive_corrupt"
        assert list(tmp_path.glob("store.json.1.corrupt-*"))
        # 归档损坏不再按空归档继续: 查询明确降级, 接受请求被拒
        assert restarted.lookup_request("archived", "bot") is None
        with pytest.raises(ManualWakeStoreError) as exc:
            restarted.accept_request("bot", "r", "fp", "group:20", "op")
        assert exc.value.reason == "degraded"

    def test_missing_expected_files_are_corruption_not_empty_store(self, tmp_path: Path):
        path = tmp_path / "store.json"
        store = ManualWakeStore(path)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        path.unlink()
        missing_main = ManualWakeStore(path)
        assert missing_main.degraded is True and missing_main.degraded_reason == "main_missing"
        assert missing_main.lookup_request("r1", "bot") is None

        store = ManualWakeStore(tmp_path / "second.json")
        store.accept_request("bot", "r2", "fp", "group:20", "op")
        store.update_request("bot", "r2", "sent")
        store._load_archive_locked()
        archive_record = store.lookup_request("r2", "bot")
        assert store._archive_requests is not None and archive_record is not None
        store._archive_requests["bot\nr2"] = archive_record
        store._save_archive_locked()
        (tmp_path / "second.json.1").unlink()
        missing_archive = ManualWakeStore(tmp_path / "second.json")
        assert missing_archive.degraded is True and missing_archive.degraded_reason == "archive_missing"

    def test_manifest_corrupt_with_data_migrates_instead_of_resetting(self, tmp_path: Path):
        path = tmp_path / "store.json"
        store = ManualWakeStore(path)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        (tmp_path / "store.manifest.json").write_text("not-json", encoding="utf-8")
        restarted = ManualWakeStore(path)
        assert restarted.degraded is False
        record = restarted.lookup_request("r1", "bot")
        assert record is not None and record["fingerprint"] == "fp" and record["status"] == "accepted"
        manifest = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert manifest["expected_files"] == {"main": "present", "archive": "absent"}

    def test_manifest_corrupt_and_main_unreadable_degrades_without_reset(self, tmp_path: Path):
        path = tmp_path / "store.json"
        path.write_text("not-json{", encoding="utf-8")
        store = ManualWakeStore(path)
        assert store.degraded is True and store.degraded_reason == "manifest_missing_with_data"
        assert (tmp_path / "store.manifest.json").is_file()
        # 上一轮的隔离文件保留在原目录, 新实例仍然判定为降级
        quarantined = list(tmp_path.glob("store.json.corrupt-*"))
        assert len(quarantined) == 1

    def test_degrade_marker_write_failure_keeps_original_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        path = tmp_path / "store.json"
        path.write_text("not-json{", encoding="utf-8")

        def broken_write(self: object, manifest: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(ManualWakeStore, "_write_manifest", broken_write)
        store = ManualWakeStore(path)
        assert store.degraded is True
        # 标记写失败: 保留原文件不隔离, 下次启动仍可重新判定
        assert path.is_file() and path.read_text(encoding="utf-8") == "not-json{"
        assert list(tmp_path.glob("store.json.corrupt-*")) == []
        with pytest.raises(ManualWakeStoreError) as exc:
            store.accept_request("bot", "r", "fp", "group:20", "op")
        assert exc.value.reason == "degraded"

    def test_legacy_files_migrate_without_record_loss(self, tmp_path: Path):
        path = tmp_path / "store.json"
        legacy_request: dict[str, Any] = {
            "request_id": "old", "fingerprint": "fp-old", "adapter_id": "bot", "target": "group:20",
            "operator": "op", "status": "sent", "detail": "", "created_at": 1.0, "updated_at": 2.0,
        }
        legacy_attempt: dict[str, Any] = {
            "turn_id": "t-old", "adapter_id": "bot", "target": "group%20", "request_id": "old",
            "segments": [{"index": 0, "kind": "chunk", "chars": 2, "digest": "ab", "status": "sent"}],
            "status": "sent", "detail": "", "created_at": 1.0, "updated_at": 2.0,
        }
        # 旧版文件缺少 version 字段, 且目录中没有清单
        path.write_text(json.dumps({"requests": {"bot\nold": legacy_request}, "attempts": {"t-old": legacy_attempt}}), encoding="utf-8")
        store = ManualWakeStore(path)
        assert store.degraded is False
        record = store.lookup_request("old", "bot")
        assert record is not None
        assert record["fingerprint"] == "fp-old" and record["status"] == "sent" and record["operator"] == "op"
        assert store._attempts["t-old"]["status"] == "sent"
        manifest = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert manifest["expected_files"] == {"main": "present", "archive": "absent"}
        with pytest.raises(ManualWakeStoreError, match="已存在"):
            store.accept_request("bot", "old", "fp-old", "group:20", "op")

    def test_rotation_crash_keeps_records_and_adopts_archive(self, tmp_path: Path):
        path = tmp_path / "store.json"
        main_record: dict[str, Any] = {
            "request_id": "rot", "fingerprint": "fp", "adapter_id": "bot", "target": "group:20",
            "operator": "op", "status": "sent", "detail": "", "created_at": 1.0, "updated_at": 2.0,
        }
        archive_only: dict[str, Any] = {**main_record, "request_id": "archived", "fingerprint": "fp2"}
        # 轮转写入归档后进程中断: 主文件仍是旧内容, 归档已包含同一记录与更早的记录
        path.write_text(json.dumps({"version": 1, "requests": {"bot\nrot": main_record}, "attempts": {}}), encoding="utf-8")
        (tmp_path / "store.json.1").write_text(
            json.dumps({"version": 1, "requests": {"bot\nrot": main_record, "bot\narchived": archive_only}, "attempts": {}}),
            encoding="utf-8",
        )
        store = ManualWakeStore(path)
        assert store.degraded is False
        assert store.lookup_request("rot", "bot") is not None
        archived = store.lookup_request("archived", "bot")
        assert archived is not None and archived["fingerprint"] == "fp2"
        manifest = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
        assert manifest["expected_files"]["archive"] == "present"
        assert store.pending_counts()["degraded"] is False

    def test_failed_persist_does_not_show_unconfirmed_terminal_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "r", "fp", "group:20", "op")
        assert store.record_send_attempt("t1", "bot", "group%20", "r", [{"index": 0}, {"index": 1}]) is True

        def broken_save() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "_save_current_locked", broken_save)
        assert store.update_request("bot", "r", "sent") is False
        record = store.lookup_request("r", "bot")
        assert record is not None and record["status"] == "accepted"
        assert store.complete_send_attempt("t1", ["sent", "failed"], "partial", "action_rejected") is False
        attempt = store._attempts["t1"]
        assert attempt["status"] == "submitted"
        assert [segment["status"] for segment in attempt["segments"]] == ["submitted", "submitted"]
        assert store.adapter_stopped("bot") is None
        stopped = store.lookup_request("r", "bot")
        assert stopped is not None and stopped["status"] == "accepted"


class TestRecoveryEntry:
    """B2 恢复入口: 只接受主文件与归档一致的完整记录集, 不提供清空历史后继续"""

    @staticmethod
    def _degrade_manifest(tmp_path: Path) -> None:
        manifest_path = tmp_path / "store.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["degraded"] = {"reason": "test", "at": 0.0}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_recover_requires_restored_original_file(self, tmp_path: Path):
        path = tmp_path / "store.json"
        path.write_text("not-json{", encoding="utf-8")
        store = ManualWakeStore(path)
        assert store.degraded is True
        # 主文件已被隔离: 不能清空重建, 必须先恢复原文件
        assert store.recover() is False
        assert store.degraded is True
        path.write_text(json.dumps({"version": 1, "requests": {}, "attempts": {}}), encoding="utf-8")
        assert store.recover() is True
        assert store.degraded is False and store.degraded_reason == ""
        store.accept_request("bot", "r", "fp", "group:20", "op")
        assert store.lookup_request("r", "bot") is not None

    def test_recover_rejects_duplicate_records(self, tmp_path: Path):
        path = tmp_path / "store.json"
        store = ManualWakeStore(path)
        store.accept_request("bot", "dup", "fp", "group:20", "op")
        record = store.lookup_request("dup", "bot")
        assert record is not None
        store._load_archive_locked()
        assert store._archive_requests is not None
        store._archive_requests["bot\ndup"] = record
        store._save_archive_locked()
        self._degrade_manifest(tmp_path)
        degraded = ManualWakeStore(path)
        assert degraded.degraded is True
        assert degraded.recover() is False
        assert degraded.degraded is True

    def test_identity_mismatch_counts_as_corruption(self, tmp_path: Path):
        path = tmp_path / "store.json"
        mismatch: dict[str, Any] = {
            "request_id": "r1", "fingerprint": "fp", "adapter_id": "other", "target": "group:20",
            "operator": "op", "status": "sent", "detail": "", "created_at": 1.0, "updated_at": 2.0,
        }
        path.write_text(json.dumps({"version": 1, "requests": {"bot\nr1": mismatch}, "attempts": {}}), encoding="utf-8")
        store = ManualWakeStore(path)
        assert store.degraded is True and store.degraded_reason == "manifest_missing_with_data"
        assert list(tmp_path.glob("store.json.corrupt-*"))

    def test_sent_attempt_with_unconfirmed_segment_counts_as_corruption(self, tmp_path: Path):
        path = tmp_path / "store.json"
        attempt: dict[str, Any] = {
            "turn_id": "t1", "adapter_id": "bot", "target": "group%20", "request_id": "r",
            "segments": [{"index": 0, "status": "submitted"}], "status": "sent", "detail": "",
            "created_at": 1.0, "updated_at": 2.0,
        }
        path.write_text(json.dumps({"version": 1, "requests": {}, "attempts": {"t1": attempt}}), encoding="utf-8")
        store = ManualWakeStore(path)
        assert store.degraded is True
        with pytest.raises(ManualWakeStoreError) as exc:
            store.accept_request("bot", "r", "fp", "group:20", "op")
        assert exc.value.reason == "degraded"


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
