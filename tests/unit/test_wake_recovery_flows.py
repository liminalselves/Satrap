"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import AsyncMock
import asyncio
import aiohttp
import pytest
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.server_auth import ServerAuth
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.platform import PlatformAdapterManager, PlatformConfig
from pathlib import Path
from typing import Any, cast
import contextlib
import threading
import json
import time
from satrap.core.pipeline.manual_wake_store import ManualWakeStore, ManualWakeStoreError
from satrap.core.platform.event import MessageChain
from satrap.core.platform.receipt import SendReceipt
import satrap.core.pipeline.manual_wake_store as store_module
from satrap.core.platform.onebot.request_registry import RequestApprovalLedger
from satrap.cli import cmd_platform
from satrap.cli.client import DaemonClient
from satrap.main import _build_parser
from support.providers import RunnableRegistry


def _manual_wake_runtime():
    """构造不连接外部平台的运行时"""
    backend = BackendManager()
    backend._running = True
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    manager.handle_call_async.return_value = ""
    backend._scheduler = PipelineScheduler(manager)
    backend._adapter_mgr = PlatformAdapterManager()
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10"}))
    adapter.started = True
    backend._adapter_mgr._adapters["bot"] = adapter
    return backend, adapter, manager

def _manual_wake_scheduler(backend: BackendManager) -> PipelineScheduler:
    """取运行时装配的调度器, 缺失视为装配错误"""
    scheduler = backend._scheduler
    if scheduler is None:
        raise AssertionError("运行时装配缺失调度器")
    return scheduler

@pytest.mark.asyncio
async def test_prompt_manual_wake_keeps_management_actor_separate_and_is_idempotent():
    backend, adapter, manager = _manual_wake_runtime()
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "prompt": "手动处理", "request_id": "one"}
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "already_pending"
    assert (await backend.wake_platform({**payload, "prompt": "另一个请求"}, operator="management")).get("reason") == "request_id_conflict"
    assert adapter._event_queue.qsize() == 1
    event = adapter._event_queue.get_nowait()
    assert not event.is_wake
    assert event.call_origin.actor_kind == "management"
    assert event.call_origin.actor_id == "management"
    assert event.call_origin.route_user_id == "30"
    await _manual_wake_scheduler(backend).execute(event)
    manager.handle_call_async.assert_awaited_once()
    assert manager.handle_call_async.call_args.args[0].message == "手动处理"
    assert (await backend.wake_platform(payload, operator="management")).get("state") == "processed"

@pytest.mark.asyncio
async def test_manual_pending_window_and_stop_guard():
    backend, adapter, manager = _manual_wake_runtime()
    adapter.config.settings.update(wake_mode="frequency", wake_message_threshold=3)
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 1,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "等待处理"}}]})
    original = adapter._event_queue.get_nowait()
    await _manual_wake_scheduler(backend).execute(original)
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "request_id": "pending"}
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    event = adapter._event_queue.get_nowait()
    await _manual_wake_scheduler(backend).execute(event)
    manager.handle_call_async.assert_awaited_once()
    assert "等待处理" in manager.handle_call_async.call_args.args[0].message
    assert (await backend.wake_platform({**payload, "request_id": "empty"}, operator="management"))["status"] == "no_pending"
    assert (await backend.wake_platform({**payload, "request_id": "stop", "prompt": "不能越过停止"}, operator="management"))["status"] == "accepted"
    event = adapter._event_queue.get_nowait()
    event.call_llm = False
    await _manual_wake_scheduler(backend).execute(event)
    assert manager.handle_call_async.await_count == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("with_identity", [False, True])
async def test_manual_explicit_window_rebuilds_images_and_original_nickname(with_identity: bool, monkeypatch: pytest.MonkeyPatch):
    backend, adapter, manager = _manual_wake_runtime()
    adapter._bot = AsyncMock()
    if with_identity:
        adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
        adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "本群助手"}
    download = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + bytes(32))
    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", download)
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 1,
        "message_type": "group", "sender": {"user_id": 30, "nickname": "小明"},
        "message": [{"type": "image", "data": {"file": "original.image", "url": "https://cdn/original.png"}}]})
    await _manual_wake_scheduler(backend).execute(adapter._event_queue.get_nowait())
    download.assert_not_awaited()
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "request_id": "picture-window"}
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    manual = adapter._event_queue.get_nowait()
    await _manual_wake_scheduler(backend).execute(manual)
    manager.handle_call_async.assert_awaited_once()
    call = manager.handle_call_async.await_args.args[0]
    prefix = "[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙, 本群昵称 本群助手]\n" if with_identity else ""
    assert call.message == "[用户 小明 (ID 30), 消息 1] [图片 1]"
    assert tuple(block.content for block in call.request_blocks) == ((prefix.rstrip("\n"),) if with_identity else ())
    assert len(call.img_urls) == 1 and call.origin.actor_kind == "management"
    download.assert_awaited_once()
    assert (await backend.wake_platform({**payload, "request_id": "empty-picture-window"}, operator="management"))["status"] == "no_pending"

@pytest.mark.asyncio
async def test_manual_rejects_forged_actor_disabled_platform_and_full_queue():
    backend, adapter, _ = _manual_wake_runtime()
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "prompt": "hello", "request_id": "one"}
    assert (await backend.wake_platform({**payload, "operator": "admin"}, operator="management"))["status"] == "rejected"
    adapter.config.enable = False
    assert (await backend.wake_platform(payload, operator="management")).get("reason") == "adapter_unavailable"
    adapter.config.enable = True
    adapter.config.settings["group_whitelist"] = ["99"]
    assert (await backend.wake_platform(payload, operator="management")).get("reason") == "source_unavailable"
    adapter.config.settings["group_whitelist"] = []
    adapter._event_queue = asyncio.Queue(maxsize=1)
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    assert (await backend.wake_platform({**payload, "request_id": "overflow"}, operator="management")).get("reason") == "queue_full"

