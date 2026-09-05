"""标准日志实时流及 WebSocket 推送测试"""
from __future__ import annotations

import asyncio
import pytest
from typing import Any, cast
import io

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.log.stream import StandardLogStream, StandardStreamCapture, standard_log_stream

from satrap.core.log import logger


@pytest.mark.asyncio
async def test_standard_log_stream_keeps_history_and_pushes_live_entries() -> None:
    """订阅应原子获取历史日志并收到后续实时日志"""
    stream = StandardLogStream(history_size=3)
    stream.publish("first", "INFO")
    stream.publish("second", "WARNING")
    stream.publish("third", "ERROR")

    subscription = stream.subscribe(history_limit=2, queue_size=2)
    assert [entry.content for entry in subscription.history] == ["second", "third"]

    stream.publish("live", "DEBUG")
    entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
    assert (entry.content, entry.level) == ("live", "DEBUG")

    stream.unsubscribe(subscription.subscription_id)
    stream.publish("after unsubscribe", "INFO")
    assert subscription.queue.empty()


@pytest.mark.asyncio
async def test_standard_log_stream_zero_history_returns_no_entries() -> None:
    """历史条数为零时不应错误返回全部历史"""
    stream = StandardLogStream()
    stream.publish("history", "INFO")

    subscription = stream.subscribe(history_limit=0)
    try:
        assert subscription.history == ()
    finally:
        stream.unsubscribe(subscription.subscription_id)


@pytest.mark.asyncio
async def test_satrap_standard_logger_publishes_to_live_stream() -> None:
    """Satrap 控制台日志应同步到标准日志实时流"""
    standard_log_stream.clear()
    subscription = standard_log_stream.subscribe(history_limit=0)
    try:
        logger.info("standard stream probe", save_to_file=False)
        entry = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert entry.level == "INFO"
        assert "standard stream probe" in entry.content
    finally:
        standard_log_stream.unsubscribe(subscription.subscription_id)


@pytest.mark.asyncio
async def test_standard_stream_capture_preserves_output_and_publishes_lines() -> None:
    """标准流代理应保留原输出并按完整文本行发布"""
    wrapped = io.StringIO()
    stream = StandardLogStream()
    capture = StandardStreamCapture(wrapped, stream, "INFO")
    subscription = stream.subscribe(history_limit=0)
    try:
        assert capture.write("plain stdout") == len("plain stdout")
        assert subscription.queue.empty()
        capture.write("\n[ERROR]: failed\n")

        first = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        second = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert (first.content, first.level) == ("plain stdout", "INFO")
        assert (second.content, second.level) == ("[ERROR]: failed", "ERROR")
        assert wrapped.getvalue() == "plain stdout\n[ERROR]: failed\n"
    finally:
        stream.unsubscribe(subscription.subscription_id)


class _Reader:
    """可控制 EOF 状态的 WebSocket 测试读取器"""

    def __init__(self) -> None:
        self.eof = False

    def at_eof(self) -> bool:
        """
        返回:
        - bool: 当前是否已结束
        """
        return self.eof


@pytest.mark.asyncio
async def test_websocket_logs_use_standard_stream_without_log_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日志 WebSocket 应发送内存标准流的历史与实时输出"""
    standard_log_stream.clear()
    standard_log_stream.publish("history from std", "INFO")
    server = BackendHTTPServer(BackendManager())
    reader = _Reader()
    sent: list[dict[str, Any]] = []

    async def capture_send(_writer: object, message: dict[str, Any]) -> None:
        """
        参数:
        - _writer: 未使用的测试写入器
        - message: WebSocket 消息
        """
        sent.append(message)

    monkeypatch.setattr(server, "_ws_send", capture_send)
    task = asyncio.create_task(server._ws_log_handler(
        cast(asyncio.StreamReader, reader),
        cast(asyncio.StreamWriter, object()),
        history_lines=10,
    ))
    await asyncio.sleep(0)

    standard_log_stream.publish("live from std", "ERROR")
    for _ in range(10):
        if len(sent) >= 2:
            break
        await asyncio.sleep(0.01)

    reader.eof = True
    standard_log_stream.publish("wake", "DEBUG")
    await asyncio.wait_for(task, timeout=1)

    assert sent[0]["data"] == {"content": "history from std", "level": "INFO"}
    assert sent[1]["data"] == {"content": "live from std", "level": "ERROR"}
