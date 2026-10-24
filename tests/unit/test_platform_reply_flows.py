"""业务链路场景与集中边界契约"""
from __future__ import annotations

from unittest.mock import AsyncMock
from typing import Any
import asyncio
from aiocqhttp.exceptions import ActionFailed
import pytest
from satrap.core.config.platform_policy import validate_wake_policy
from satrap.core.platform.onebot.outbound import OutboundTurns, split_components
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.components import BaseMessageComponent, Plain, Image, At
from satrap.core.platform.event import MessageChain
from satrap.core.platform import PlatformConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True], ids=["private", "group"])
async def test_onebot_file_stream_transfers_real_bytes_then_uploads_only_remote_path(tmp_path, group):
    """真实 OneBot HTTP 客户端分块传字节, 不要求实现端能读 Satrap 本地路径"""
    import base64
    import hashlib
    from aiocqhttp import CQHttp
    from aiohttp import web
    from satrap.core.call_context import CallOrigin
    from satrap.core.file_access.artifacts import freeze_file
    chunks, uploads = {}, []
    mode = ["success"]
    async def rpc(request):
        data = await request.json()
        action = request.match_info["action"]
        if action == "upload_file_stream":
            if mode[0] == "unsupported":
                return web.json_response({"status": "failed", "retcode": 1404, "data": None, "wording": "API not supported"})
            if "chunk_data" in data:
                chunk = base64.b64decode(data["chunk_data"])
                assert len(chunk) <= 256 * 1024
                chunks[data["chunk_index"]] = chunk
                result = {"status": "chunk_received", "received_chunks": len(chunks)}
            else:
                payload = b"".join(chunks[index] for index in sorted(chunks))
                result = {"status": "file_complete", "file_path": "/napcat-temp/report.bin", "file_size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        else:
            uploads.append((action, data))
            assert action == ("upload_group_file" if group else "upload_private_file")
            assert data["file"] == "/napcat-temp/report.bin"
            result = {"file_id": "platform-file"} if mode[0] == "success" else {}
        return web.json_response({"status": "ok", "retcode": 0, "data": result})
    app = web.Application()
    app.router.add_post("/{action}", rpc)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = cast(Any, site._server).sockets[0].getsockname()[1]
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    adapter._bot = CQHttp(api_root=f"http://127.0.0.1:{port}")
    adapter.bot_self_id = adapter.client_self_id = "10000"
    adapter._running = True
    source = tmp_path / "report.bin"
    source.write_bytes(bytes(range(256)) * 3000)
    artifact = freeze_file(source, tmp_path / "artifacts")
    origin = CallOrigin("ob", "10000", "GroupMessage" if group else "FriendMessage", "456", "123", "77", "files")
    try:
        checks = []
        result = await adapter.deliver_file(origin, artifact, lambda: checks.append(True))
        assert len(checks) == 3

        assert result["ok"] and result["status"] == "uploaded", result
        assert b"".join(chunks[index] for index in sorted(chunks)) == source.read_bytes()
        assert len(uploads) == 1 and uploads[0][1]["name"] == "report.bin"
        mode[0] = "unknown"
        chunks.clear()
        result = await adapter.deliver_file(origin, artifact, lambda: None)
        assert result["status"] == "unknown" and len(uploads) == 2
        mode[0] = "unsupported"
        result = await adapter.deliver_file(origin, artifact, lambda: None)
        assert not result["ok"] and len(uploads) == 2
        mode[0] = "success"
        chunks.clear()
        source.write_bytes(b"")
        empty = freeze_file(source, tmp_path / "artifacts")
        result = await adapter.deliver_file(origin, empty, lambda: None)
        delivered = cast(dict[str, object], result["file"])
        assert result["ok"] and delivered["size"] == 0 and chunks == {0: b""}
    finally:
        await runner.cleanup()
from collections.abc import Iterator
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from pathlib import Path
import socket
from typing import Protocol, cast
import json
from satrap.core.components.message import download_file
from satrap.core.utils.outbound import OutboundResponseTooLargeError, ResolvedOutboundTarget
from satrap.core.utils.outbound import TRUSTED_DOWNLOAD_HOSTS_ENV_NAME, UnsafeOutboundURLError, _PinnedResolver
from satrap.core.utils.outbound import safe_async_get, safe_sync_get, validate_outbound_http_url
from satrap.core.utils.outbound import validate_outbound_redirect
from satrap.core.config.wake_overrides import validate_wake_overrides
from satrap.core.components import Reply
from satrap.core.platform.event import MessageEvent
from satrap.core.platform.receipt import SendReceipt, combine_receipts

def _texts(chunks: list[list[BaseMessageComponent]]) -> list[str]:
    return ["".join(c.text for c in chunk if isinstance(c, Plain)) for chunk in chunks]

def test_split_prefers_paragraph_then_newline_and_keeps_all_characters():
    text = "第一段\n\n第二段很长很长\n第三段"
    chunks = split_components([Plain(text)], 8)
    assert "".join(_texts(chunks)) == text
    assert _texts(chunks)[0] == "第一段\n\n"
    assert all(len(t) <= 8 for t in _texts(chunks))

def test_split_hard_cuts_single_long_paragraph():
    chunks = split_components([Plain("a" * 25)], 10)
    assert _texts(chunks) == ["a" * 10, "a" * 10, "a" * 5]

def test_split_keeps_non_text_components_in_order_and_intact():
    image = Image(file="http://x/1.png")
    at = At(qq="1")
    chunks = split_components([Plain("x" * 6), image, Plain("y" * 6), at], 8)
    flat = [c for chunk in chunks for c in chunk]
    assert [type(c) for c in flat] == [Plain, Image, Plain, Plain, At]
    assert flat[1] is image and flat[4] is at
    assert "".join(_texts(chunks)) == "x" * 6 + "y" * 6

def test_split_short_message_is_single_chunk():
    components: list[BaseMessageComponent] = [Plain("短"), Image(file="http://x/1.png")]
    chunks = split_components(components, 2000)
    assert len(chunks) == 1 and chunks[0][1] is components[1]
    assert components == [components[0], components[1]]

def test_split_rejects_non_positive_limit():
    with pytest.raises(ValueError):
        split_components([Plain("a")], 0)

@pytest.mark.parametrize("value,valid", [
    (63, False), (32001, False), ("2000", False), (2000.5, False), (True, False),
    (64, True), (32000, True), (None, True),
])
def test_message_text_limit_validation_bounds(value: object, valid: bool):
    """边界值接受, 越界或非整数拒绝, 缺省配置也合法"""
    if value is None:
        validate_wake_policy({})
    elif valid:
        validate_wake_policy({"message_text_limit": value})
    else:
        with pytest.raises(ValueError, match="message_text_limit"):
            validate_wake_policy({"message_text_limit": value})

def _adapter(**settings: object) -> OneBotAdapter:
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=settings))
    adapter._bot = AsyncMock()
    return adapter

