"""真实 WebSocket 上验证溢出, 重连快照及询问恢复"""
import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

from satrap.core.server_auth import ServerAuth
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.display.recorder import DisplayRecorder
from satrap.display.server import ChatHTTPServer
from satrap.display.service import ChatService, _SubscriberQueue


async def test_slow_websocket_recovers_reply_and_pending_question(tmp_path):
    recorder = DisplayRecorder(str(tmp_path / "display.db"), "audit")
    conv = SimpleNamespace(conversation_id="audit", recorder=recorder, subscribers=set(), pending_user_inputs={}, task=None)
    service = ChatService.__new__(ChatService)
    service._conversations = {"audit": conv}
    service._orphan_queues = {}
    service._operations = {}
    service._event_sequences = {}
    service._stream_id = "server-test"
    service._ask_user_timeout_seconds = 5
    token = "audit-only-token-that-is-at-least-32-characters"
    server = ChatHTTPServer.__new__(ChatHTTPServer)
    MiniHTTPServer.__init__(server, host="127.0.0.1", port=0, auth=ServerAuth.create("127.0.0.1", 0, token=token))
    server.service = service
    question_task = None
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as client:
            url = f"http://127.0.0.1:{port}/ws/chat?conversation=audit"
            async with client.ws_connect(url) as slow:
                snapshot = await slow.receive_json(timeout=2)
                assert snapshot["type"] == "snapshot" and snapshot["state"] == "idle"
                fast = service.subscribe("audit")
                recorder.start_turn("question")
                for _ in range(1005):
                    recorder.on_content("x")
                    service._broadcast(conv, {"type": "content_delta", "delta": "x"})
                    assert fast.get_nowait()["type"] == "content_delta"
                completed = recorder.end_turn()
                service._broadcast(conv, {"type": "turn_done", "answer": "x" * 1005, **completed})
                assert fast.get_nowait()["type"] == "turn_done"
                assert not fast.invalidated
                event = await slow.receive_json(timeout=2)
                assert event == {"type": "resync_required"}
                assert (await slow.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
                service.unsubscribe("audit", fast)
            recorder.start_turn("approval")
            question_task = asyncio.create_task(service._request_user_input(conv, "请选择", ["继续", "取消"]))
            await asyncio.sleep(0)
            async with client.ws_connect(url) as recovered:
                snapshot = await recovered.receive_json(timeout=2)
                assert snapshot["turns"][0]["answer"] == "x" * 1005
                assert snapshot["state"] == "running"
                request = snapshot["pending_user_inputs"][0]
                assert request["question"] == "请选择"
                assert service.answer_user_input("audit", request["request_id"], "1")["ok"]
                assert not service.answer_user_input("audit", request["request_id"], "1")["ok"]
                assert await question_task == "1"
                ended = await recovered.receive_json(timeout=2)
                assert ended["type"] == "ask_user_end" and ended["seq"] == snapshot["seq"] + 1
                completed = recorder.end_turn("finished")
                service._broadcast(conv, {"type": "turn_done", "answer": "finished", **completed})
                assert (await recovered.receive_json(timeout=2))["type"] == "turn_done"
                assert service.runtime_snapshot("audit")["state"] == "idle"
    finally:
        if question_task and not question_task.done():
            question_task.cancel()
            await asyncio.gather(question_task, return_exceptions=True)
        await server.stop()
        recorder.close()


def test_subscriber_byte_budget_is_independent_of_message_count():
    queue = _SubscriberQueue(maxsize=1000, max_bytes=128)
    queue.put_nowait({"type": "content_delta", "delta": "x" * 60})
    import pytest
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait({"type": "content_delta", "delta": "x" * 60})
    queue.invalidate()
    assert queue.qsize() == 1 and queue.buffered_bytes <= 128


@pytest.mark.parametrize("event_type", ["turn_done", "ask_user"])
def test_full_queue_requires_resynchronization(event_type):
    from satrap.display.service import _SubscriberQueue
    service = ChatService.__new__(ChatService)
    service._event_sequences = {}
    service._stream_id = "audit"
    queue = _SubscriberQueue(maxsize=1000)
    for _ in range(1000):
        queue.put_nowait({"type": "content", "delta": "old"})
    conv = SimpleNamespace(conversation_id="audit", subscribers={queue})
    service._broadcast(conv, {"type": event_type})
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert events == [{"type": "resync_required"}]
    assert queue.invalidated and queue not in conv.subscribers
