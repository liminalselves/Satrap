"""
后端内嵌 HTTP + WebSocket 服务器 (管理 API 与前端静态托管)

基于共享基类 satrap.core.utils.minihttp.MiniHTTPServer, 只保留本服务特有逻辑:
- /api/* 管理路由: health / config / session-classes / models / users / checkpoint / shutdown
- /ws/logs, /ws/status WebSocket 推送
- 静态文件 / SPA 入口 (satrap-ui/dist, 生产模式托管前端构建产物)
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote
from typing import TYPE_CHECKING, Any

from satrap.api import checkpoint as checkpoint_api
from satrap.api import user as user_api
from satrap.core.type import EmbeddingConfig, LLMConfig, ReRankConfig, safe_getattr, safe_getattr_str
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.core.utils.paths import get_data_dir, get_db_path, get_project_root

if TYPE_CHECKING:
    from satrap.core.backend.BackendManager import BackendManager

STATIC_DIR = Path(__file__).resolve().parent.parent.parent.parent / "satrap-ui" / "dist"
# 静态文件目录 - 前端构建产物


class BackendHTTPServer(MiniHTTPServer):
    """
    内嵌 HTTP 服务器, 提供管理 API

    基于共享基类 (asyncio.start_server, 零外部依赖)
    默认监听 127.0.0.1:19870, 仅接受本地连接
    """

    def __init__(self, backend: BackendManager, host: str = "127.0.0.1", port: int = 19870):
        """
        初始化 BackendHTTPServer

        参数:
        - backend: 后端实例
        - host: 监听地址
        - port: 监听端口
        """
        super().__init__(host=host, port=port, log_errors=False)
        self.backend = backend

    # ---------- 静态文件服务 ----------

    async def _serve_static(self, writer: asyncio.StreamWriter, path: str) -> bool:
        """
        服务静态文件或 SPA 入口 (仅非 API 路径)

        参数:
        - writer: 流写入器
        - path: 路径

        返回:
        - bool: 服务静态文件或 SPA 入口 (仅非 API 路径)
        """
        if path.startswith("/api/"):
            return False
        # 移除查询参数
        path = path.split("?")[0]

        if path == "/" or path == "":
            self._send_index_html(writer)
            return True
        # 默认返回 index.html (SPA 路由)

        file_path = STATIC_DIR / path.lstrip("/")
        # 尝试提供静态文件
        if file_path.exists() and file_path.is_file():
            self._send_file(writer, file_path)
            return True

        self._send_index_html(writer)
        # 所有其他路径返回 index.html (SPA 客户端路由)
        return True

    def _send_file(self, writer: asyncio.StreamWriter, file_path: Path):
        """
        发送静态文件

        参数:
        - writer: 流写入器
        - file_path: 文件路径
        """
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
        """
        发送前端入口 HTML (用于 SPA 路由)

        参数:
        - writer: 流写入器
        """
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            self._send_file(writer, index_path)
        else:
            self._send_json(writer, 404, {"error": "frontend not built"})

    # ---------- WebSocket 端点分发 ----------

    async def _ws_dispatch(
        self, path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        按路径分发到对应的 WebSocket 处理器

        参数:
        - path: 路径
        - reader: 流读取器
        - writer: 流写入器
        """
        if path == "/ws/logs":
            await self._ws_log_handler(reader, writer)
        elif path == "/ws/status":
            await self._ws_status_handler(reader, writer)
        else:
            await self._ws_close(writer, 1008, "unknown endpoint")

    async def _ws_log_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        WebSocket 日志推送处理器

        参数:
        - reader: 流读取器
        - writer: 流写入器
        """
        log_file = self._find_log_file()
        if not log_file:
            await self._ws_send(writer, {"type": "error", "message": "log file not found"})
            await self._ws_close(writer, 1008, "log file not found")
            return

        try:
            with log_file.open("rb") as f:
                f.seek(0, 2)   # 移到文件末尾
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
        # 发送历史日志 (最后 100 行)

        position = log_file.stat().st_size
        # 监控新日志
        try:
            while True:
                if reader.at_eof():
                    break
                # 检查客户端是否关闭连接

                current_size = log_file.stat().st_size
                # 检查文件是否有新内容
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
                    position = 0
                    # 文件被截断, 重新从头开始

                await asyncio.sleep(0.5)   # 500ms 轮询间隔

        except asyncio.CancelledError:
            pass
        except Exception as e:
            try:
                await self._ws_send(writer, {"type": "error", "message": str(e)})
            except Exception:
                pass

    async def _ws_status_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """
        WebSocket 状态推送处理器

        参数:
        - reader: 流读取器
        - writer: 流写入器
        """
        last_status = None

        try:
            while True:
                if reader.at_eof():
                    break
                # 检查客户端是否关闭连接

                health = await self.backend.health()
                # 获取当前状态
                current_status = {
                    "running": health.get("running", False),
                    "adapters": health.get("adapters", {}),
                }

                if current_status != last_status:
                    await self._ws_send(writer, {
                        "type": "status",
                        "data": current_status
                    })
                    last_status = current_status
                # 状态变化时推送

                await asyncio.sleep(2)   # 2秒轮询间隔

        except asyncio.CancelledError:
            pass
        except Exception as e:
            try:
                await self._ws_send(writer, {"type": "error", "message": str(e)})
            except Exception:
                pass

    def _find_log_file(self) -> Path | None:
        """
        查找日志文件

        返回:
        - Path | None: 查找日志文件
        """
        log_dirs = [
            get_data_dir() / "logs",
            get_project_root(),
        ]

        for log_dir in log_dirs:
            if not log_dir.exists():
                continue
            # 按修改时间排序, 取最新的 .log 文件
            log_files = sorted(
                log_dir.glob("*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            if log_files:
                return log_files[0]
        return None

    def _parse_log_level(self, line: str) -> str:
        """
        从日志行解析级别

        参数:
        - line: 文本行

        返回:
        - str: 从日志行解析级别
        """
        for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
            if level in line:
                return level
        return "INFO"

    # ---------- API 路由 ----------

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        """
        路由分发到 BackendManager 对应方法

        参数:
        - method: HTTP 方法
        - path: 路径
        - body: 请求体

        返回:
        - tuple[int, dict[str, Any]]: 路由分发到 BackendManager 对应方法
        """
        backend = self.backend

        if method == "GET" and path == "/api/health":
            return 200, await backend.health()
        # 接口: GET /api/health

        if method == "POST" and path == "/api/config/reload":
            await backend.reload_config()
            return 200, {"ok": True}
        # 接口: POST /api/config/reload

        if method == "POST" and path == "/api/shutdown":
            asyncio.get_event_loop().call_soon(backend.request_shutdown)
            return 200, {"ok": True}
        # 接口: POST /api/shutdown

        if method == "GET" and path == "/api/config/session-classes":
            mgr = backend.session_class_mgr
            if mgr:
                return 200, mgr.list_configs()
            return 200, {}
        # 接口: GET /api/config/session-classes

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
        # 接口: POST /api/config/session-classes

        if method == "POST" and path.endswith("/enable") and "/api/config/session-classes/" in path and backend.session_class_mgr:
            name = unquote(path.split("/")[5])
            backend.session_class_mgr.enable(name)
            return 200, {"ok": True}
        # 接口: POST /api/config/session-classes/{name}/enable

        if method == "POST" and path.endswith("/disable") and "/api/config/session-classes/" in path and backend.session_class_mgr:
            name = unquote(path.split("/")[5])
            backend.session_class_mgr.disable(name)
            return 200, {"ok": True}
        # 接口: POST /api/config/session-classes/{name}/disable

        path_prefix = "/api/config/session-classes/"
        # 接口: GET /api/config/session-classes/{name}
        if method == "GET" and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            cfg = backend.session_class_mgr.get_config(name)
            if cfg is None:
                return 404, {"error": "not found"}
            return 200, cfg

        if method == "PUT" and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            payload = json.loads(body)
            if "params" in payload:
                backend.session_class_mgr.set_config(name, payload["params"])
            return 200, {"ok": True}
        # 接口: PUT /api/config/session-classes/{name}

        if method == "DELETE" and path.startswith(path_prefix) and backend.session_class_mgr:
            name = unquote(path[len(path_prefix):])
            if backend.session_class_mgr.remove_config(name):
                return 200, {"ok": True}
            return 404, {"error": "not found"}
        # 接口: DELETE /api/config/session-classes/{name}

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
        # 接口: GET /api/config/models?type=llm

        model_prefix = "/api/config/models/"
        # 接口: POST/PATCH/DELETE /api/config/models/{type}/{name}
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

        user_db = safe_getattr_str(safe_getattr(backend, "config"), "user_db_path") or get_db_path("user_info.db")
        # 接口: GET /api/users (列表, 支持 ?limit=) / GET /api/users?user_id=xxx (详情)
        # 接口: GET /api/user/sessions?user_id=xxx
        # 接口: POST /api/user/{create|update|delete|bind|unbind}
        if path.startswith("/api/user"):
            try:
                if method == "GET" and path.startswith("/api/users"):
                    user_id = self._query_param(path, "user_id")
                    if user_id:
                        return 200, user_api.get_user(user_db, user_id)
                    limit = int(self._query_param(path, "limit") or "200")
                    return 200, user_api.list_users(user_db, limit=limit)
                if method == "GET" and path.startswith("/api/user/sessions"):
                    user_id = self._query_param(path, "user_id")
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

        db = backend.checkpoint_db_path
        # 接口: GET /api/checkpoints?conversation=xxx
        # 接口: GET /api/checkpoint/branches?conversation=xxx
        # 接口: GET /api/checkpoint/lineage?checkpoint_id=xxx
        # 接口: GET /api/checkpoint/audit?conversation=xxx
        # 接口: POST /api/checkpoint/{create|rollback|retry|fork}
        if path.startswith("/api/checkpoint"):
            try:
                if method == "GET" and path.startswith("/api/checkpoints"):
                    conv = self._query_param(path, "conversation")
                    return 200, checkpoint_api.list_checkpoints(db, conv)
                if method == "GET" and path.startswith("/api/checkpoint/branches"):
                    conv = self._query_param(path, "conversation")
                    return 200, checkpoint_api.list_branches(db, conv)
                if method == "GET" and path.startswith("/api/checkpoint/lineage"):
                    cid = self._query_param(path, "checkpoint_id")
                    return 200, checkpoint_api.trace_lineage(db, cid)
                if method == "GET" and path.startswith("/api/checkpoint/audit"):
                    conv = self._query_param(path, "conversation")
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