@pytest.mark.asyncio
async def test_manual_message_lookup_rejects_cross_group_and_concurrent_duplicates():
    backend, adapter, _ = _manual_wake_runtime()
    entered = asyncio.Event()
    release = asyncio.Event()
    async def get_msg(**kwargs):
        entered.set()
        await release.wait()
        return {"message_id": 42, "group_id": 20, "message_type": "group", "sender": {"user_id": 30},
                "message": [{"type": "text", "data": {"text": "回源正文"}}]}
    adapter._bot = AsyncMock()
    adapter._bot.get_msg.side_effect = get_msg
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "message_id": "42", "request_id": "lookup"}
    tasks = [asyncio.create_task(backend.wake_platform(payload, operator="management")) for _ in range(2)]
    await entered.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert sorted(item["status"] for item in results) == ["accepted", "already_pending"]
    assert adapter._event_queue.qsize() == 1
    event = adapter._event_queue.get_nowait()
    assert event.call_origin.source_message_id == "42"
    assert event.message_str == "回源正文"
    result = await backend.wake_platform({**payload, "group_id": "21", "request_id": "cross"}, operator="management")
    assert result["status"] == "rejected"
    assert adapter._event_queue.empty()

@pytest.mark.asyncio
async def test_manual_wake_real_http_requires_management_auth(unused_tcp_port):
    backend, adapter, _ = _manual_wake_runtime()
    server = BackendHTTPServer(backend, port=unused_tcp_port)
    server.auth = ServerAuth.create("127.0.0.1", unused_tcp_port, token="test-management-token-32-characters")
    await server.start()
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "prompt": "HTTP 测试", "request_id": "http"}
    try:
        async with aiohttp.ClientSession() as client:
            url = f"http://127.0.0.1:{unused_tcp_port}/api/platforms/wake"
            async with client.post(url, json=payload) as response:
                assert response.status == 401
            assert adapter._event_queue.empty()
            async with client.post(url, json=payload, headers={"Authorization": "Bearer test-management-token-32-characters"}) as response:
                assert response.status == 200
                assert (await response.json())["status"] == "accepted"
            event = adapter._event_queue.get_nowait()
            assert event.call_origin.actor_id == "management"
            assert event.call_origin.actor_kind == "management"
    finally:
        await server.stop()

def test_stale_pending_requests_release_capacity(monkeypatch: pytest.MonkeyPatch):
    from satrap.core.pipeline import manual_wake as module
    from satrap.core.pipeline.manual_wake import ManualWakeRequests, ManualWakeTicket

    requests = ManualWakeRequests()
    clock = [1000.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    for index in range(512):
        ticket = ManualWakeTicket(request_id=f"r{index}")
        requests.records[ticket.request_id] = ("fp", clock[0], ticket)
    rejected = requests.check("new", "fp")
    if rejected is None:
        raise AssertionError("容量满时应拒绝")
    assert rejected.get("reason") == "request_capacity"
    clock[0] += module.PENDING_TTL + 301
    assert requests.check("new", "fp") is None
    assert not requests.records

@pytest.mark.asyncio
async def test_manual_wake_logs_acceptance_and_lookup_failure(caplog: pytest.LogCaptureFixture):
    import logging
    backend, adapter, _ = _manual_wake_runtime()
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "prompt": "手动处理", "request_id": "log1"}
    with caplog.at_level(logging.INFO):
        assert (await backend.wake_platform(payload, operator="op"))["status"] == "accepted"
        adapter._bot = AsyncMock()
        adapter._bot.get_msg.side_effect = RuntimeError("gone")
        rejected = await backend.wake_platform({"adapter_id": "bot", "group_id": "20", "user_id": "30", "message_id": "77", "request_id": "log2"}, operator="op")
    assert rejected["status"] == "rejected"
    messages = [r.getMessage() for r in caplog.records if not r.name.endswith("_file")]
    assert any("手动唤醒已接受 request_id=log1 adapter=bot group=20 operator=op" in m for m in messages)
    assert any("手动唤醒回源失败 request_id=log2" in m for m in messages)


def _manual_wake_store_runtime(tmp_path: Path) -> tuple[BackendManager, OneBotAdapter, Any, ManualWakeStore]:
    """构造带持久化存储但不连接外部平台的运行时"""
    backend = BackendManager()
    backend._running = True
    manager = AsyncMock()
    manager.provider_registry = RunnableRegistry()
    manager.handle_call_async.return_value = ""
    scheduler = PipelineScheduler(manager)
    store = ManualWakeStore(tmp_path / "manual_wake_store.json")
    scheduler.manual_wake_store = store
    backend._scheduler = scheduler
    backend._manual_wake_store = store
    backend._adapter_mgr = PlatformAdapterManager()
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10"}))
    adapter.started = True
    cast(Any, backend._adapter_mgr)._adapters["bot"] = adapter
    return backend, adapter, manager, store

def _manual_wake_store_scheduler(backend: BackendManager) -> PipelineScheduler:
    scheduler = backend._scheduler
    if scheduler is None:
        raise AssertionError("运行时装配缺失调度器")
    return scheduler

def _payload(request_id: str) -> dict[str, object]:
    return {"adapter_id": "bot", "group_id": "20", "user_id": "30", "prompt": "手动处理", "request_id": request_id}

class _BlockedFileLock:
    """进入即超时的锁替身, 用于观察事务入口的失败归一"""

    def __init__(self, path: Path, timeout: float = 30) -> None:
        self.path = path
        self.timeout = timeout

    def __enter__(self) -> None:
        raise TimeoutError("目标文件正在执行其他写入操作, 请稍后重试")

    def __exit__(self, *args: object) -> None:
        return None