@pytest.mark.asyncio
async def test_long_message_sends_chunks_in_order_and_aggregates_ids():
    adapter = _adapter(message_text_limit=64)
    adapter._bot.send_group_msg.side_effect = [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]
    receipt = await adapter.send_message("group%456", MessageChain.from_text("甲" * 150))
    assert receipt.status == "success"
    assert receipt.message_ids == ("1", "2", "3")
    sent = [call.kwargs["message"][0]["data"]["text"] for call in adapter._bot.send_group_msg.await_args_list]
    assert "".join(sent) == "甲" * 150
    assert all(len(s) <= 64 for s in sent)

@pytest.mark.asyncio
async def test_empty_message_returns_failed_receipt_without_calling_platform():
    adapter = _adapter()
    receipt = await adapter.send_message("group%456", MessageChain.from_text(""))
    assert receipt.status == "failed" and receipt.reason == "empty_message"
    adapter._bot.send_group_msg.assert_not_awaited()

@pytest.mark.asyncio
async def test_terminate_then_restart_can_send_again():
    adapter = _adapter()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    await adapter.terminate()
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 2}
    receipt = await adapter.send_message("group%456", MessageChain.from_text("回滚后"))
    assert receipt.status == "success" and receipt.message_ids == ("2",)

@pytest.mark.asyncio
async def test_long_message_stops_after_first_failed_chunk_without_resend():
    adapter = _adapter(message_text_limit=64)
    adapter._bot.send_group_msg.side_effect = [{"message_id": 1}, ActionFailed({"retcode": 100}), {"message_id": 3}]
    receipt = await adapter.send_message("group%456", MessageChain.from_text("甲" * 150))
    assert receipt.status == "partial"
    assert receipt.message_ids == ("1",)
    assert receipt.failed_index == 1
    assert adapter._bot.send_group_msg.await_count == 2

