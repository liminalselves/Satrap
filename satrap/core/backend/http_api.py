from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satrap.core.backend.BackendManager import BackendManager


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

    def _send_json(self, writer: asyncio.StreamWriter, status: int, data: dict):
        """发送 JSON 响应"""
        resp_body = json.dumps(data, ensure_ascii=False).encode()
        status_text = "OK" if status == 200 else "Error"
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(resp_body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + resp_body)

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict]:
        """路由分发到 BackendManager 对应方法"""
        backend = self.backend

        # GET /api/health
        if method == "GET" and path == "/api/health":
            return 200, await backend.health()

        # POST /api/config/reload
        if method == "POST" and path == "/api/config/reload":
            await backend.reload_config()
            return 200, {"ok": True}

        # GET /api/config/session-classes
        if method == "GET" and path == "/api/config/session-classes":
            mgr = backend.session_class_mgr
            if mgr:
                return 200, mgr.list_configs()
            return 200, {}

        # POST /api/config/session-classes/{name}/enable
        if method == "POST" and path.endswith("/enable") and "/api/config/session-classes/" in path:
            name = path.split("/")[5]
            backend.session_class_mgr.enable(name)
            return 200, {"ok": True}

        # POST /api/config/session-classes/{name}/disable
        if method == "POST" and path.endswith("/disable") and "/api/config/session-classes/" in path:
            name = path.split("/")[5]
            backend.session_class_mgr.disable(name)
            return 200, {"ok": True}

        # GET /api/config/session-classes/{name}
        path_prefix = "/api/config/session-classes/"
        if method == "GET" and path.startswith(path_prefix):
            name = path[len(path_prefix):]
            cfg = backend.session_class_mgr.get_config(name)
            if cfg is None:
                return 404, {"error": "not found"}
            return 200, cfg

        # PUT /api/config/session-classes/{name}
        if method == "PUT" and path.startswith(path_prefix):
            name = path[len(path_prefix):]
            payload = json.loads(body)
            if "params" in payload:
                backend.session_class_mgr.set_config(name, payload["params"])
            return 200, {"ok": True}

        # GET /api/config/models?type=llm
        if method == "GET" and path.startswith("/api/config/models"):
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

        return 404, {"error": f"unknown route: {method} {path}"}
