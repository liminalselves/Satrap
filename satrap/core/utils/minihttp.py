"""迷你 asyncio HTTP + WebSocket 服务器基类

零外部依赖, 基于 asyncio.start_server 实现。供后端管理 API (http_api) 与
聊天展示层 (display.server) 等服务复用; 子类只需实现三个钩子:

- async _route(method, path, body) -> (status, dict): 普通 API 路由
- async _ws_dispatch(path, reader, writer): WebSocket 端点分发 (未知端点自行关闭)
- async _serve_static(writer, path) -> bool: 非 API 静态路径钩子, 返回 True 表示已处理

共享基础设施: 请求解析, CORS 预检, JSON 响应, WebSocket 握手与帧收发。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

# CORS 配置 - 允许前端开发服务器跨域访问
DEFAULT_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
}

# WebSocket 魔术字符串 (RFC 6455)
WS_MAGIC_STRING = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def query_param(path: str, key: str) -> str:
    """提取 query 参数值 (缺失返回空串)"""
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
        log_errors: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self._cors = cors_headers if cors_headers is not None else DEFAULT_CORS_HEADERS
        self._log_errors = log_errors
        self._server: asyncio.Server | None = None

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        """启动 HTTP 服务器"""
        self._server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        asyncio.ensure_future(self._server.serve_forever())

    async def stop(self) -> None:
        """停止 HTTP 服务器"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ---------------- 连接处理 ----------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """处理单次 HTTP 连接"""
        try:
            raw_request = await reader.readuntil(b"\r\n\r\n")
            first_line = raw_request.split(b"\r\n")[0].decode()
            parts = first_line.split(" ")
            method = parts[0]
            path = parts[1] if len(parts) > 1 else "/"

            # CORS 预检
            if method == "OPTIONS":
                self._send_cors_preflight(writer)
                return

            # WebSocket 升级请求
            if path.startswith("/ws/"):
                await self._handle_websocket(reader, writer, raw_request, path)
                return

            # 静态文件 / SPA 钩子 (子类覆写, 默认不处理)
            if await self._serve_static(writer, path):
                return

            # 读取请求体 (Content-Length)
            body = b""
            cl_idx = raw_request.lower().find(b"content-length:")
            if cl_idx >= 0:
                cl_end = raw_request.find(b"\r\n", cl_idx)
                cl_line = raw_request[cl_idx:cl_end].decode()
                cl = int(cl_line.split(":")[1].strip())
                body = await reader.readexactly(cl)

            status, data = await self._route(method, path, body)
            self._send_json(writer, status, data)
        except asyncio.IncompleteReadError:
            self._send_json(writer, 400, {"error": "bad request"})
        except Exception as e:
            if self._log_errors:
                from satrap.core.log import logger

                logger.error(f"[HTTP] 请求处理异常: {e}")
            self._send_json(writer, 500, {"error": str(e)})
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # ---------------- 响应 ----------------

    def _send_cors_preflight(self, writer: asyncio.StreamWriter) -> None:
        """发送 CORS 预检响应"""
        cors = "".join(f"{k}: {v}\r\n" for k, v in self._cors.items())
        header = (
            "HTTP/1.1 204 No Content\r\n"
            f"{cors}"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(header)

    def _send_json(self, writer: asyncio.StreamWriter, status: int, data: dict[str, Any]) -> None:
        """发送 JSON 响应 (带 CORS 头)"""
        resp = json.dumps(data, ensure_ascii=False).encode()
        status_text = "OK" if status == 200 else "Error"
        cors = "".join(f"{k}: {v}\r\n" for k, v in self._cors.items())
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(resp)}\r\n"
            f"{cors}"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + resp)

    def _query_param(self, path: str, key: str) -> str:
        """提取 query 参数值 (缺失返回空串)"""
        return query_param(path, key)

    # ---------------- WebSocket ----------------

    async def _handle_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        raw_request: bytes,
        path: str,
    ) -> None:
        """处理 WebSocket 连接升级并按路径分发"""
        headers = self._parse_headers(raw_request)
        ws_key = headers.get("sec-websocket-key", "")
        if not ws_key:
            self._send_json(writer, 400, {"error": "missing Sec-WebSocket-Key"})
            return

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
        await self._ws_dispatch(path, reader, writer)

    def _parse_headers(self, raw_request: bytes) -> dict[str, str]:
        """解析 HTTP 请求头"""
        headers: dict[str, str] = {}
        for line in raw_request.decode().split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return headers

    async def _ws_send(self, writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
        """发送 WebSocket 文本帧"""
        payload = json.dumps(data, ensure_ascii=False).encode()
        header = bytearray([0x81])  # FIN=1, Opcode=1 (文本帧)
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

    async def _ws_close(self, writer: asyncio.StreamWriter, code: int, reason: str) -> None:
        """发送 WebSocket 关闭帧"""
        payload = struct.pack(">H", code) + reason.encode()
        writer.write(bytes(bytearray([0x88, len(payload)])) + payload)
        await writer.drain()

    # ---------------- 钩子 (子类实现) ----------------

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        """路由分发 (子类实现)"""
        raise NotImplementedError

    async def _ws_dispatch(
        self, path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """WebSocket 端点分发 (子类实现, 未知端点自行关闭)"""
        await self._ws_close(writer, 1008, "unknown endpoint")

    async def _serve_static(self, writer: asyncio.StreamWriter, path: str) -> bool:
        """静态文件 / SPA 钩子 (子类覆写), 返回 True 表示已处理"""
        return False