@pytest.mark.asyncio
async def test_long_message_unknown_chunk_result_blocks_further_chunks():
    adapter = _adapter(message_text_limit=64)
    adapter._bot.send_group_msg.side_effect = [{"message_id": 1}, None, {"message_id": 3}]
    receipt = await adapter.send_message("group%456", MessageChain.from_text("甲" * 150))
    assert receipt.status == "unknown"
    assert receipt.message_ids == ("1",)
    assert adapter._bot.send_group_msg.await_count == 2

@pytest.mark.asyncio
async def test_sends_to_same_target_are_serialized_and_other_targets_are_not_blocked():
    adapter = _adapter()
    started: list[str] = []
    gate = asyncio.Event()

    async def slow_send(**kwargs: Any) -> dict[str, int]:
        started.append(str(kwargs.get("group_id") or kwargs.get("user_id")))
        if len(started) == 1:
            await gate.wait()
        return {"message_id": len(started)}

    adapter._bot.send_group_msg.side_effect = slow_send
    adapter._bot.send_private_msg.side_effect = slow_send
    first = asyncio.create_task(adapter.send_text("group%456", "一"))
    await asyncio.sleep(0)
    second = asyncio.create_task(adapter.send_text("group%456", "二"))
    other = asyncio.create_task(adapter.send_text("private%789", "三"))
    await asyncio.sleep(0.05)
    assert started == ["456", "789"]
    gate.set()
    receipts = await asyncio.gather(first, second, other)
    assert [r.status for r in receipts] == ["success"] * 3
    assert started == ["456", "789", "456"]

@pytest.mark.asyncio
async def test_outbound_turns_rejects_when_full_and_after_close(caplog: pytest.LogCaptureFixture):
    turns = OutboundTurns()
    release = asyncio.Event()
    entered = asyncio.Event()
    outcomes: list[str] = []

    async def hold(target: str):
        async def operation() -> None:
            entered.set()
            await release.wait()
        try:
            await turns.run(target, operation)
        except RuntimeError:
            outcomes.append("closed")
        outcomes.append("caller_survived")

    holders = [asyncio.create_task(hold(f"t{i}")) for i in range(64)]
    await entered.wait()
    await asyncio.sleep(0)
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError):
            await turns.run("extra", lambda: asyncio.sleep(0))
        await turns.close()
    await asyncio.gather(*holders)
    # 关闭只取消发送子任务, 调用方任务自身不被取消
    assert outcomes.count("caller_survived") == 64 and outcomes.count("closed") == 64
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("发送队列不可用 closed=False inflight=64 target=extra" in m for m in warnings)
    assert any("关闭发送队列, 取消 64 个在途发送" in m for m in infos)
    with pytest.raises(RuntimeError):
        await turns.run("t0", lambda: asyncio.sleep(0))
    assert not turns.locks

@pytest.mark.asyncio
async def test_outbound_turns_external_cancel_keeps_cancelled_error_and_child_failure_keeps_cause():
    turns = OutboundTurns()
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(10)

    caller = asyncio.create_task(turns.run("t", slow))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not turns.tasks and not turns.locks

    async def failing() -> None:
        raise ValueError("platform said no")

    with pytest.raises(ValueError, match="platform said no"):
        await turns.run("t", failing)

@pytest.mark.asyncio
async def test_cancel_waits_for_cleanup_before_same_target_proceeds():
    """调用方取消后, 同目标新发送必须等旧任务清理完成"""
    turns = OutboundTurns()
    log: list[str] = []
    cleanup_release = asyncio.Event()

    async def first_op() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            log.append("first_cleanup_started")
            await cleanup_release.wait()
            log.append("first_cleanup_done")

    async def second_op() -> None:
        log.append("second_started")

    caller = asyncio.create_task(turns.run("t", first_op))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(turns.run("t", second_op))
    caller.cancel()
    await asyncio.sleep(0.05)
    assert log == ["first_cleanup_started"]
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await second
    assert log == ["first_cleanup_started", "first_cleanup_done", "second_started"]
    assert not turns.tasks and not turns.locks