class TestRequestStore:
    @pytest.mark.parametrize("location", ["current", "archived"])
    def test_update_outcome_codes_and_terminal_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str,
    ):
        """结果码区分幂等/冲突/缺失/降级, 终态不回退, 归档记录同样只认同目标幂等"""
        if location == "archived":
            # 满容量插入触发轮转: 已终结记录进入归档
            monkeypatch.setattr(store_module, "REQUEST_CAPACITY", 2)
        store = ManualWakeStore(tmp_path / "store.json")
        # 缺失与非法状态在没有记录时也能区分
        assert store.update_request("bot", "missing", "executing") == "missing"
        assert store.update_request("bot", "anything", "not_a_status") == "invalid"
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        record = store.lookup_request("r1", "bot")
        assert record is not None
        assert record["status"] == "accepted" and record["operator"] == "op" and record["target"] == "group:20"
        with pytest.raises(ManualWakeStoreError, match="已存在"):
            store.accept_request("bot", "r1", "fp2", "group:20", "op")
        if location == "archived":
            assert store.update_request("bot", "r1", "sent") == "persisted"
            store.accept_request("bot", "p1", "fp", "group:20", "op")
            store.accept_request("bot", "p2", "fp", "group:20", "op")
            assert "bot\nr1" not in store._requests
            assert store.lookup_request("r1", "bot") is not None
            # 归档终态只认同目标幂等, 异目标判冲突, 两处都没有才判缺失
            assert store.update_request("bot", "r1", "sent", "late") == "no_op"
            assert store.update_request("bot", "r1", "failed", "late") == "conflict"
            assert store.update_request("bot", "never", "sent") == "missing"
            archived = store.lookup_request("r1", "bot")
            assert archived is not None and archived["status"] == "sent"
            store.degraded = True
            assert store.update_request("bot", "r1", "sent") == "degraded"
            return
        assert store.update_request("bot", "r1", "executing") == "persisted"
        assert store.update_request("bot", "r1", "sent") == "persisted"
        # 终态不回退: 同目标重复写入幂等, 改写成别的终态是冲突
        assert store.update_request("bot", "r1", "sent", "repeat") == "no_op"
        assert store.update_request("bot", "r1", "failed", "late") == "conflict"
        assert store.update_request("bot", "never", "sent") == "missing"
        final = store.lookup_request("r1", "bot")
        assert final is not None and final["status"] == "sent"
        # 在途 unknown 可被可信终态精化, 重启结论不可
        store.accept_request("bot", "r2", "fp", "group:20", "op")
        assert store.update_request("bot", "r2", "unknown", "in_flight_unconfirmed") == "persisted"
        assert store.update_request("bot", "r2", "sent", "confirmed", refine=True) == "persisted"
        assert store.update_request("bot", "r2", "sent", "again") == "no_op"
        store.accept_request("bot", "r3", "fp", "group:20", "op")
        store.update_request("bot", "r3", "unknown", "restart_unconfirmed")
        assert store.update_request("bot", "r3", "sent", "confirmed", refine=True) == "no_op"
        assert store.update_request("bot", "r3", "unknown", "restart_unconfirmed") == "no_op"
        store.degraded = True
        assert store.update_request("bot", "r1", "sent") == "degraded"

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
        assert store.record_send_attempt(
            "t2", "bot", "group%20", "running", [{"index": 0, "kind": "chunk", "chars": 2, "digest": "cd"}],
        ) is True
        # t2 只写了计划未推进 submitted: 该段确定尚未尝试, 重启后标 skipped
        store.mark_segment_submitted("t1", 0)
        # 模拟进程崩溃重启: 新实例接管同一文件, 未确认的一律 unknown 且不重发
        restarted = ManualWakeStore(path)
        queued = restarted.lookup_request("queued", "bot")
        running = restarted.lookup_request("running", "bot")
        done = restarted.lookup_request("done", "bot")
        assert queued is not None and queued["status"] == "unknown" and queued["detail"] == "restart_unconfirmed"
        assert running is not None and running["status"] == "unknown"
        assert done is not None and done["status"] == "sent"
        submitted = restarted._attempts["t1"]
        assert submitted["status"] == "unknown" and submitted["segments"][0]["status"] == "unknown"
        planned = restarted._attempts["t2"]
        assert planned["status"] == "failed" and planned["segments"][0]["status"] == "skipped"

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
        assert len(json.loads(store._path.read_text(encoding="utf-8"))["requests"]) == 3
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

    def test_adapter_stopped_semantics(self, tmp_path: Path):
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "queued", "fp", "group:20", "op")
        store.accept_request("bot", "running", "fp", "group:20", "op")
        store.update_request("bot", "running", "executing")
        store.accept_request("other", "keep", "fp", "group:21", "op")
        store.record_send_attempt("t1", "bot", "group%20", "running", [{"index": 0}])
        store.mark_segment_submitted("t1", 0)
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

    @pytest.mark.parametrize("scenario", [
        "main_corrupt_degrades_across_restarts",
        "manifest_corrupt_with_valid_data_migrates",
        "corrupt_main_without_manifest_degrades",
    ])
    def test_corruption_outcomes(self, tmp_path: Path, scenario: str):
        """损坏文件按清单与数据判定: 主文件损坏持久降级且不重置, 清单损坏但有数据则迁移"""
        path = tmp_path / "store.json"

        def make_store() -> ManualWakeStore:
            return ManualWakeStore(path)

        if scenario == "main_corrupt_degrades_across_restarts":
            store = make_store()
            store.accept_request("bot", "keep", "fp", "group:20", "op")
            store.update_request("bot", "keep", "sent")
            path.write_text("{broken", encoding="utf-8")
            first = make_store()
            assert first.degraded is True and first.degraded_reason == "main_corrupt"
            assert list(tmp_path.glob("store.json.corrupt-*"))
            for _ in range(2):
                again = make_store()
                assert again.degraded is True and again.degraded_reason != ""
                assert again.lookup_request("keep", "bot") is None
                with pytest.raises(ManualWakeStoreError) as exc:
                    again.accept_request("bot", "keep", "fp", "group:20", "op")
                assert exc.value.reason == "degraded"
        elif scenario == "manifest_corrupt_with_valid_data_migrates":
            store = make_store()
            store.accept_request("bot", "r1", "fp", "group:20", "op")
            (tmp_path / "store.manifest.json").write_text("not-json", encoding="utf-8")
            restarted = make_store()
            assert restarted.degraded is False
            record = restarted.lookup_request("r1", "bot")
            assert record is not None and record["fingerprint"] == "fp" and record["status"] == "accepted"
            manifest = json.loads((tmp_path / "store.manifest.json").read_text(encoding="utf-8"))
            assert manifest["expected_files"] == {"main": "present", "archive": "absent"}
        else:
            path.write_text("not-json{", encoding="utf-8")
            store = make_store()
            assert store.degraded is True and store.degraded_reason == "manifest_missing_with_data"
            assert (tmp_path / "store.manifest.json").is_file()
            # 上一轮的隔离文件保留在原目录, 新实例仍然判定为降级
            assert len(list(tmp_path.glob("store.json.corrupt-*"))) == 1

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
        assert store.degraded is False

    def test_failed_persist_does_not_show_unconfirmed_terminal_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "r", "fp", "group:20", "op")
        assert store.record_send_attempt("t1", "bot", "group%20", "r", [{"index": 0}, {"index": 1}]) is True

        def broken_save() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "_save_current_locked", broken_save)
        assert store.update_request("bot", "r", "sent") == "io"
        record = store.lookup_request("r", "bot")
        assert record is not None and record["status"] == "accepted"
        assert store.mark_segment_submitted("t1", 0) is False
        assert store.record_segment_result("t1", 0, "sent") is False
        assert store.complete_send_attempt("t1", "sent", "action_rejected") is False
        attempt = store._attempts["t1"]
        assert attempt["status"] == "submitted"
        assert [segment["status"] for segment in attempt["segments"]] == ["planned", "planned"]
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
        backend, adapter, manager, store = _manual_wake_store_runtime(tmp_path)
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
        assert dup["status"] == "already_pending" and dup.get("state") == "unknown"
        conflict = await backend.wake_platform({**_payload("one"), "prompt": "另一个请求"}, operator="management")
        assert conflict["status"] == "rejected" and conflict.get("reason") == "request_id_conflict"
        assert adapter._event_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_enqueue_failure_marks_failed_and_frees_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backend, adapter, _, store = _manual_wake_store_runtime(tmp_path)

        def fail_commit(event: object) -> bool:
            return False

        monkeypatch.setattr(adapter, "commit_event", fail_commit)
        result = await backend.wake_platform(_payload("enq"), operator="management")
        assert result["status"] == "rejected" and result.get("reason") == "queue_full"
        record = store.lookup_request("enq", "bot")
        assert record is not None and record["status"] == "failed" and record["detail"] == "queue_full"
        # 同指纹重试返回原状态, 不遗留假 accepted
        retry = await backend.wake_platform(_payload("enq"), operator="management")
        assert retry["status"] == "already_pending" and retry.get("state") == "failed"
        assert all(row["status"] not in {"accepted", "executing"} for row in json.loads(store._path.read_text(encoding="utf-8"))["requests"].values())

    @pytest.mark.asyncio
    async def test_persist_failure_rejects_without_placeholder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backend, adapter, _, store = _manual_wake_store_runtime(tmp_path)

        def broken_save() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(store, "_save_current_locked", broken_save)
        result = await backend.wake_platform(_payload("io"), operator="management")
        assert result["status"] == "rejected" and result.get("reason") == "store_unavailable"
        # 占位已回滚: 同 request_id 再走一遍仍按落盘失败拒绝, 而非 already_pending
        again = await backend.wake_platform(_payload("io"), operator="management")
        assert again["status"] == "rejected" and again.get("reason") == "store_unavailable"
        assert adapter._event_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_concurrent_duplicate_single_winner(self, tmp_path: Path):
        backend, adapter, _, store = _manual_wake_store_runtime(tmp_path)
        results = await asyncio.gather(*(backend.wake_platform(_payload("dup"), operator="management") for _ in range(3)))
        statuses = sorted(result["status"] for result in results)
        assert statuses == ["accepted", "already_pending", "already_pending"]
        assert adapter._event_queue.qsize() == 1
        assert len(json.loads(store._path.read_text(encoding="utf-8"))["requests"]) == 1

