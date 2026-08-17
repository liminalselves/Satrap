"""聊天展示层独立 HTTP + WebSocket 服务

与平台后端 (BackendManager) 完全隔离:
- 不初始化平台适配器 / 事件分发 / Pipeline / SessionManager / UserManager
- 仅经 ModelConfigManager 读 .satrap/model_config.json (与平台后端同一份配置)
- 独立端口 (默认 19872), 独立进程: python -m satrap.display.server

API (前缀 /api/chat/):
- GET  /api/chat/health                     健康检查
- GET  /api/chat/models                     LLM 配置名列表
- POST /api/chat/conversations              新建会话 {model, think, system_prompt} -> {conversation_id}
- GET  /api/chat/conversations              会话列表 (display db)
- GET  /api/chat/turns?conversation=xxx     对话轮次 (含工具明细)
- POST /api/chat/send                       发送 {conversation, text, think} -> 立即返回, WS 推流
- GET  /api/chat/plugins                    插件清单 (扫描 + 启用状态)
- POST /api/chat/plugins/{name}/enable      启用插件 (对活动会话即时 install)
- POST /api/chat/plugins/{name}/disable     停用插件 (对活动会话即时 uninstall)
- POST /api/chat/plugins/{name}/capability  能力独立启停 {kind, cap, enabled}
- GET  /api/chat/plugins/{name}/config      插件配置 (schema + 当前值)
- PUT  /api/chat/plugins/{name}/config      保存插件配置 {config}
- GET  /api/chat/memories?scope=xxx         列出记忆
- POST /api/chat/memories                   添加记忆 {title, content, tags, importance, scope}
- PUT  /api/chat/memories/{id}              更新记忆 {title, content, tags, importance}
- DELETE /api/chat/memories/{id}?scope=xxx  删除记忆

WebSocket:
- /ws/chat?conversation=xxx                 订阅会话实时事件 (thinking/content/tool/turn_done)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.log import logger
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.recorder import list_conversations
from satrap.display.service import ChatService

# CORS 配置 - 允许前端开发服务器跨域访问
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
}

WS_MAGIC_STRING = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19872


def _query(path: str, key: str) -> str:
    """提取 query 参数值 (缺失返回空串)"""
    values = parse_qs(urlsplit(path).query).get(key)
    return unquote(values[0]) if values else ""


class ChatHTTPServer:
    """聊天展示层 HTTP + WS 服务器 (零依赖 asyncio)"""

    def __init__(
        self,
        service: ChatService,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.service = service
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        asyncio.ensure_future(self._server.serve_forever())
        logger.info(f"[聊天服务] HTTP API: http://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ---------------- 连接处理 ----------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw_request = await reader.readuntil(b"\r\n\r\n")
            first_line = raw_request.split(b"\r\n")[0].decode()
            parts = first_line.split(" ")
            method = parts[0]
            path = parts[1] if len(parts) > 1 else "/"

            if method == "OPTIONS":
                self._send_cors_preflight(writer)
                return

            if path.startswith("/ws/"):
                await self._handle_websocket(reader, writer, raw_request, path)
                return

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
            logger.error(f"[聊天服务] 请求处理异常: {e}")
            self._send_json(writer, 500, {"error": str(e)})
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _send_cors_preflight(self, writer: asyncio.StreamWriter) -> None:
        cors = "".join(f"{k}: {v}\r\n" for k, v in CORS_HEADERS.items())
        writer.write(f"HTTP/1.1 204 No Content\r\n{cors}Connection: close\r\n\r\n".encode())

    def _send_json(self, writer: asyncio.StreamWriter, status: int, data: dict[str, Any]) -> None:
        resp = json.dumps(data, ensure_ascii=False).encode()
        status_text = "OK" if status == 200 else "Error"
        cors = "".join(f"{k}: {v}\r\n" for k, v in CORS_HEADERS.items())
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(resp)}\r\n"
            f"{cors}"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + resp)

    # ---------------- 路由 ----------------

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        svc = self.service
        clean = path.split("?", 1)[0]

        if method == "GET" and clean == "/api/chat/health":
            return 200, {"ok": True, "conversations": len(svc._conversations)}

        if method == "GET" and clean == "/api/chat/models":
            return 200, {"models": svc.list_models()}

        # 模型配置管理
        if method == "GET" and clean == "/api/chat/models/detail":
            return 200, svc.list_models_detail()

        if method == "POST" and clean == "/api/chat/models":
            payload = json.loads(body or b"{}")
            return 200, svc.add_model(payload)

        if clean.startswith("/api/chat/models/"):
            model_name = unquote(clean[len("/api/chat/models/"):])
            if method == "PUT":
                payload = json.loads(body or b"{}")
                return 200, svc.update_model(model_name, payload)
            if method == "DELETE":
                return 200, svc.delete_model(model_name)
            return 405, {"error": f"method not allowed: {method}"}

        if method == "POST" and clean == "/api/chat/conversations":
            payload = json.loads(body or b"{}")
            cid = await svc.create_conversation(
                model=str(payload.get("model") or "default"),
                system_prompt=str(payload.get("system_prompt") or "") or None,
            )
            return 200, {"ok": True, "conversation_id": cid}

        if method == "GET" and clean == "/api/chat/conversations":
            return 200, {"conversations": list_conversations()}

        # DELETE /api/chat/conversations/{id}
        if method == "DELETE" and clean.startswith("/api/chat/conversations/"):
            conv_id = unquote(clean[len("/api/chat/conversations/"):])
            if not conv_id:
                return 400, {"error": "缺少 conversation_id"}
            return 200, await svc.delete_conversation(conv_id)

        if method == "GET" and clean == "/api/chat/turns":
            conv = _query(path, "conversation")
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            return 200, {"turns": svc.list_turns(conv)}

        if method == "POST" and clean == "/api/chat/send":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            text = str(payload.get("text") or "")
            think = str(payload.get("think") or "off")
            attachments = payload.get("attachments")  # list[dict] | None
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            result = await svc.send(conv, text, think=think, attachments=attachments)
            return (200 if result.get("ok") else 400), result

        # 文件上传
        if method == "POST" and clean == "/api/chat/upload":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            file_name = str(payload.get("file_name") or "")
            file_data_b64 = str(payload.get("file_data") or "")
            if not conv or not file_name or not file_data_b64:
                return 400, {"error": "缺少 conversation / file_name / file_data"}
            import base64 as _b64
            try:
                file_data = _b64.b64decode(file_data_b64)
            except Exception:
                return 400, {"error": "file_data base64 解码失败"}
            return 200, svc.save_upload(conv, file_name, file_data)

        # Retry / Fork
        if method == "POST" and clean == "/api/chat/retry":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            result = await svc.retry(conv)
            return (200 if result.get("ok") else 400), result

        if method == "POST" and clean == "/api/chat/fork":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            turn_index = int(payload.get("turn_index") or 0)
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            result = await svc.fork(conv, turn_index)
            return (200 if result.get("ok") else 400), result

        # 取消生成
        if method == "POST" and clean == "/api/chat/cancel":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            result = await svc.cancel(conv)
            return (200 if result.get("ok") else 400), result

        if method == "GET" and clean == "/api/chat/plugins":
            return 200, {"plugins": svc.list_plugins()}

        # GET/PUT /api/chat/plugins/{name}/config (需在 POST 分支之前匹配)
        if clean.startswith("/api/chat/plugins/") and clean.endswith("/config"):
            rest = clean[len("/api/chat/plugins/"):]
            name = unquote(rest[:rest.index("/")])
            if method == "GET":
                return 200, svc.get_plugin_config(name)
            if method == "PUT":
                payload = json.loads(body or b"{}")
                cfg = payload.get("config")
                if not isinstance(cfg, dict):
                    return 400, {"error": "缺少 config 对象"}
                return 200, svc.save_plugin_config(name, cfg)
            return 405, {"error": f"method not allowed: {method}"}

        # POST /api/chat/plugins/{name}/enable|disable|capability
        if method == "POST" and clean.startswith("/api/chat/plugins/"):
            rest = clean[len("/api/chat/plugins/"):]
            parts = rest.split("/")
            name = unquote(parts[0])
            action = parts[1] if len(parts) > 1 else ""
            if action == "enable":
                return 200, await svc.set_plugin_enabled(name, True)
            if action == "disable":
                return 200, await svc.set_plugin_enabled(name, False)
            if action == "capability":
                payload = json.loads(body or b"{}")
                result = await svc.set_plugin_capability(
                    name,
                    str(payload.get("kind") or ""),
                    str(payload.get("cap") or ""),
                    bool(payload.get("enabled", True)),
                )
                return (200 if result.get("ok") else 400), result
            return 404, {"error": f"unknown plugin action: {action}"}

        # GET /api/chat/memories?scope=xxx
        if method == "GET" and clean == "/api/chat/memories":
            scope = _query(path, "scope") or "web_chat"
            return 200, svc.list_memories(scope)

        # POST /api/chat/memories
        if method == "POST" and clean == "/api/chat/memories":
            payload = json.loads(body or b"{}")
            title = str(payload.get("title") or "").strip()
            content = str(payload.get("content") or "").strip()
            if not title or not content:
                return 400, {"error": "title 与 content 不能为空"}
            return 200, svc.add_memory(
                title, content,
                tags=str(payload.get("tags") or ""),
                importance=int(payload.get("importance") or 1),
                scope=str(payload.get("scope") or "web_chat"),
            )

        # PUT /api/chat/memories/{id}  /  DELETE /api/chat/memories/{id}?scope=xxx
        if clean.startswith("/api/chat/memories/"):
            memory_id = unquote(clean[len("/api/chat/memories/"):])
            if method == "PUT":
                payload = json.loads(body or b"{}")
                fields: dict[str, Any] = {}
                for key in ("title", "content", "tags", "importance"):
                    if key in payload:
                        fields[key] = payload[key]
                scope = str(payload.get("scope") or "web_chat")
                return 200, svc.update_memory(memory_id, scope=scope, **fields)
            if method == "DELETE":
                scope = _query(path, "scope") or "web_chat"
                return 200, svc.delete_memory(memory_id, scope=scope)
            return 405, {"error": f"method not allowed: {method}"}

        return 404, {"error": f"unknown route: {method} {path}"}

    # ---------------- WebSocket ----------------

    async def _handle_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        raw_request: bytes,
        path: str,
    ) -> None:
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

        if path.split("?", 1)[0] == "/ws/chat":
            await self._ws_chat_handler(reader, writer, path)
        else:
            await self._ws_close(writer, 1008, "unknown endpoint")

    @staticmethod
    def _parse_headers(raw_request: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in raw_request.decode().split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return headers

    async def _ws_send(self, writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
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
        payload = struct.pack(">H", code) + reason.encode()
        writer.write(bytes(bytearray([0x88, len(payload)])) + payload)
        await writer.drain()

    async def _ws_chat_handler(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str
    ) -> None:
        """订阅会话广播, 把队列消息经 WS 推给客户端"""
        conversation_id = _query(path, "conversation")
        if not conversation_id:
            await self._ws_send(writer, {"type": "error", "message": "missing conversation"})
            await self._ws_close(writer, 1008, "missing conversation")
            return

        queue = self.service.subscribe(conversation_id)
        await self._ws_send(writer, {"type": "subscribed", "conversation_id": conversation_id})
        try:
            while True:
                if reader.at_eof():
                    break
                    # 等待广播消息 (带超时以便检测客户端断开)
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                    await self._ws_send(writer, msg)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[聊天服务] WS 推送异常: {e}")
        finally:
            self.service.unsubscribe(conversation_id, queue)


# ---------------- 入口 ----------------

async def _run(host: str, port: int) -> None:
    model_cfg = ModelConfigManager()
    plugins = ChatPluginRegistry()
    service = ChatService(model_cfg, plugins)
    server = ChatHTTPServer(service, host=host, port=port)
    await server.start()
    print(f"Satrap 聊天服务已启动: http://{host}:{port} (按 Ctrl+C 停止)")

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    import signal
    for sig in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig), stop.set)
        except (NotImplementedError, AttributeError):
            pass
    try:
        await stop.wait()
    finally:
        await service.close()
        await server.stop()
        logger.info("[聊天服务] 已停止")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Satrap 聊天展示层服务")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