@pytest.mark.asyncio
async def test_cancel_settle_timeout_keeps_registry_until_child_finishes(monkeypatch: pytest.MonkeyPatch):
    """清理超过有界等待时调用方先退出, 登记保留且同目标不并发; 重复取消不改变语义"""
    monkeypatch.setattr("satrap.core.platform.onebot.outbound.CANCEL_SETTLE_TIMEOUT", 0.2)
    turns = OutboundTurns()
    log: list[str] = []
    cleanup_release = asyncio.Event()

    async def slow_cleanup() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            log.append("cleanup_started")
            await cleanup_release.wait()
            log.append("cleanup_done")

    async def second_op() -> None:
        log.append("second_started")

    caller = asyncio.create_task(turns.run("t", slow_cleanup))
    await asyncio.sleep(0.05)
    caller.cancel()
    await asyncio.sleep(0.05)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    # 调用方已退出, 子任务仍在清理: 登记保留, 同目标不并发放行
    assert turns.tasks and turns.locks.get("t") is not None
    second = asyncio.create_task(turns.run("t", second_op))
    await asyncio.sleep(0.1)
    assert "second_started" not in log
    cleanup_release.set()
    await second
    assert log == ["cleanup_started", "cleanup_done", "second_started"]
    assert not turns.tasks and not turns.locks

@pytest.mark.asyncio
async def test_close_waits_for_cleaning_task_and_reports_timeout(monkeypatch: pytest.MonkeyPatch):
    """close 覆盖清理中的任务; 超时明确报告未终态"""
    monkeypatch.setattr("satrap.core.platform.onebot.outbound.CANCEL_SETTLE_TIMEOUT", 0.2)
    turns = OutboundTurns()
    cleanup_release = asyncio.Event()

    async def slow_cleanup() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            await cleanup_release.wait()

    caller = asyncio.create_task(turns.run("t", slow_cleanup))
    await asyncio.sleep(0.05)
    closer = asyncio.create_task(turns.close())
    await asyncio.sleep(0.6)
    assert closer.done()
    assert turns.tasks, "超时后未终态任务必须保持登记"
    cleanup_release.set()
    await asyncio.sleep(0.1)
    assert not turns.tasks
    with pytest.raises(RuntimeError, match="已关闭"):
        await caller

