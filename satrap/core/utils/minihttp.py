"""
迷你 asyncio HTTP + WebSocket 服务器基类

零外部依赖, 基于 asyncio.start_server 实现. 供后端管理 API (http_api) 与
聊天展示层 (display.server) 等服务复用; 子类只需实现三个钩子:

- async _route(method, path, body) -> (status, dict): 普通 API 路由
- async _ws_dispatch(path, reader, writer): WebSocket 端点分发 (未知端点自行关闭)
- async _serve_static(writer, path, headers) -> bool: 非 API 静态路径钩子, 返回 True 表示已处理

共享基础设施: 请求解析, CORS 预检, JSON 响应, WebSocket 握手与帧收发
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from http import HTTPStatus
import json
import struct
from collections.abc import Mapping
from typing import Any
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from satrap.core.server_auth import ServerAuth
from satrap.core.log import logger

DEFAULT_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
}
# CORS 配置 - 允许前端开发服务器跨域访问

WS_MAGIC_STRING = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# WebSocket 魔术字符串 (RFC 6455)

MAX_HEADER_BYTES = 64 * 1024
"""单个 HTTP 请求头的最大字节数"""

DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024
"""默认请求体上限, 覆盖 10 MiB 上传文件的编码开销"""

DEFAULT_HEADER_TIMEOUT = 10.0
"""完整接收 HTTP 请求头的默认超时秒数"""

DEFAULT_BODY_TIMEOUT = 30.0
"""完整接收 HTTP 请求体的默认超时秒数"""

DEFAULT_MAX_CONNECTIONS = 256
"""单个 HTTP 服务允许的最大并发连接数"""

DEFAULT_MAX_WEBSOCKET_CONNECTIONS = 64
"""单个 HTTP 服务允许的最大 WebSocket 并发连接数"""

DEFAULT_WEBSOCKET_IDLE_TIMEOUT = 300.0
"""WebSocket 未收到客户端帧时的默认空闲超时秒数"""

DEFAULT_WEBSOCKET_PING_INTERVAL = 30.0
"""WebSocket 服务端主动 ping 的默认间隔秒数"""

DEFAULT_WEBSOCKET_MAX_FRAME_BYTES = 64 * 1024
"""WebSocket 客户端单帧载荷的默认上限"""


class HTTPRequestError(Exception):
    """携带 HTTP 状态码的安全请求解析错误"""

    def __init__(self, status: int, message: str) -> None:
        """
        初始化请求解析错误

        参数:
        - status: 应返回的 HTTP 状态码
        - message: 可安全返回客户端的错误说明
        """
        super().__init__(message)
        self.status = status
        self.message = message


class WebSocketProtocolError(Exception):
    """携带关闭码的 WebSocket 协议错误"""

    def __init__(self, code: int, message: str) -> None:
        """
        初始化 WebSocket 协议错误

        参数:
        - code: WebSocket 关闭码
        - message: 可安全返回客户端的错误说明
        """
        super().__init__(message)
        self.code = code
        self.message = message


async def read_request_headers(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int = MAX_HEADER_BYTES,
    timeout: float = DEFAULT_HEADER_TIMEOUT,
) -> bytes:
    """
    在大小和时间限制内读取完整 HTTP 请求头

    参数:
    - reader: 流读取器
    - max_bytes: 请求头最大字节数
    - timeout: 完整读取超时秒数

    返回:
    - bytes: 包含结尾空行的原始请求头
    """
    try:
        raw_request = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=timeout,
        )
    except TimeoutError as error:
        raise HTTPRequestError(408, "request header timeout") from error
    except asyncio.LimitOverrunError as error:
        raise HTTPRequestError(431, "request headers too large") from error
    except asyncio.IncompleteReadError as error:
        raise HTTPRequestError(400, "incomplete request headers") from error
    if len(raw_request) > max_bytes:
        raise HTTPRequestError(431, "request headers too large")
    return raw_request


def parse_content_length(raw_request: bytes) -> int | None:
    """
    严格解析唯一的 Content-Length 请求头

    参数:
    - raw_request: 原始 HTTP 请求头

    返回:
    - int | None: 非负请求体长度, 未提供时返回 None
    """
    values = [
        raw_line.split(b":", 1)[1].strip()
        for raw_line in raw_request.split(b"\r\n")[1:]
        if raw_line.lower().startswith(b"content-length:") and b":" in raw_line
    ]
    if not values:
        return None
    if len(values) != 1:
        raise HTTPRequestError(400, "duplicate Content-Length")
    try:
        text = values[0].decode("ascii")
    except UnicodeDecodeError as error:
        raise HTTPRequestError(400, "invalid Content-Length") from error
    if not text.isdecimal():
        raise HTTPRequestError(400, "invalid Content-Length")
    return int(text)


async def read_request_body(
    reader: asyncio.StreamReader,
    raw_request: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BODY_BYTES,
    timeout: float = DEFAULT_BODY_TIMEOUT,
    required: bool = False,
) -> bytes:
    """
    在大小和时间限制内读取 Content-Length 请求体

    参数:
    - reader: 流读取器
    - raw_request: 原始 HTTP 请求头
    - max_bytes: 请求体最大字节数
    - timeout: 完整读取超时秒数
    - required: 是否要求非空请求体

    返回:
    - bytes: 请求体, 未提供且非必需时返回空字节串
    """
    content_length = parse_content_length(raw_request)
    if content_length is None or content_length == 0:
        if required:
            raise HTTPRequestError(400, "missing request body")
        return b""
    if content_length > max_bytes:
        raise HTTPRequestError(413, "request body too large")
    try:
        return await asyncio.wait_for(
            reader.readexactly(content_length),
            timeout=timeout,
        )
    except TimeoutError as error:
        raise HTTPRequestError(408, "request body timeout") from error
    except asyncio.IncompleteReadError as error:
        raise HTTPRequestError(400, "incomplete request body") from error


def query_param(path: str, key: str) -> str:
    """
    提取 query 参数值 (缺失返回空串)

    参数:
    - path: 路径
    - key: 密钥

    返回:
    - str: 空串)
    """
    values = parse_qs(urlsplit(path).query).get(key)
    return unquote(values[0]) if values else ""


class MiniHTTPServer:
    """零依赖 asyncio HTTP + WebSocket 服务器基类"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        cors_headers: dict[str, str] | None = None,
        *,
        auth: ServerAuth | None = None,
        log_errors: bool = False,
        max_header_bytes: int = MAX_HEADER_BYTES,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        header_timeout: float = DEFAULT_HEADER_TIMEOUT,
        body_timeout: float = DEFAULT_BODY_TIMEOUT,
        session_namespace: str = "api",
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_websocket_connections: int = DEFAULT_MAX_WEBSOCKET_CONNECTIONS,
        websocket_idle_timeout: float = DEFAULT_WEBSOCKET_IDLE_TIMEOUT,
        websocket_ping_interval: float = DEFAULT_WEBSOCKET_PING_INTERVAL,
        websocket_max_frame_bytes: int = DEFAULT_WEBSOCKET_MAX_FRAME_BYTES,
    ) -> None:
        """
        初始化 MiniHTTPServer

        参数:
        - host: 监听地址
        - port: 监听端口
        - cors_headers: CORS响应头
        - auth: 服务鉴权策略
        - log_errors: 日志错误
        - max_header_bytes: 请求头最大字节数
        - max_body_bytes: 请求体最大字节数
        - header_timeout: 请求头读取超时秒数
        - body_timeout: 请求体读取超时秒数
        - session_namespace: 浏览器会话 Cookie 的服务命名空间
        - max_connections: 最大并发连接数
        - max_websocket_connections: 最大 WebSocket 并发连接数
        - websocket_idle_timeout: WebSocket 客户端空闲超时秒数
        - websocket_ping_interval: WebSocket 服务端 ping 间隔秒数
        - websocket_max_frame_bytes: WebSocket 客户端单帧载荷上限
        """
        if max_connections <= 0:
            raise ValueError("max_connections must be positive")
        if max_websocket_connections <= 0:
            raise ValueError("max_websocket_connections must be positive")
        if websocket_idle_timeout <= 0:
            raise ValueError("websocket_idle_timeout must be positive")
        if websocket_ping_interval <= 0:
            raise ValueError("websocket_ping_interval must be positive")
        if websocket_max_frame_bytes <= 0:
            raise ValueError("websocket_max_frame_bytes must be positive")
        self.host = host
        self.port = port
        self._cors = cors_headers if cors_headers is not None else DEFAULT_CORS_HEADERS
        self.auth = auth or ServerAuth.create(host, port, session_namespace=session_namespace)
        self._log_errors = log_errors
        self._max_header_bytes = max_header_bytes
        self._max_body_bytes = max_body_bytes
        self._header_timeout = header_timeout
        self._body_timeout = body_timeout
        self._max_connections = max_connections
        self._max_websocket_connections = max_websocket_connections
        self._websocket_idle_timeout = websocket_idle_timeout
        self._websocket_ping_interval = websocket_ping_interval
        self._websocket_max_frame_bytes = websocket_max_frame_bytes
        self._active_connections = 0
        self._active_websockets = 0
        self._server: asyncio.Server | None = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动 HTTP 服务器"""
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            self.port,
            limit=self._max_header_bytes + 4,
        )
        asyncio.ensure_future(self._server.serve_forever())

    async def stop(self) -> None:
        """停止 HTTP 服务器"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ---------- 连接处理 ----------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        处理单次 HTTP 连接

        参数:
        - reader: 流读取器
        - writer: 流写入器
        """
        if self._active_connections >= self._max_connections:
            self._send_json(writer, 503, {"error": "service unavailable"})
            try:
                writer.close()
            except Exception:
                pass
            return

        self._active_connections += 1
        try:
            raw_request = await read_request_headers(
                reader,
                max_bytes=self._max_header_bytes,
                timeout=self._header_timeout,
            )
            first_line = raw_request.split(b"\r\n")[0].decode()
            parts = first_line.split(" ")
            method = parts[0]
            path = parts[1] if len(parts) > 1 else "/"
            headers = self._parse_headers(raw_request)
            origin = headers.get("origin")

            if not self.auth.origin_allowed(origin):
                self._send_json(writer, 403, {"error": "origin not allowed"})
                return

            if method == "OPTIONS":
                self._send_cors_preflight(writer, origin)
                return
            # CORS 预检

            if path.startswith("/ws/"):
                await self._handle_websocket(reader, writer, raw_request, path)
                return
            # WebSocket 升级请求

            if method == "POST" and urlsplit(path).path == "/auth/session":
                peer = writer.get_extra_info("peername")
                peer_tuple = cast(tuple[object, ...], peer) if isinstance(peer, tuple) else ()
                peer_host = str(peer_tuple[0]) if peer_tuple else ""
                if not self.auth.can_bootstrap(peer_host, origin, headers):
                    self._send_json(writer, 401, {"error": "unauthorized"}, origin)
                    return
                self._send_json(
                    writer,
                    200,
                    {"ok": True},
                    origin,
                    {"Set-Cookie": self.auth.session_cookie()},
                )
                return

            if method == "POST" and urlsplit(path).path == "/auth/logout":
                if not self.auth.cookie_authorized(headers):
                    self._send_json(writer, 401, {"error": "unauthorized"}, origin)
                    return
                self.auth.revoke_cookie(headers)
                self._send_json(
                    writer,
                    200,
                    {"ok": True},
                    origin,
                    {"Set-Cookie": self.auth.expired_session_cookie()},
                )
                return

            if await self._serve_static(writer, path, headers):
                return
            # 静态文件 / SPA 钩子 (子类覆写, 默认不处理)

            public_ui_config = method == "GET" and urlsplit(path).path == "/ui-config.json"
            if not public_ui_config and not self.auth.authorized(headers):
                self._send_json(writer, 401, {"error": "unauthorized"}, origin)
                return

            body = await read_request_body(
                reader,
                raw_request,
                max_bytes=self._max_body_bytes,
                timeout=self._body_timeout,
            )

            status, data = await self._route(method, path, body)
            self._send_json(writer, status, data, origin)
        except HTTPRequestError as error:
            self._send_json(writer, error.status, {"error": error.message})
        except Exception as e:
            if self._log_errors:
                logger.error(f"[HTTP] 请求处理异常: {e}")
            self._send_json(writer, 500, {"error": "internal server error"})
        finally:
            self._active_connections -= 1
            try:
                writer.close()
            except Exception:
                pass

    # ---------- 响应 ----------

    def _send_cors_preflight(self, writer: asyncio.StreamWriter, origin: str | None = None) -> None:
        """
        发送 CORS 预检响应

        参数:
        - writer: 流写入器
        - origin: 请求来源
        """
        response_headers = {**self._cors, **self.auth.cors_headers(origin)}
        cors = "".join(f"{k}: {v}\r\n" for k, v in response_headers.items())
        header = (
            "HTTP/1.1 204 No Content\r\n"
            f"{cors}"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(header)

    def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        data: Mapping[str, object],
        origin: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """
        发送 JSON 响应 (带 CORS 头)

        参数:
        - writer: 流写入器
        - status: 状态码
        - data: 输入数据
        - origin: 请求来源
        - extra_headers: 额外响应头
        """
        resp = json.dumps(data, ensure_ascii=False).encode()
        try:
            status_text = HTTPStatus(status).phrase
        except ValueError:
            status_text = "Unknown Status"
        response_headers = {**self.auth.cors_headers(origin), **(extra_headers or {})}
        cors = "".join(f"{k}: {v}\r\n" for k, v in response_headers.items())
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(resp)}\r\n"
            f"{cors}"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + resp)

    def _query_param(self, path: str, key: str) -> str:
        """
        提取 query 参数值 (缺失返回空串)

        参数:
        - path: 路径
        - key: 密钥

        返回:
        - str: 空串)
        """
        return query_param(path, key)

    # ---------- WebSocket 支持 ----------

    async def _handle_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        raw_request: bytes,
        path: str,
    ) -> None:
        """
        处理 WebSocket 连接升级并按路径分发

        参数:
        - reader: 流读取器
        - writer: 流写入器
        - raw_request: 原始请求
        - path: 路径
        """
        headers = self._parse_headers(raw_request)
        origin = headers.get("origin")
        if not self.auth.origin_allowed(origin):
            self._send_json(writer, 403, {"error": "origin not allowed"})
            return
        if not self.auth.authorized(headers):
            self._send_json(writer, 401, {"error": "unauthorized"}, origin)
            return
        ws_key = headers.get("sec-websocket-key", "")
        if not ws_key:
            self._send_json(writer, 400, {"error": "missing Sec-WebSocket-Key"})
            return
        if self._active_websockets >= self._max_websocket_connections:
            self._send_json(writer, 503, {"error": "service unavailable"}, origin)
            return

        self._active_websockets += 1
        try:
            accept_key = base64.b64encode(
                hashlib.sha1((ws_key + WS_MAGIC_STRING).encode()).digest()
            ).decode()
            writer.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept_key}\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            dispatch_task = asyncio.create_task(self._ws_dispatch(path, reader, writer))
            monitor_task = asyncio.create_task(self._ws_client_monitor(reader, writer))
            done, pending = await asyncio.wait(
                {dispatch_task, monitor_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                error = None if task.cancelled() else task.exception()
                if error is not None and self._log_errors:
                    logger.error(f"[WebSocket] 连接处理异常: {type(error).__name__}")
        finally:
            self._active_websockets -= 1

    def _parse_headers(self, raw_request: bytes) -> dict[str, str]:
        """
        解析 HTTP 请求头

        参数:
        - raw_request: 原始请求

        返回:
        - dict[str, str]: 解析 HTTP 请求头
        """
        headers: dict[str, str] = {}
        for line in raw_request.decode().split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return headers

    async def _ws_send(self, writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
        """
        发送 WebSocket 文本帧

        参数:
        - writer: 流写入器
        - data: 输入数据
        """
        payload = json.dumps(data, ensure_ascii=False).encode()
        header = bytearray([0x81])   # FIN=1, Opcode=1 (文本帧)
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        writer.write(bytes(header) + payload)
        await writer.drain()

    async def _ws_send_frame(
        self,
        writer: asyncio.StreamWriter,
        opcode: int,
        payload: bytes = b"",
    ) -> None:
        """
        发送 WebSocket 服务端帧

        参数:
        - writer: 流写入器
        - opcode: WebSocket 操作码
        - payload: 帧载荷
        """
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        writer.write(bytes(header) + payload)
        await writer.drain()

    async def _ws_read_frame(
        self,
        reader: asyncio.StreamReader,
    ) -> tuple[int, bytes]:
        """
        读取并校验一个客户端 WebSocket 帧

        参数:
        - reader: 流读取器

        返回:
        - tuple[int, bytes]: 操作码与解掩码后的载荷
        """
        first, second = await reader.readexactly(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        payload_length = second & 0x7F
        if payload_length == 126:
            payload_length = struct.unpack(">H", await reader.readexactly(2))[0]
        elif payload_length == 127:
            payload_length = struct.unpack(">Q", await reader.readexactly(8))[0]
        if not masked:
            raise WebSocketProtocolError(1002, "client frame must be masked")
        if payload_length > self._websocket_max_frame_bytes:
            raise WebSocketProtocolError(1009, "frame too large")
        if opcode >= 0x8 and (not final or payload_length > 125):
            raise WebSocketProtocolError(1002, "invalid control frame")
        mask = await reader.readexactly(4)
        payload = await reader.readexactly(payload_length)
        return opcode, bytes(value ^ mask[index % 4] for index, value in enumerate(payload))

    async def _ws_client_monitor(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        读取客户端控制帧并回收空闲 WebSocket

        参数:
        - reader: 流读取器
        - writer: 流写入器
        """
        loop = asyncio.get_running_loop()
        last_activity = loop.time()
        next_ping = last_activity + self._websocket_ping_interval
        while True:
            now = loop.time()
            idle_deadline = last_activity + self._websocket_idle_timeout
            timeout = max(0.0, min(idle_deadline, next_ping) - now)
            try:
                opcode, payload = await asyncio.wait_for(
                    self._ws_read_frame(reader),
                    timeout=timeout,
                )
            except TimeoutError:
                now = loop.time()
                if now >= idle_deadline:
                    await self._ws_close(writer, 1001, "idle timeout")
                    return
                if now >= next_ping:
                    await self._ws_send_frame(writer, 0x9)
                    next_ping = now + self._websocket_ping_interval
                continue
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            except WebSocketProtocolError as error:
                await self._ws_close(writer, error.code, error.message)
                return

            last_activity = loop.time()
            if opcode == 0x8:
                close_payload = payload[:125] if payload else struct.pack(">H", 1000)
                await self._ws_send_frame(writer, 0x8, close_payload)
                return
            if opcode == 0x9:
                await self._ws_send_frame(writer, 0xA, payload)

    async def _ws_close(self, writer: asyncio.StreamWriter, code: int, reason: str) -> None:
        """
        发送 WebSocket 关闭帧

        参数:
        - writer: 流写入器
        - code: 代码内容
        - reason: 原因说明
        """
        payload = struct.pack(">H", code) + reason.encode()[:123]
        await self._ws_send_frame(writer, 0x8, payload)

    # ---------- 钩子 (子类实现) ----------

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        """
        路由分发 (子类实现)

        参数:
        - method: HTTP 方法
        - path: 路径
        - body: 请求体

        返回:
        - tuple[int, dict[str, Any]]: 路由分发 (子类实现)
        """
        raise NotImplementedError

    async def _ws_dispatch(
        self, path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        WebSocket 端点分发 (子类实现, 未知端点自行关闭)

        参数:
        - path: 路径
        - reader: 流读取器
        - writer: 流写入器
        """
        await self._ws_close(writer, 1008, "unknown endpoint")

    async def _serve_static(
        self,
        writer: asyncio.StreamWriter,
        path: str,
        request_headers: dict[str, str] | None = None,
    ) -> bool:
        """
        静态文件 / SPA 钩子 (子类覆写), 返回 True 表示已处理

        参数:
        - writer: 流写入器
        - path: 路径
        - request_headers: 小写键名的 HTTP 请求头

        返回:
        - bool:  True 表示已处理
        """
        return False
