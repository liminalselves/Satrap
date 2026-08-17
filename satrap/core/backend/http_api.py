from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import struct
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from typing import TYPE_CHECKING, Any

from satrap.api import checkpoint as checkpoint_api
from satrap.api import user as user_api
from satrap.core.type import EmbeddingConfig, LLMConfig, ReRankConfig, safe_getattr, safe_getattr_str
from satrap.core.utils.paths import get_data_dir, get_db_path, get_project_root

if TYPE_CHECKING:
    from satrap.core.backend.BackendManager import BackendManager


# CORS 配置 - 允许前端开发服务器跨域访问
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",  # 生产环境应限制为具体域名
    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
}

# 静态文件目录 - 前端构建产物
STATIC_DIR = Path(__file__).resolve().parent.parent.parent.parent / "satrap-ui" / "dist"

# WebSocket 魔术字符串 (RFC 6455)
WS_MAGIC_STRING = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _query_param(path: str, key: str) -> str:
    """提取请求路径 query 参数值 (缺失或未传返回空串)"""
    values = parse_qs(urlsplit(path).query).get(key)
    return unquote(values[0]) if values else ""


class BackendHTTPServer:
    """内嵌 HTTP 服务器, 提供管理 API

    使用 asyncio.start_server 实现, 零外部依赖.
    默认监听 127.0.0.1:19870, 仅接受本地连接.
    """

    def __init__(self, backend: BackendManager, host: str = "127.0.0.1", port: int = 19870):
        self.backend = backend
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self):
        """启动 HTTP 服务器"""
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )
        asyncio.ensure_future(self._server.serve_forever())

    async def stop(self):
        """停止 HTTP 服务器"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """处理单次 HTTP 连接"""
        try:
            raw_request = await reader.readuntil(b"\r\n\r\n")
            first_line = raw_request.split(b"\r\n")[0].decode()
            parts = first_line.split(" ")
            method = parts[0]
            path = parts[1] if len(parts) > 1 else "/"

            # 处理 CORS 预检请求
            if method == "OPTIONS":
                self._send_cors_preflight(writer)
                return

            # WebSocket 升级请求
            if path.startswith("/ws/"):
                await self._handle_websocket(reader, writer, raw_request, path)
                return

            # 静态文件服务 (非 API 路径)
            if not path.startswith("/api/"):
                await self._serve_static(writer, path)
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
            self._send_json(writer, 500, {"error": str(e)})
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _send_cors_preflight(self, writer: asyncio.StreamWriter):
        """发送 CORS 预检响应"""
        cors_headers = "".join(f"{k}: {v}\r\n" for k, v in CORS_HEADERS.items())
        header = (
            "HTTP/1.1 204 No Content\r\n"
            f"{cors_headers}"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(header)

    async def _serve_static(self, writer: asyncio.StreamWriter, path: str):
        """服务静态文件或 SPA 入口"""
        # 移除查询参数
        path = path.split("?")[0]
        
        # 默认返回 index.html (SPA 路由)
        if path == "/" or path == "":
            self._send_index_html(writer)
            return
        
        # 尝试提供静态文件
        file_path = STATIC_DIR / path.lstrip("/")
        if file_path.exists() and file_path.is_file():
            self._send_file(writer, file_path)
            return
        
        # 所有其他路径返回 index.html (SPA 客户端路由)
        self._send_index_html(writer)

    def _send_json(self, writer: asyncio.StreamWriter, status: int, data: dict[str, Any]):
        """发送 JSON 响应 (带 CORS 头)"""
        resp_body = json.dumps(data, ensure_ascii=False).encode()
        status_text = "OK" if status == 200 else "Error"
        cors_headers = "".join(f"{k}: {v}\r\n" for k, v in CORS_HEADERS.items())
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(resp_body)}\r\n"
            f"{cors_headers}"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + resp_body)

    def _send_file(self, writer: asyncio.StreamWriter, file_path: Path):
        """发送静态文件"""
        try:
            content = file_path.read_bytes()
            mime_type, _ = mimetypes.guess_type(str(file_path))
            mime_type = mime_type or "application/octet-stream"
            header = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: {mime_type}\r\n"
                f"Content-Length: {len(content)}\r\n"
                f"Cache-Control: public, max-age=31536000\r\n"
                f"Connection: close\r\n\r\n"
            ).encode()
            writer.write(header + content)
        except FileNotFoundError:
            self._send_json(writer, 404, {"error": "file not found"})

    def _send_index_html(self, writer: asyncio.StreamWriter):
        """发送前端入口 HTML (用于 SPA 路由)"""
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            self._send_file(writer, index_path)
        else:
            self._send_json(writer, 404, {"error": "frontend not built"})

    # ==================== WebSocket 支持 ====================

    async def _handle_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        raw_request: bytes,
        path: str,
    ):
        """处理 WebSocket 连接升级和消息"""
        # 解析 WebSocket 握手请求
        headers = self._parse_headers(raw_request)
        ws_key = headers.get("sec-websocket-key", "")
        
        if not ws_key:
            self._send_json(writer, 400, {"error": "missing Sec-WebSocket-Key"})
            return

        # 计算接受键
        accept_key = base64.b64encode(
            hashlib.sha1((ws_key + WS_MAGIC_STRING).encode()).digest()
        ).decode()

        # 发送升级响应
        upgrade_response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key}\r\n"
            "\r\n"
        ).encode()
        writer.write(upgrade_response)
        await writer.drain()

        # 根据路径分发到不同的处理器
        if path == "/ws/logs":
            await self._ws_log_handler(reader, writer)
        elif path == "/ws/status":
            await self._ws_status_handler(reader, writer)
        else:
            await self._ws_close(writer, 1008, "unknown endpoint")

    def _parse_headers(self, raw_request: bytes) -> dict[str, str]:
        """解析 HTTP 请求头"""
        headers = {}
        lines = raw_request.decode().split("\r\n")
        for line in lines[1:]:  # 跳过请求行
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        return headers

    async def _ws_send(self, writer: asyncio.StreamWriter, data: dict[str, Any]):
        """发送 WebSocket 消息"""
        payload = json.dumps(data, ensure_ascii=False).encode()
        header = bytearray()
        
        # FIN=1, Opcode=1 (文本帧)
        header.append(0x81)
        
        # 载荷长度
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

    async def _ws_close(self, writer: asyncio.StreamWriter, code: int, reason: str):
        """发送 WebSocket 关闭帧"""
        payload = struct.pack(">H", code) + reason.encode()
        header = bytearray([0x88])  # FIN=1, Opcode=8 (关闭帧)
        header.append(len(payload))
        writer.write(bytes(header) + payload)
        await writer.drain()

    async def _ws_log_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """WebSocket 日志推送处理器"""
        log_file = self._find_log_file()
        if not log_file:
            await self._ws_send(writer, {"type": "error", "message": "log file not found"})
            await self._ws_close(writer, 1008, "log file not found")
            return

        # 发送历史日志 (最后 100 行)
        try:
            with log_file.open("rb") as f:
                f.seek(0, 2)  # 移到文件末尾
                file_size = f.tell()
                # 读取最后 10KB 或整个文件
                read_size = min(10 * 1024, file_size)
                f.seek(file_size - read_size)
                data = f.read().decode("utf-8", errors="replace")
                lines = data.strip().split("\n")[-100:]
                for line in lines:
                    if line.strip():
                        await self._ws_send(writer, {
                            "type": "log",
                            "data": {"content": line, "level": self._parse_log_level(line)}
                        })
        except Exception as e:
            await self._ws_send(writer, {"type": "error", "message": str(e)})

        # 监控新日志
        position = log_file.stat().st_size
        try:
            while True:
                # 检查客户端是否关闭连接
                if reader.at_eof():
                    break

                # 检查文件是否有新内容
                current_size = log_file.stat().st_size
                if current_size > position:
                    with log_file.open("rb") as f:
                        f.seek(position)
                        new_data = f.read().decode("utf-8", errors="replace")
                        position = f.tell()
                    
                    for line in new_data.strip().split("\n"):
                        if line.strip():
                            await self._ws_send(writer, {
                                "type": "log",
                                "data": {"content": line, "level": self._parse_log_level(line)}
                            })
                elif current_size < position:
                    # 文件被截断，重新从头开始
                    position = 0

                await asyncio.sleep(0.5)  # 500ms 轮询间隔

        except asyncio.CancelledError:
            pass
        except Exception as e:
            try:
                await self._ws_send(writer, {"type": "error", "message": str(e)})
            except Exception:
                pass

    async def _ws_status_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """WebSocket 状态推送处理器"""
        last_status = None
        
        try:
            while True:
                # 检查客户端是否关闭连接
                if reader.at_eof():
                    break

                # 获取当前状态
                health = await self.backend.health()
                current_status = {
                    "running": health.get("running", False),
                    "adapters": health.get("adapters", {}),
                }

                # 状态变化时推送
                if current_status != last_status:
                    await self._ws_send(writer, {
                        "type": "status",
                        "data": current_status
                    })
                    last_status = current_status

                await asyncio.sleep(2)  # 2秒轮询间隔

        except asyncio.CancelledError:
            pass
        except Exception as e:
            try:
                await self._ws_send(writer, {"type": "error", "message": str(e)})
            except Exception:
                pass

    def _find_log_file(self) -> Path | None:
        """查找日志文件"""
        log_dirs = [
            get_data_dir() / "logs",
            get_project_root(),
        ]
        
        for log_dir in log_dirs:
            if not log_dir.exists():
                continue
            # 按修改时间排序，取最新的 .log 文件
            log_files = sorted(
                log_dir.glob("*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            if log_files:
                return log_files[0]
        return None

    def _parse_log_level(self, line: str) -> str:
        """从日志行解析级别"""
        for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
            if level in line:
                return level
        return "INFO"

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        """路由分发到 BackendManager 对应方法"""
        backend = self.backend

        # GET /api/health
        if method == "GET" and path == "/api/health":
            return 200, await backend.health()

        # POST /api/config/reload
        if method == "POST" and path == "/api/config/reload":
            await backend.reload_config()
            return 200, {"ok": True}

        # POST /api/shutdown
        if method == "POST" and path == "/api/shutdown":
            asyncio.get_event_loop().call_soon(backend.request_shutdown)
            return 200, {"ok": True}

        # GET /api/config/session-classes
        if method == "GET" and path == "/api/config/session-classes":
            mgr = backend.session_class_mgr
            if mgr:
                return 200, mgr.list_configs()
            return 200, {}

        # POST /api/config/session-classes
        if method == "POST" and path == "/api/config/session-classes" and backend.session_class_mgr:
            try:
                payload = json.loads(body or b"{}")
                backend.session_class_mgr.register_by_class_path(
                    str(payload.get("name", "")),
                    str(payload.get("class_path", "")),
                    description=str(payload.get("description", "")),
                    context_key=str(payload.get("context_key", "")),
                    model_key=str(payload.get("model_key", "")),
                )
                return 200, {"ok": True}
            except Exception as e:
                return 400, {"error": str(e)}

        # POST /api/config/session-classes/{name}/enable
        if method == "POST" and path.endswith("/enable") and "/api/config/session-classes/" in path and backend.session_class_mgr:
            name = unquote(path.split("/")[5])
            backend.session_class_mgr.enable(name)
            return 200, {"ok": True}

        # POST /api/config/session-classes/{name}/disable
        if method == "POST" and path.endswith("/disable") and "/api/config/session-classes/" in path and backend.session_class_mgr:
            name = unquote(path.split("/")[5])
            backend.session_class_mgr.disable(name)
            return 200, {"ok": True}

        # GET /api/config/session-classes/{name}
        path_prefix = "/api/config/session-classes/"
        if method == "GET" and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            cfg = backend.session_class_mgr.get_config(name)
            if cfg is None:
                return 404, {"error": "not found"}
            return 200, cfg

        # PUT /api/config/session-classes/{name}
        if method == "PUT" and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            payload = json.loads(body)
            if "params" in payload:
                backend.session_class_mgr.set_config(name, payload["params"])
            return 200, {"ok": True}

        # DELETE /api/config/session-classes/{name}
        if method == "DELETE" and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            if backend.session_class_mgr.remove_config(name):
                return 200, {"ok": True}
            return 404, {"error": "not found"}

        # GET /api/config/models?type=llm
        if method == "GET" and path.startswith("/api/config/models") and backend.model_config_manager:
            typ = "llm"
            qs = path.split("?", 1)[1] if "?" in path else ""
            for pair in qs.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    if k == "type":
                        typ = v
            mgr = backend.model_config_manager
            if typ == "llm":
                return 200, mgr.list_llm_configs(mask_api_key=True)
            elif typ == "embedding":
                return 200, mgr.list_embedding_configs(mask_api_key=True)
            elif typ == "rerank":
                return 200, mgr.list_rerank_configs(mask_api_key=True)
            return 200, {}

        # POST/PATCH/DELETE /api/config/models/{type}/{name}
        model_prefix = "/api/config/models/"
        if path.startswith(model_prefix) and backend.model_config_manager:
            parts = path[len(model_prefix):].split("/", 1)
            if len(parts) != 2:
                return 404, {"error": f"unknown route: {method} {path}"}
            typ = unquote(parts[0])
            name = unquote(parts[1])
            mgr = backend.model_config_manager
            try:
                if typ not in ("llm", "embedding", "rerank"):
                    return 400, {"error": f"未知模型类型: {typ}"}

                if method == "POST":
                    payload = json.loads(body or b"{}")
                    payload["name"] = name
                    if typ == "llm":
                        mgr.set_llm_config(LLMConfig(**payload), name=name)
                    elif typ == "embedding":
                        mgr.set_embedding_config(EmbeddingConfig(**payload), name=name)
                    else:
                        mgr.set_rerank_config(ReRankConfig(**payload), name=name)
                    return 200, {"ok": True}
                if method == "PATCH":
                    payload = json.loads(body or b"{}")
                    if typ == "llm":
                        mgr.update_llm_config(name=name, **payload)
                    elif typ == "embedding":
                        mgr.update_embedding_config(name=name, **payload)
                    else:
                        mgr.update_rerank_config(name=name, **payload)
                    return 200, {"ok": True}
                if method == "DELETE":
                    if typ == "llm":
                        removed = mgr.remove_llm_config(name=name)
                    elif typ == "embedding":
                        removed = mgr.remove_embedding_config(name=name)
                    else:
                        removed = mgr.remove_rerank_config(name=name)
                    if removed:
                        return 200, {"ok": True}
                    return 404, {"error": "not found"}
            except Exception as e:
                return 400, {"error": str(e)}

        # GET /api/users (列表, 支持 ?limit=) / GET /api/users?user_id=xxx (详情)
        # GET /api/user/sessions?user_id=xxx
        # POST /api/user/{create|update|delete|bind|unbind}
        user_db = safe_getattr_str(safe_getattr(backend, "config"), "user_db_path") or get_db_path("user_info.db")
        if path.startswith("/api/user"):
            try:
                if method == "GET" and path.startswith("/api/users"):
                    user_id = _query_param(path, "user_id")
                    if user_id:
                        return 200, user_api.get_user(user_db, user_id)
                    limit = int(_query_param(path, "limit") or "200")
                    return 200, user_api.list_users(user_db, limit=limit)
                if method == "GET" and path.startswith("/api/user/sessions"):
                    user_id = _query_param(path, "user_id")
                    if not user_id:
                        return 400, {"error": "缺少 user_id 参数"}
                    return 200, user_api.list_user_sessions(user_db, user_id)
                if method == "POST":
                    payload = json.loads(body or b"{}")
                    user_id = str(payload.get("user_id", "")).strip()
                    if not user_id:
                        return 400, {"error": "缺少 user_id 参数"}
                    if path == "/api/user/create":
                        return 200, user_api.create_user(
                            user_db, user_id,
                            platform=str(payload.get("platform", "")),
                            nickname=str(payload.get("nickname", "")),
                        )
                    if path == "/api/user/update":
                        return 200, user_api.update_user(
                            user_db, user_id,
                            nickname=payload.get("nickname"),
                            platform=payload.get("platform"),
                        )
                    if path == "/api/user/delete":
                        return 200, user_api.delete_user(user_db, user_id)
                    if path == "/api/user/bind":
                        session_id = str(payload.get("session_id", "")).strip()
                        if not session_id:
                            return 400, {"error": "缺少 session_id 参数"}
                        return 200, user_api.bind_session(user_db, user_id, session_id)
                    if path == "/api/user/unbind":
                        session_id = str(payload.get("session_id", "")).strip()
                        if not session_id:
                            return 400, {"error": "缺少 session_id 参数"}
                        return 200, user_api.unbind_session(user_db, user_id, session_id)
            except (ValueError, KeyError, IndexError) as e:
                return 400, {"error": str(e)}

        # GET /api/checkpoints?conversation=xxx
        # GET /api/checkpoint/branches?conversation=xxx
        # GET /api/checkpoint/lineage?checkpoint_id=xxx
        # GET /api/checkpoint/audit?conversation=xxx
        # POST /api/checkpoint/{create|rollback|retry|fork}
        db = backend.checkpoint_db_path
        if path.startswith("/api/checkpoint"):
            try:
                if method == "GET" and path.startswith("/api/checkpoints"):
                    conv = _query_param(path, "conversation")
                    return 200, checkpoint_api.list_checkpoints(db, conv)
                if method == "GET" and path.startswith("/api/checkpoint/branches"):
                    conv = _query_param(path, "conversation")
                    return 200, checkpoint_api.list_branches(db, conv)
                if method == "GET" and path.startswith("/api/checkpoint/lineage"):
                    cid = _query_param(path, "checkpoint_id")
                    return 200, checkpoint_api.trace_lineage(db, cid)
                if method == "GET" and path.startswith("/api/checkpoint/audit"):
                    conv = _query_param(path, "conversation")
                    return 200, checkpoint_api.list_mutations(db, conv)
                if method == "POST":
                    payload = json.loads(body or b"{}")
                    conv = str(payload.get("conversation", "")).strip()
                    if not conv:
                        return 400, {"error": "缺少 conversation 参数"}
                    if path == "/api/checkpoint/create":
                        return 200, checkpoint_api.create_checkpoint(
                            db, conv,
                            name=str(payload.get("name", "")),
                            description=str(payload.get("description", "")),
                        )
                    if path == "/api/checkpoint/rollback":
                        return 200, checkpoint_api.rollback_checkpoint(
                            db, conv, str(payload.get("checkpoint_id", ""))
                        )
                    if path == "/api/checkpoint/retry":
                        return 200, checkpoint_api.retry_checkpoint(
                            db, conv, str(payload.get("checkpoint_id", ""))
                        )
                    if path == "/api/checkpoint/fork":
                        cid = payload.get("checkpoint_id")
                        return 200, checkpoint_api.fork_checkpoint(
                            db, conv,
                            branch_name=str(payload.get("branch_name", "")),
                            checkpoint_id=str(cid) if cid else None,
                        )
            except (ValueError, KeyError, IndexError) as e:
                return 400, {"error": str(e)}

        return 404, {"error": f"unknown route: {method} {path}"}