class _Resolver(Protocol):
    """声明测试使用的 DNS 解析函数接口"""

    def __call__(
        self,
        host: str,
        port: int,
        *,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        """返回地址解析记录"""
        ...

def _resolver_for(*addresses: str) -> _Resolver:
    """构造返回指定 IP 的 getaddrinfo 替身"""
    def resolve(
        host: str,
        port: int,
        *,
        type: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        """返回固定 IP 的解析记录"""
        family = socket.AF_INET6 if any(":" in item for item in addresses) else socket.AF_INET
        return [(family, type, 6, "", (address, port)) for address in addresses]

    return resolve

@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fe80::1"])
def test_outbound_url_rejects_non_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    """出站 URL 校验应拒绝本机, 私网, 链路本地和元数据地址"""
    monkeypatch.setattr(socket, "getaddrinfo", _resolver_for(address))
    with pytest.raises(UnsafeOutboundURLError, match="非公网地址"):
        validate_outbound_http_url("https://attacker.example/resource")

def test_outbound_url_rejects_redirect_to_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """每一跳重定向都必须重新执行公网地址校验"""
    monkeypatch.setattr(socket, "getaddrinfo", _resolver_for("127.0.0.1"))
    with pytest.raises(UnsafeOutboundURLError, match="非公网地址"):
        validate_outbound_redirect("https://public.example/start", "http://metadata.local/latest")

def test_trusted_service_host_may_use_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式配置的服务主机允许使用私网地址而不扩大到其他主机"""
    monkeypatch.setattr(socket, "getaddrinfo", _resolver_for("10.0.0.2"))
    assert validate_outbound_http_url(
        "https://misskey.internal/file",
        trusted_hosts=("misskey.internal",),
    ) == "https://misskey.internal/file"
    with pytest.raises(UnsafeOutboundURLError):
        validate_outbound_http_url("https://other.internal/file")


_TRUST_LOOPBACK = ("127.0.0.1",)

class _OutboundHandler(BaseHTTPRequestHandler):
    """回环测试服务器的固定路由处理器"""

    def log_message(self, format: str, *args: object) -> None:
        """静默请求日志"""

    def do_GET(self) -> None:
        """按固定路由响应, 覆盖正文/重定向/超限三类行为"""
        if self.path == "/ok":
            self._respond(200, b"hello-outbound")
        elif self.path == "/big":
            self._respond(200, b"x" * (64 * 1024))
        elif self.path == "/stream-big":
            self.send_response(200)   # HTTP/1.0 关闭定界, 无 Content-Length
            self.end_headers()
            self.wfile.write(b"y" * (64 * 1024))
        elif self.path == "/redirect":
            self._redirect("/ok")
        elif self.path == "/redirect-private":
            self._redirect("http://169.254.169.254/latest/meta-data")
        elif self.path == "/redirect-loop":
            self._redirect("/redirect-loop")
        else:
            self._respond(404, b"not found")

    def _respond(self, status: int, body: bytes) -> None:
        """发送带 Content-Length 的响应"""
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        """发送 302 重定向"""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

@pytest.fixture()
def local_server() -> Iterator[str]:
    """启动回环测试服务器并返回 base URL (端口随机)"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OutboundHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, port = cast("tuple[str, int]", server.server_address)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

def test_safe_sync_get_rejects_private_target_without_trust(local_server: str) -> None:
    """未配置可信主机时回环地址应在连接前被拒绝"""
    with pytest.raises(UnsafeOutboundURLError):
        safe_sync_get(f"{local_server}/ok")

@pytest.mark.asyncio
async def test_safe_async_get_rejects_private_target_without_trust(local_server: str) -> None:
    """未配置可信主机时回环地址应在连接前被拒绝 (异步)"""
    with pytest.raises(UnsafeOutboundURLError):
        await safe_async_get(f"{local_server}/ok")

def test_safe_sync_get_downloads_from_trusted_loopback(local_server: str) -> None:
    """可信回环主机应完成固定地址下载"""
    response = safe_sync_get(f"{local_server}/ok", trusted_hosts=_TRUST_LOOPBACK)
    assert response.status_code == 200
    assert response.content == b"hello-outbound"

@pytest.mark.asyncio
async def test_safe_async_get_downloads_from_trusted_loopback(local_server: str) -> None:
    """可信回环主机应完成固定地址下载 (异步)"""
    response = await safe_async_get(f"{local_server}/ok", trusted_hosts=_TRUST_LOOPBACK)
    assert response.status_code == 200
    assert response.content == b"hello-outbound"

@pytest.mark.asyncio
async def test_safe_async_get_follows_redirect_with_per_hop_validation(local_server: str) -> None:
    """同主机重定向应逐跳校验后跟随"""
    response = await safe_async_get(f"{local_server}/redirect", trusted_hosts=_TRUST_LOOPBACK)
    assert response.status_code == 200
    assert response.content == b"hello-outbound"
    assert response.url.endswith("/ok")

@pytest.mark.asyncio
async def test_safe_async_get_blocks_redirect_to_private_address(local_server: str) -> None:
    """重定向到非公网地址应在该跳被拒绝"""
    with pytest.raises(UnsafeOutboundURLError):
        await safe_async_get(f"{local_server}/redirect-private", trusted_hosts=_TRUST_LOOPBACK)

@pytest.mark.asyncio
async def test_safe_async_get_enforces_redirect_limit(local_server: str) -> None:
    """循环重定向应触发次数上限"""
    with pytest.raises(UnsafeOutboundURLError, match="重定向次数超过限制"):
        await safe_async_get(f"{local_server}/redirect-loop", trusted_hosts=_TRUST_LOOPBACK, max_redirects=3)

@pytest.mark.asyncio
async def test_safe_async_get_enforces_byte_limit(local_server: str) -> None:
    """Content-Length 超限应在读取前拒绝"""
    with pytest.raises(OutboundResponseTooLargeError):
        await safe_async_get(
            f"{local_server}/big", trusted_hosts=_TRUST_LOOPBACK, max_response_bytes=1024
        )

@pytest.mark.asyncio
async def test_safe_async_get_enforces_streaming_byte_limit(local_server: str) -> None:
    """无 Content-Length 的响应应由流式计数拦截"""
    with pytest.raises(OutboundResponseTooLargeError):
        await safe_async_get(
            f"{local_server}/stream-big", trusted_hosts=_TRUST_LOOPBACK, max_response_bytes=1024
        )

def test_safe_sync_get_enforces_streaming_byte_limit(local_server: str) -> None:
    """无 Content-Length 的响应应由限量读取拦截 (同步)"""
    with pytest.raises(OutboundResponseTooLargeError):
        safe_sync_get(
            f"{local_server}/stream-big", trusted_hosts=_TRUST_LOOPBACK, max_response_bytes=1024
        )

@pytest.mark.asyncio
async def test_pinned_resolver_only_returns_validated_addresses() -> None:
    """固定解析器应拒绝错配主机且仅返回校验地址"""
    target = ResolvedOutboundTarget(
        url="http://example.com",
        hostname="example.com",
        port=80,
        addresses=((socket.AF_INET, "93.184.216.34"),),
    )
    resolver = _PinnedResolver(target)
    with pytest.raises(OSError):
        await resolver.resolve("evil.example.com", 80)
    results = await resolver.resolve("example.com", 80)
    assert [item["host"] for item in results] == ["93.184.216.34"]

@pytest.mark.asyncio
async def test_download_file_uses_env_trusted_hosts(
    local_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """download_file 默认读取 SATRAP_TRUSTED_DOWNLOAD_HOSTS 环境变量"""
    monkeypatch.delenv(TRUSTED_DOWNLOAD_HOSTS_ENV_NAME, raising=False)
    target = tmp_path / "file.bin"
    with pytest.raises(UnsafeOutboundURLError):
        await download_file(f"{local_server}/ok", str(target))

    monkeypatch.setenv(TRUSTED_DOWNLOAD_HOSTS_ENV_NAME, "127.0.0.1")
    saved = await download_file(f"{local_server}/ok", str(target))
    assert Path(saved).read_bytes() == b"hello-outbound"

@pytest.mark.asyncio
async def test_download_file_accepts_explicit_trusted_hosts(
    local_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """download_file 显式 trusted_hosts 应覆盖环境变量缺省"""
    monkeypatch.delenv(TRUSTED_DOWNLOAD_HOSTS_ENV_NAME, raising=False)
    target = tmp_path / "file.bin"
    saved = await download_file(f"{local_server}/ok", str(target), trusted_hosts=["127.0.0.1"])
    assert Path(saved).read_bytes() == b"hello-outbound"

async def _event(private: bool = False, **settings: object) -> tuple[OneBotAdapter, MessageEvent]:
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    adapter._bot = AsyncMock()
    adapter._bot.send_group_msg.return_value = {"message_id": 1}
    adapter._bot.send_private_msg.return_value = {"message_id": 1}
    if private:
        await adapter._handle_private_message({
            "self_id": 10000, "user_id": 123, "message_id": 77, "message_type": "private",
            "message": [{"type": "text", "data": {"text": "你好"}}], "sender": {"user_id": 123},
        })
    else:
        await adapter._handle_group_message({
            "self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 77,
            "message_type": "group", "message": [{"type": "text", "data": {"text": "你好"}}],
        })
    return adapter, adapter._event_queue.get_nowait()

def _sent_segments(adapter: OneBotAdapter, private: bool = False):
    mock = adapter._bot.send_private_msg if private else adapter._bot.send_group_msg
    return [call.kwargs["message"] for call in mock.await_args_list]

@pytest.mark.asyncio
async def test_default_policy_keeps_plain_reply():
    adapter, event = await _event()
    await event.send(MessageChain.from_text("回复"))
    assert _sent_segments(adapter) == [[{"type": "text", "data": {"text": "回复"}}]]

@pytest.mark.asyncio
async def test_quote_and_mention_are_prepended_with_space_after_at():
    adapter, event = await _event(reply_with_quote=True, reply_with_mention=True)
    await event.send(MessageChain.from_text("回复"))
    assert _sent_segments(adapter) == [[
        {"type": "reply", "data": {"id": "77"}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": " 回复"}},
    ]]

@pytest.mark.asyncio
async def test_existing_reply_and_at_are_not_duplicated():
    adapter, event = await _event(reply_with_quote=True, reply_with_mention=True)
    await event.send(MessageChain([Reply(id="5"), At(qq="123"), Plain("已有")]))
    segments = _sent_segments(adapter)[0]
    assert [s["type"] for s in segments] == ["reply", "at", "text"]
    assert segments[0]["data"]["id"] == "5" and segments[2]["data"]["text"] == "已有"

@pytest.mark.asyncio
async def test_at_other_user_does_not_suppress_sender_mention():
    adapter, event = await _event(reply_with_mention=True)
    await event.send(MessageChain([At(qq="999"), Plain("转告")]))
    segments = _sent_segments(adapter)[0]
    assert [s["data"].get("qq") for s in segments[:2]] == ["123", "999"]

@pytest.mark.asyncio
async def test_private_chat_never_decorated():
    adapter, event = await _event(private=True, reply_with_quote=True, reply_with_mention=True)
    await event.send(MessageChain.from_text("回复"))
    assert _sent_segments(adapter, private=True) == [[{"type": "text", "data": {"text": "回复"}}]]

@pytest.mark.asyncio
async def test_missing_source_message_id_skips_quote_but_keeps_mention():
    adapter, event = await _event(reply_with_quote=True, reply_with_mention=True)
    event.platform_message.message_id = ""
    event._call_origin = event._call_origin.__class__(**{**event._call_origin.__dict__, "source_message_id": ""})
    await event.send(MessageChain.from_text("回复"))
    assert [s["type"] for s in _sent_segments(adapter)[0]] == ["at", "text"]

@pytest.mark.asyncio
async def test_non_text_first_component_gets_no_space_and_original_chain_untouched():
    adapter, event = await _event(reply_with_mention=True)
    image = Image(file="http://x/1.png")
    chain = MessageChain([image, Plain("说明")])
    await event.send(chain)
    assert chain.components == [image, chain.components[1]] and len(chain.components) == 2
    segments = _sent_segments(adapter)[0]
    assert [s["type"] for s in segments] == ["at", "image", "text"]
    assert segments[2]["data"]["text"] == "说明"

@pytest.mark.asyncio
async def test_stream_fallback_decorates_only_first_non_empty_chunk():
    adapter, event = await _event(reply_with_quote=True, reply_with_mention=True)

    async def gen():
        yield MessageChain([])
        yield MessageChain.from_text("第一块")
        yield MessageChain.from_text("第二块")

    await event.send_streaming(gen(), use_fallback=True)
    sent = _sent_segments(adapter)
    assert [s["type"] for s in sent[0]] == ["reply", "at", "text"]
    assert sent[1] == [{"type": "text", "data": {"text": "第二块"}}]

@pytest.mark.asyncio
async def test_stream_merged_decorates_once_and_long_split_only_first_chunk():
    adapter, event = await _event(reply_with_quote=True, message_text_limit=64)
    adapter._bot.send_group_msg.side_effect = [{"message_id": 1}, {"message_id": 2}]

    async def gen():
        yield MessageChain.from_text("甲" * 60)
        yield MessageChain.from_text("乙" * 60)

    await event.send_streaming(gen())
    sent = _sent_segments(adapter)
    assert len(sent) == 2
    assert sent[0][0]["type"] == "reply" and all(s["type"] == "text" for s in sent[1])

@pytest.mark.asyncio
async def test_group_override_can_enable_quote_for_one_group():
    adapter, event = await _event(wake_group_overrides={"456": {"reply_with_quote": True}})
    await event.send(MessageChain.from_text("回复"))
    assert _sent_segments(adapter)[0][0]["type"] == "reply"

@pytest.mark.parametrize("value", ["true", 1, None])
def test_reply_policy_requires_boolean(value: object):
    with pytest.raises(ValueError, match="reply_with_quote"):
        validate_wake_policy({"reply_with_quote": value})

def test_time_rules_cannot_change_reply_policy():
    rules: dict[str, object] = {"wake_time_rules": [{"start": "01:00", "end": "02:00", "settings": {"reply_with_quote": True}}]}
    with pytest.raises(ValueError):
        validate_wake_overrides(rules)
    groups: dict[str, object] = {"wake_group_overrides": {"1": {"reply_with_mention": False}}}
    validate_wake_overrides(groups)

async def make_event():
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
    adapter._bot = AsyncMock()
    await adapter._handle_group_message({
        "self_id": 10000, "user_id": 123, "group_id": 456, "message_id": 10,
        "message_type": "group", "message": [{"type": "text", "data": {"text": "你好"}}],
    })
    return adapter, adapter._event_queue.get_nowait()

@pytest.mark.asyncio
@pytest.mark.parametrize("response,status,ids", [
    ({"message_id": 42}, "success", ("42",)),
    ({"message_id": "-12"}, "success", ("-12",)),
    (None, "unknown", ()), ({}, "unknown", ()),
    ({"message_id": True}, "unknown", ()),
])
async def test_onebot_receipt_requires_platform_confirmation(response, status, ids):
    adapter, event = await make_event()
    adapter._bot.send_group_msg.return_value = response
    await event.send(MessageChain.from_text("回复"))
    assert event.last_send_receipt.status == status
    assert event.last_send_receipt.message_ids == ids
    assert event.has_send_operation()

@pytest.mark.asyncio
async def test_unavailable_client_is_not_marked_sent():
    adapter, event = await make_event()
    adapter._bot = None
    await event.send(MessageChain.from_text("回复"))
    assert event.last_send_receipt == SendReceipt("failed", reason="client_unavailable")
    assert not event.has_send_operation()

@pytest.mark.asyncio
async def test_timeout_suppresses_whole_response_retry():
    adapter, event = await make_event()
    adapter._bot.send_group_msg.side_effect = TimeoutError()
    await event.send(MessageChain.from_text("回复"))
    assert event.last_send_receipt.status == "unknown"
    assert event.has_send_operation()
    assert adapter._bot.send_group_msg.await_count == 1

@pytest.mark.asyncio
async def test_stream_stops_on_unknown_and_preserves_confirmed_ids():
    adapter, event = await make_event()
    adapter._bot.send_group_msg.side_effect = [{"message_id": 42}, None, {"message_id": 44}]

    async def chunks():
        for text in ("第一块", "第二块", "第三块"):
            yield MessageChain.from_text(text)

    await event.send_streaming(chunks(), use_fallback=True)
    assert adapter._bot.send_group_msg.await_count == 2
    assert event.last_send_receipt == SendReceipt("unknown", ("42",), 1, "missing_message_id")
    assert event.has_send_operation()

@pytest.mark.asyncio
async def test_stream_producer_failure_preserves_sent_prefix():
    adapter, event = await make_event()
    adapter._bot.send_group_msg.return_value = {"message_id": 42}

    async def chunks():
        yield MessageChain.from_text("第一块")
        raise RuntimeError("producer failed")

    await event.send_streaming(chunks(), use_fallback=True)
    assert event.last_send_receipt == SendReceipt("unknown", ("42",), 1, "stream_interrupted")
    assert event.has_send_operation()

@pytest.mark.asyncio
async def test_later_failure_cannot_clear_previous_send_marker():
    adapter, event = await make_event()
    adapter._bot.send_group_msg.return_value = {"message_id": 42}
    await event.send(MessageChain.from_text("第一块"))
    adapter._bot = None
    await event.send(MessageChain.from_text("第二块"))
    assert event.last_send_receipt.status == "failed"
    assert event.has_send_operation()

def test_partial_receipt_aggregation():
    receipt = combine_receipts([SendReceipt("success", ("1",)), SendReceipt("failed", reason="disabled")])
    assert receipt == SendReceipt("partial", ("1",), 1, "disabled")
    assert receipt.suppress_fallback
    assert not combine_receipts([]).suppress_fallback

@pytest.mark.asyncio
async def test_platform_explicit_rejection_is_distinct_from_timeout():
    from aiocqhttp.exceptions import ActionFailed

    adapter, event = await make_event()
    adapter._bot.send_group_msg.side_effect = ActionFailed({"retcode": 100, "wording": "private details"})
    await event.send(MessageChain.from_text("回复"))
    assert event.last_send_receipt == SendReceipt("failed", reason="action_rejected")
    assert not event.has_send_operation()

@pytest.mark.asyncio
async def test_stream_explicit_failure_returns_partial():
    from aiocqhttp.exceptions import ActionFailed

    adapter, event = await make_event()
    adapter._bot.send_group_msg.side_effect = [{"message_id": 42}, ActionFailed({"retcode": 100})]

    async def chunks():
        for text in ("第一块", "第二块", "第三块"):
            yield MessageChain.from_text(text)

    await event.send_streaming(chunks(), use_fallback=True)
    assert event.last_send_receipt == SendReceipt("partial", ("42",), 1, "action_rejected")
    assert event.has_send_operation()
    assert adapter._bot.send_group_msg.await_count == 2
