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


def runtime():
    """构造不连接外部平台的运行时"""
    backend = BackendManager()
    backend._running = True
    manager = AsyncMock()
    manager.handle_call_async.return_value = ""
    backend._scheduler = PipelineScheduler(manager)
    backend._adapter_mgr = PlatformAdapterManager()
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10"}))
    adapter.started = True
    backend._adapter_mgr._adapters["bot"] = adapter
    return backend, adapter, manager


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
    await backend._scheduler.execute(event)
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
    await backend._scheduler.execute(original)
    payload = {"adapter_id": "bot", "group_id": "20", "user_id": "30", "request_id": "pending"}
    assert (await backend.wake_platform(payload, operator="management"))["status"] == "accepted"
    event = adapter._event_queue.get_nowait()
    await backend._scheduler.execute(event)
    manager.handle_call_async.assert_awaited_once()
    assert "等待处理" in manager.handle_call_async.call_args.args[0].message
    assert (await backend.wake_platform({**payload, "request_id": "empty"}, operator="management"))["status"] == "no_pending"
    assert (await backend.wake_platform({**payload, "request_id": "stop", "prompt": "不能越过停止"}, operator="management"))["status"] == "accepted"
    event = adapter._event_queue.get_nowait()
    event.call_llm = False
    await backend._scheduler.execute(event)
    assert manager.handle_call_async.await_count == 1


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
