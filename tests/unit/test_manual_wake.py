"""手动唤醒的身份边界, 幂等及调度约束"""
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


def runtime():
    """构造不连接外部平台的运行时"""
    backend = BackendManager()
    backend._running = True
    manager = AsyncMock()
    manager.provider_registry = _RunnableRegistry()
    manager.handle_call_async.return_value = ""
    backend._scheduler = PipelineScheduler(manager)
    backend._adapter_mgr = PlatformAdapterManager()
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10"}))
    adapter.started = True
    backend._adapter_mgr._adapters["bot"] = adapter
    return backend, adapter, manager


def _scheduler(backend: BackendManager) -> PipelineScheduler:
    """取运行时装配的调度器, 缺失视为装配错误"""
    scheduler = backend._scheduler
    if scheduler is None:
        raise AssertionError("运行时装配缺失调度器")
    return scheduler


@pytest.mark.asyncio
async def test_prompt_manual_wake_keeps_management_actor_separate_and_is_idempotent():
    backend, adapter, manager = runtime()
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "prompt": "手动处理", "request_id": "one"}
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "already_pending"
    assert (await backend.wake_platform({**payload, "prompt": "另一个请求"}, operator="management"))["reason"] == "request_id_conflict"
    assert adapter._event_queue.qsize() == 1
    event = adapter._event_queue.get_nowait()
    assert not event.is_wake
    assert event.call_origin.actor_kind == "management"
    assert event.call_origin.actor_id == "management"
    assert event.call_origin.route_user_id == "30"
    await _scheduler(backend).execute(event)
    manager.handle_call_async.assert_awaited_once()
    assert manager.handle_call_async.call_args.args[0].message == "手动处理"
    assert (await backend.wake_platform(payload, operator="management"))["state"] == "processed"


@pytest.mark.asyncio
async def test_manual_pending_window_and_stop_guard():
    backend, adapter, manager = runtime()
    adapter.config.settings.update(wake_mode="frequency", wake_message_threshold=3)
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 1,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "等待处理"}}]})
    original = adapter._event_queue.get_nowait()
    await _scheduler(backend).execute(original)
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "request_id": "pending"}
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    event = adapter._event_queue.get_nowait()
    await _scheduler(backend).execute(event)
    manager.handle_call_async.assert_awaited_once()
    assert "等待处理" in manager.handle_call_async.call_args.args[0].message
    assert (await backend.wake_platform({**payload, "request_id": "empty"}, operator="management"))["status"] == "no_pending"
    assert (await backend.wake_platform({**payload, "request_id": "stop", "prompt": "不能越过停止"}, operator="management"))["status"] == "accepted"
    event = adapter._event_queue.get_nowait()
    event.call_llm = False
    await _scheduler(backend).execute(event)
    assert manager.handle_call_async.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_identity", [False, True])
async def test_manual_explicit_window_rebuilds_images_and_original_nickname(with_identity: bool, monkeypatch: pytest.MonkeyPatch):
    backend, adapter, manager = runtime()
    adapter._bot = AsyncMock()
    if with_identity:
        adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人乙"}
        adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "本群助手"}
    download = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + bytes(32))
    monkeypatch.setattr("satrap.core.pipeline.media_resolve._download", download)
    await adapter._handle_group_message({"self_id": 10, "group_id": 20, "user_id": 30, "message_id": 1,
        "message_type": "group", "sender": {"user_id": 30, "nickname": "小明"},
        "message": [{"type": "image", "data": {"file": "original.image", "url": "https://cdn/original.png"}}]})
    await _scheduler(backend).execute(adapter._event_queue.get_nowait())
    download.assert_not_awaited()
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "request_id": "picture-window"}
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    manual = adapter._event_queue.get_nowait()
    await _scheduler(backend).execute(manual)
    manager.handle_call_async.assert_awaited_once()
    call = manager.handle_call_async.await_args.args[0]
    prefix = "[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙, 本群名片 本群助手]\n" if with_identity else ""
    assert call.message == prefix + "[用户 小明 (ID 30), 消息 1] [图片 1]"
    assert len(call.img_urls) == 1 and call.origin.actor_kind == "management"
    download.assert_awaited_once()
    assert (await backend.wake_platform({**payload, "request_id": "empty-picture-window"}, operator="management"))["status"] == "no_pending"


@pytest.mark.asyncio
async def test_manual_rejects_forged_actor_disabled_platform_and_full_queue():
    backend, adapter, _ = runtime()
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "prompt": "hello", "request_id": "one"}
    assert (await backend.wake_platform({**payload, "operator": "admin"}, operator="management"))["status"] == "rejected"
    adapter.config.enable = False
    assert (await backend.wake_platform(payload, operator="management"))["reason"] == "adapter_unavailable"
    adapter.config.enable = True
    adapter.config.settings["group_whitelist"] = ["99"]
    assert (await backend.wake_platform(payload, operator="management"))["reason"] == "source_unavailable"
    adapter.config.settings["group_whitelist"] = []
    adapter._event_queue = asyncio.Queue(maxsize=1)
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    assert (await backend.wake_platform({**payload, "request_id": "overflow"}, operator="management"))["reason"] == "queue_full"


@pytest.mark.asyncio
async def test_manual_message_lookup_rejects_cross_group_and_concurrent_duplicates():
    backend, adapter, _ = runtime()
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
    backend, adapter, _ = runtime()
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
    assert rejected["reason"] == "request_capacity"
    clock[0] += module.PENDING_TTL + 301
    assert requests.check("new", "fp") is None
    assert not requests.records


@pytest.mark.asyncio
async def test_manual_wake_logs_acceptance_and_lookup_failure(caplog: pytest.LogCaptureFixture):
    import logging
    backend, adapter, _ = runtime()
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