class TestPipelineAndSendAttempts:
    @pytest.mark.asyncio
    async def test_full_pipeline_transitions_accepted_executing_sent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backend, adapter, manager, store = _manual_wake_store_runtime(tmp_path)
        manager.handle_call_async.return_value = "收到"
        seen_during_send: list[str] = []

        async def fake_send(
            session_id: str, message: object, *, request_id: str = "",
            purpose: str = "business", require_tracking: bool = False,
        ) -> SendReceipt:
            current = store.lookup_request("pipe", "bot")
            seen_during_send.append(current["status"] if current is not None else "missing")
            return SendReceipt("success", ("m1",))

        monkeypatch.setattr(adapter, "send_message", fake_send)
        assert (await backend.wake_platform(_payload("pipe"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        await _manual_wake_store_scheduler(backend).execute(event)
        record = store.lookup_request("pipe", "bot")
        assert record is not None and record["status"] == "sent"
        assert seen_during_send == ["executing"]

    @pytest.mark.asyncio
    async def test_pipeline_error_marks_failed_not_sent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backend, adapter, manager, store = _manual_wake_store_runtime(tmp_path)
        manager.handle_call_async.side_effect = RuntimeError("llm down")
        _manual_wake_store_scheduler(backend).error_feedback = False

        async def fake_send(
            session_id: str, message: object, *, request_id: str = "",
            purpose: str = "business", require_tracking: bool = False,
        ) -> SendReceipt:
            raise AssertionError("失败路径不应发送")

        monkeypatch.setattr(adapter, "send_message", fake_send)
        assert (await backend.wake_platform(_payload("err"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        await _manual_wake_store_scheduler(backend).execute(event)
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
        # 段证据与调用方结论矛盾: 计划落盘全为 planned, 未确认段拒绝写成 sent
        other = ManualWakeStore(tmp_path / "other.json")
        other.record_send_attempt("t2", "bot", "group%20", "rq", [
            {"index": 0, "kind": "chunk", "chars": 3, "digest": "aa"},
            {"index": 1, "kind": "chunk", "chars": 3, "digest": "bb"},
            {"index": 2, "kind": "chunk", "chars": 3, "digest": "cc"},
        ])
        assert [segment["status"] for segment in other._attempts["t2"]["segments"]] == ["planned", "planned", "planned"]
        assert other.mark_segment_submitted("t2", 0) is True
        assert other.record_segment_result("t2", 0, "sent", advance_to=1) is True
        assert [segment["status"] for segment in other._attempts["t2"]["segments"]] == ["sent", "submitted", "planned"]
        assert other.record_segment_result("t2", 1, "failed") is True
        assert other.complete_send_attempt("t2", "sent") is False
        assert other.complete_send_attempt("t2", "partial", "action_rejected") is True
        assert [segment["status"] for segment in other._attempts["t2"]["segments"]] == ["sent", "failed", "skipped"]
        # 终态不重复更新
        assert other.complete_send_attempt("t2", "sent") is False
        assert other._attempts["t2"]["status"] == "partial"

    @pytest.mark.asyncio
    async def test_degraded_store_rejects_new_but_sends_continue(self, tmp_path: Path):
        path = tmp_path / "manual_wake_store.json"
        path.write_text("{broken", encoding="utf-8")
        backend, adapter, _, store = _manual_wake_store_runtime(tmp_path)
        assert store.degraded is True
        result = await backend.wake_platform(_payload("deg"), operator="management")
        assert result["status"] == "rejected" and result.get("reason") == "store_unavailable"
        status = await backend.manual_wake_status("deg", "bot")
        assert status.get("reason") == "store_degraded"
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
        backend, _, _, store = _manual_wake_store_runtime(tmp_path)
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


    def test_lock_entry_failure_is_typed_store_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """E4: 事务入口的锁失败按存储不可用上报, 不再是裸 OSError"""
        store = ManualWakeStore(tmp_path / "store.json")
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        monkeypatch.setattr(store_module, "FileLock", _BlockedFileLock)
        with pytest.raises(ManualWakeStoreError) as exc:
            store.accept_request("bot", "r2", "fp", "group:20", "op")
        assert exc.value.reason == "io"
        with pytest.raises(ManualWakeStoreError) as exc:
            store.update_request("bot", "r1", "sent")
        assert exc.value.reason == "io"
        # 降级组件按 OSError/TimeoutError 判定"标记未落盘", 双基类保证该契约不被破坏
        assert isinstance(exc.value, (OSError, TimeoutError))

    def test_marker_write_lock_failure_keeps_degrade_without_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """E4 反例: 降级标记写不动时保持内存降级且不隔离任何文件"""
        store = ManualWakeStore(tmp_path / "store.json")
        bad = tmp_path / "bad.json"
        bad.write_text("not-json{", encoding="utf-8")
        monkeypatch.setattr(store_module, "FileLock", _BlockedFileLock)
        store._degrade("archive_corrupt", "标记写不动", [bad])
        assert store.degraded is True and store.degraded_reason == "archive_corrupt"
        assert bad.is_file() and list(tmp_path.glob("bad.json.corrupt-*")) == []

class TestStoreUnavailableRoute:
    """E4 反例: 事务进入失败与保存失败都返回稳定拒绝原因, 不退化成通用 500"""

    @staticmethod
    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise ManualWakeStoreError("io", "存储锁不可用: TimeoutError")

    @pytest.mark.asyncio
    async def test_wake_post_lookup_failure_reports_store_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ):
        import logging

        backend, adapter, _, store = _manual_wake_store_runtime(tmp_path)
        monkeypatch.setattr(store, "lookup_request", self._blocked)
        server = BackendHTTPServer(backend, port=0)
        with caplog.at_level(logging.WARNING):
            status, result = await server._route("POST", "/api/platforms/wake", json.dumps(_payload("locked")).encode())
        assert status == 409 and result["status"] == "rejected" and result["reason"] == "store_unavailable"
        assert result["request_id"] == "locked"
        assert any("手动唤醒查重失败" in r.getMessage() for r in caplog.records)
        # 无入队与进程内登记副作用
        assert adapter._event_queue.empty() and not _manual_wake_store_scheduler(backend).manual_wakes.records

    @pytest.mark.asyncio
    async def test_wake_post_accept_failure_reports_store_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ):
        import logging

        backend, adapter, _, store = _manual_wake_store_runtime(tmp_path)
        monkeypatch.setattr(store, "accept_request", self._blocked)
        server = BackendHTTPServer(backend, port=0)
        with caplog.at_level(logging.WARNING):
            status, result = await server._route("POST", "/api/platforms/wake", json.dumps(_payload("locked2")).encode())
        assert status == 409 and result["reason"] == "store_unavailable"
        assert any("手动唤醒占位落盘失败" in r.getMessage() for r in caplog.records)
        assert adapter._event_queue.empty() and not _manual_wake_store_scheduler(backend).manual_wakes.records

    @pytest.mark.asyncio
    async def test_wake_status_route_reports_store_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ):
        import logging

        backend, _, _, store = _manual_wake_store_runtime(tmp_path)
        monkeypatch.setattr(store, "lookup_request", self._blocked)
        server = BackendHTTPServer(backend, port=0)
        with caplog.at_level(logging.WARNING):
            status, result = await server._route("GET", "/api/platforms/wake/locked?adapter_id=bot", b"")
        assert status == 503 and result["reason"] == "store_unavailable"
        assert any("手动唤醒状态查询失败" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_platform_stop_survives_store_write_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ):
        import logging

        backend, _, _, store = _manual_wake_store_runtime(tmp_path)
        monkeypatch.setattr(store, "adapter_stopped", self._blocked)
        scheduler = _manual_wake_store_scheduler(backend)
        with caplog.at_level(logging.WARNING):
            await scheduler.clear_manual_wakes("bot")
        assert any("平台停止状态未落盘" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_platform_stop_waits_off_loop_for_contended_lock(self, tmp_path: Path):
        """R2 反例: 等文件锁期间事件循环必须继续推进, 清理仍要等到真正落盘"""
        from satrap.core.storage.file_lock import FileLock

        backend, _, _, store = _manual_wake_store_runtime(tmp_path)
        scheduler = _manual_wake_store_scheduler(backend)
        store.accept_request("bot", "r1", "fp", "group:20", "op")
        held, release = threading.Event(), threading.Event()

        def holder() -> None:
            with FileLock(store._lock_path):
                held.set()
                release.wait(10)

        holder_thread = threading.Thread(target=holder, daemon=True)
        holder_thread.start()
        assert held.wait(10)
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        heartbeat = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        baseline = ticks
        threading.Timer(0.3, release.set).start()
        started = time.monotonic()
        try:
            await scheduler.clear_manual_wakes("bot")
        finally:
            release.set()
            holder_thread.join(10)
        elapsed = time.monotonic() - started
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        # 确实等到了锁, 但等待期间事件循环仍在跑心跳
        assert elapsed >= 0.2
        assert ticks - baseline >= 5
        record = store.lookup_request("r1", "bot")
        assert record is not None and record["status"] == "failed"

class TestSendOutcomeAdjudication:
    """B3 反例: 请求终态由同一 request_id 的全部业务尝试归并, 副作用已发生就不能谎报失败"""

    @staticmethod
    def _attach(backend: BackendManager, tmp_path: Path) -> tuple[OneBotAdapter, ManualWakeStore, PipelineScheduler]:
        store = ManualWakeStore(tmp_path / "manual_wake_store.json")
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10", "message_text_limit": 64}))
        adapter.set_send_attempt_recorder(store)
        adapter.started = True
        adapter._bot = AsyncMock()
        scheduler = _manual_wake_store_scheduler(backend)
        scheduler.manual_wake_store = store
        backend._manual_wake_store = store
        cast(Any, backend._adapter_mgr)._adapters["bot"] = adapter
        return adapter, store, scheduler

    @pytest.mark.asyncio
    async def test_cancel_before_any_io_is_failed_cancelled_before_send(self, tmp_path: Path):
        """反例: 取消发生在任何发送 I/O 之前时请求判失败, 且不得发出网络动作"""
        backend, _, _, _ = _manual_wake_store_runtime(tmp_path)
        adapter, store, scheduler = self._attach(backend, tmp_path)

        async def cancelled(*args: Any, **kwargs: Any) -> str:
            raise asyncio.CancelledError()

        replacement_manager = AsyncMock(handle_call_async=cancelled)
        replacement_manager.provider_registry = RunnableRegistry()
        scheduler.session_manager = cast(Any, replacement_manager)
        assert (await backend.wake_platform(_payload("cancel0"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        with pytest.raises(asyncio.CancelledError):
            await scheduler.execute(event)
        record = store.lookup_request("cancel0", "bot")
        assert record is not None and record["status"] == "failed" and record["detail"] == "cancelled_before_send"
        adapter._bot.send_group_msg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_after_first_segment_is_unknown_with_confirmed_prefix(self, tmp_path: Path):
        """反例: 第二段可能已提交时不得显示已送达也不得判失败, 已确认首段保留"""
        backend, _, _, _ = _manual_wake_store_runtime(tmp_path)
        adapter, store, scheduler = self._attach(backend, tmp_path)
        blocker = asyncio.Event()

        async def slow_send(**kwargs: Any) -> dict[str, Any]:
            if "第二节" in json.dumps(kwargs, ensure_ascii=False):
                await blocker.wait()
            return {"message_id": 1}

        adapter._bot.send_group_msg = AsyncMock(side_effect=slow_send)

        async def send_two_then_cancel(*args: Any, **kwargs: Any) -> str:
            event = next(iter(scheduler.manual_wakes.tickets))
            await event.send(MessageChain.from_text("第一节"))
            pending = asyncio.ensure_future(event.send(MessageChain.from_text("第二节")))
            await asyncio.sleep(0.05)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            return ""

        replacement_manager = AsyncMock(handle_call_async=send_two_then_cancel)
        replacement_manager.provider_registry = RunnableRegistry()
        scheduler.session_manager = cast(Any, replacement_manager)
        assert (await backend.wake_platform(_payload("cancel1"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        record = store.lookup_request("cancel1", "bot")
        assert record is not None and record["status"] == "unknown"
        attempts = store.lookup_attempts("cancel1", "bot")
        assert len(attempts) == 2
        interrupted = next(item for item in attempts if item["status"] == "unknown")
        assert [segment["status"] for segment in interrupted["segments"]] == ["unknown"]
        confirmed = next(item for item in attempts if item["status"] == "sent")
        assert confirmed["segments"][0]["status"] == "sent"
        assert store.request_send_outcome("cancel1", "bot")["confirmed"] == 1

    @pytest.mark.asyncio
    async def test_llm_timeout_does_not_erase_tool_delivery(self, tmp_path: Path):
        """反例: 模型超时不得把模型调用期间已经发出的回复抹成失败"""
        backend, _, _, _ = _manual_wake_store_runtime(tmp_path)
        adapter, store, scheduler = self._attach(backend, tmp_path)
        adapter._bot.send_group_msg = AsyncMock(return_value={"message_id": 1})
        scheduler.llm_timeout = 0.05
        scheduler.error_feedback = False

        async def tool_then_hang(*args: Any, **kwargs: Any) -> str:
            event = next(iter(scheduler.manual_wakes.tickets))
            await event.send(MessageChain.from_text("工具已发送的回复"))
            await asyncio.sleep(5)
            return ""

        replacement_manager = AsyncMock(handle_call_async=tool_then_hang)
        replacement_manager.provider_registry = RunnableRegistry()
        scheduler.session_manager = cast(Any, replacement_manager)
        assert (await backend.wake_platform(_payload("timeout"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        record = store.lookup_request("timeout", "bot")
        # 已确认的工具输出保留为已确认前缀, 模型超时既不抹掉它也不谎报全部送达
        assert record is not None and record["status"] == "partial"
        assert "llm_timeout" in record["detail"] and "confirmed_prefix" in record["detail"]
        adapter._bot.send_group_msg.assert_awaited_once()
        attempts = store.lookup_attempts("timeout", "bot")
        assert attempts[0]["segments"][0]["status"] == "sent"

    @pytest.mark.asyncio
    async def test_error_feedback_receipt_does_not_mark_business_sent(self, tmp_path: Path):
        """反例: 错误提示发送成功不能覆盖业务失败"""
        backend, _, _, _ = _manual_wake_store_runtime(tmp_path)
        adapter, store, scheduler = self._attach(backend, tmp_path)

        async def plain_failure(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("llm down")

        replacement_manager = AsyncMock(handle_call_async=plain_failure)
        replacement_manager.provider_registry = RunnableRegistry()
        scheduler.session_manager = cast(Any, replacement_manager)
        assert (await backend.wake_platform(_payload("feedback"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        await scheduler.execute(event)
        record = store.lookup_request("feedback", "bot")
        assert record is not None and record["status"] == "failed"
        assert record["detail"] == "pipeline_error:RuntimeError"
        adapter._bot.send_group_msg.assert_awaited_once()
        attempts = store.lookup_attempts("feedback", "bot")
        assert [item["purpose"] for item in attempts] == ["error_feedback"]
        assert store.request_send_outcome("feedback", "bot")["confirmed"] == 0

    @pytest.mark.asyncio
    async def test_tracking_failure_refuses_business_send(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """反例: 已受理请求的必要记录无法落盘时不发业务输出, 也不冒充可恢复"""
        backend, _, _, _ = _manual_wake_store_runtime(tmp_path)
        adapter, store, scheduler = self._attach(backend, tmp_path)

        def broken_save() -> None:
            raise OSError("disk full")

        async def reply(*args: Any, **kwargs: Any) -> str:
            event = next(iter(scheduler.manual_wakes.tickets))
            await event.send(MessageChain.from_text("业务回复"))
            return ""

        replacement_manager = AsyncMock(handle_call_async=reply)
        replacement_manager.provider_registry = RunnableRegistry()
        scheduler.session_manager = cast(Any, replacement_manager)
        assert (await backend.wake_platform(_payload("notrack"), operator="management"))["status"] == "accepted"
        event = adapter._event_queue.get_nowait()
        monkeypatch.setattr(store, "_save_current_locked", broken_save)
        await scheduler.execute(event)
        adapter._bot.send_group_msg.assert_not_awaited()
        receipt = event.last_business_receipt
        assert receipt is not None and receipt.status == "unknown" and receipt.reason == "tracking_unavailable"

    @pytest.mark.asyncio
    async def test_legacy_attempt_without_purpose_is_not_business_evidence(self, tmp_path: Path):
        """反例: 旧记录缺 purpose 不能按默认值判成业务已送达"""
        path = tmp_path / "store.json"
        store = ManualWakeStore(path)
        store.accept_request("bot", "legacy", "fp", "group:20", "op")
        assert store.record_send_attempt("t-old", "bot", "group%20", "legacy", [{"index": 0}]) is True
        assert store.mark_segment_submitted("t-old", 0) is True
        assert store.record_segment_result("t-old", 0, "sent") is True
        assert store.complete_send_attempt("t-old", "sent") is True
        # 模拟旧版本写入的记录: 缺少 purpose 字段
        raw = json.loads(path.read_text(encoding="utf-8"))
        del raw["attempts"]["t-old"]["purpose"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        migrated = ManualWakeStore(path)
        attempt = migrated.lookup_attempts("legacy", "bot")[0]
        assert attempt["purpose"] == "unknown"
        outcome = migrated.request_send_outcome("legacy", "bot")
        assert outcome["confirmed"] == 1 and outcome["legacy_confirmed"] == 1 and outcome["business_attempts"] == 0

@pytest.mark.parametrize('name', ['manual_wake', 'request_ledger'])
async def test_management_recovery_requires_restored_files_and_preserves_history(tmp_path, name):
    backend = BackendManager()
    path = tmp_path / f'{name}.json'
    if name == 'manual_wake':
        initial = ManualWakeStore(path)
        initial.accept_request('bot', 'request', 'fingerprint', 'group:20', 'management')
        original = path.read_bytes()
        path.write_text('{broken', encoding='utf-8')
        store = ManualWakeStore(path)
        backend._manual_wake_store = store
    else:
        initial = RequestApprovalLedger(path)
        await initial.register('bot', '100', 'friend', 'used', user_id='321')
        await initial.occupy('bot', '100', 'friend', 'used')
        await initial.settle('bot', '100', 'friend', 'used', 'completed')
        await initial.register('bot', '100', 'friend', 'interrupted', user_id='321')
        await initial.occupy('bot', '100', 'friend', 'interrupted')
        original = path.read_bytes()
        path.write_text('{broken', encoding='utf-8')
        store = RequestApprovalLedger(path)
        backend._request_ledger = store
    assert store.degraded
    server = BackendHTTPServer(backend)
    body = json.dumps({'store': name}).encode()
    status, failed = await server._route('POST', '/api/platforms/recover-store', body)
    assert status == 409 and not failed['ok'] and store.degraded
    path.write_bytes(original)
    status, recovered = await server._route('POST', '/api/platforms/recover-store', body)
    assert status == 200 and recovered['ok'] and not store.degraded
    if name == 'manual_wake':
        assert backend._manual_wake_store is not None
        record = backend._manual_wake_store.lookup_request('request', 'bot')
        assert record is not None and record['fingerprint'] == 'fingerprint'
    else:
        ledger = backend._request_ledger
        assert ledger is not None
        with pytest.raises(LookupError):
            await ledger.occupy('bot', '100', 'friend', 'used')
        interrupted = ledger.lookup('bot', '100', 'friend', 'interrupted')
        assert interrupted is not None and interrupted['state'] == 'unknown'
        await ledger.register('bot', '100', 'friend', 'new', user_id='321')
        await ledger.occupy('bot', '100', 'friend', 'new')
    before_repeat = path.read_bytes()
    assert (await server._route('POST', '/api/platforms/recover-store', body))[0] == 200
    assert path.read_bytes() == before_repeat
    if name == 'request_ledger':
        assert backend._request_ledger is not None
        active = backend._request_ledger.lookup('bot', '100', 'friend', 'new')
        assert active is not None and active['state'] == 'executing'

def test_missing_expected_manual_wake_archive_cannot_be_discarded_by_recovery(tmp_path):
    path = tmp_path / 'wake.json'
    initial = ManualWakeStore(path)
    initial._load_archive_locked()
    initial._save_archive_locked()
    archive = initial._archive_path
    original = archive.read_bytes()
    archive.unlink()
    degraded = ManualWakeStore(path)
    assert degraded.degraded
    assert not degraded.recover()
    assert degraded.degraded
    degraded = ManualWakeStore(path)
    assert not degraded.recover()
    archive.write_bytes(original)
    assert degraded.recover()

@pytest.mark.parametrize('payload', [{}, {'store': []}, {'store': 'other'}, {'store': 'manual_wake', 'path': 'arbitrary'}])
async def test_recovery_route_rejects_unknown_stores_and_extra_fields(payload):
    server = BackendHTTPServer(BackendManager())
    status, result = await server._route('POST', '/api/platforms/recover-store', json.dumps(payload).encode())
    assert status == 400 and result['reason'] == 'invalid_store'

async def test_uninitialized_store_is_reported_unavailable():
    backend = BackendManager()
    server = BackendHTTPServer(backend)
    status, result = await server._route('POST', '/api/platforms/recover-store', b'{"store":"request_ledger"}')
    assert status == 503 and result['reason'] == 'store_unavailable'

async def test_failed_interrupted_state_write_never_clears_ledger_degradation(tmp_path, monkeypatch):
    path = tmp_path / 'ledger.json'
    original = RequestApprovalLedger(path)
    await original.register('bot', '100', 'friend', 'interrupted', user_id='321')
    await original.occupy('bot', '100', 'friend', 'interrupted')
    repaired = path.read_bytes()
    path.write_text('{broken', encoding='utf-8')
    ledger = RequestApprovalLedger(path)
    path.write_bytes(repaired)

    def failed_write():
        raise OSError('磁盘不可写')

    monkeypatch.setattr(ledger, '_save_locked', failed_write)
    backend = BackendManager()
    backend._request_ledger = ledger
    status, result = await BackendHTTPServer(backend)._route('POST', '/api/platforms/recover-store', b'{"store":"request_ledger"}')
    assert status == 409 and not result['ok']
    assert ledger.degraded
    assert json.loads((tmp_path / 'ledger.manifest.json').read_text(encoding='utf-8'))['degraded'] is not None
    with pytest.raises(LookupError):
        await ledger.occupy('bot', '100', 'friend', 'interrupted')

async def test_recovery_endpoint_requires_management_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv('SATRAP_CREDENTIALS_ROOT', str(tmp_path / 'credentials'))
    backend = BackendManager()
    recover = AsyncMock(return_value={'ok': True, 'store': 'manual_wake', 'reason': ''})
    monkeypatch.setattr(backend, 'recover_platform_store', recover)
    server = BackendHTTPServer(backend, port=0)
    await server.start()
    try:
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        body = b'{"store":"manual_wake"}'
        writer.write(f'POST /api/platforms/recover-store HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()
        assert response.startswith(b'HTTP/1.1 401')
        recover.assert_not_awaited()
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'POST /api/platforms/recover-store HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {server.auth.token}\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()
        assert response.startswith(b'HTTP/1.1 200')
        recover.assert_awaited_once_with('manual_wake')
    finally:
        await server.stop()

@pytest.mark.parametrize('name', ['manual-wake', 'request-ledger'])
def test_cli_recovery_command_uses_authenticated_daemon_route(monkeypatch, name):
    args = _build_parser().parse_args(['platform', 'recover-store', name])
    calls = []
    client = DaemonClient()
    monkeypatch.setattr(client, 'require_alive', lambda: None)

    def request(method, path, body):
        calls.append((method, path, body))
        return {'ok': True, 'store': body['store']}

    monkeypatch.setattr(client, '_request', request)
    monkeypatch.setattr(cmd_platform, 'daemon_client_from_args', lambda *_, **__: client)
    monkeypatch.setattr(cmd_platform, 'render_data', lambda *_: None)
    cmd_platform.dispatch(args)
    assert calls == [('POST', '/api/platforms/recover-store', {'store': name.replace('-', '_')})]
