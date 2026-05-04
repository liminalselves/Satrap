from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class DaemonInfo:
    """后端进程连接信息"""
    host: str = "127.0.0.1"
    port: int = 19870

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def detect(cls) -> DaemonInfo:
        """从环境变量/默认值检测 daemon 地址"""
        host = os.getenv("SATRAP_API_HOST", "127.0.0.1")
        port = int(os.getenv("SATRAP_API_PORT", "19870"))
        return cls(host=host, port=port)


class DaemonClient:
    """HTTP 客户端, 与后端 daemon 通信"""

    def __init__(self, daemon: DaemonInfo | None = None):
        self.daemon = daemon or DaemonInfo.detect()

    def is_alive(self) -> bool:
        """检测 daemon 是否在线"""
        try:
            resp = self._request("GET", "/api/health")
            return resp.get("running", False)
        except Exception:
            return False

    def reload_config(self) -> dict:
        return self._request("POST", "/api/config/reload")

    def list_session_classes(self) -> dict:
        return self._request("GET", "/api/config/session-classes")

    def enable_session_class(self, name: str) -> dict:
        return self._request("POST", f"/api/config/session-classes/{name}/enable")

    def disable_session_class(self, name: str) -> dict:
        return self._request("POST", f"/api/config/session-classes/{name}/disable")

    def get_session_class(self, name: str) -> dict:
        return self._request("GET", f"/api/config/session-classes/{name}")

    def set_session_class_params(self, name: str, params: dict) -> dict:
        return self._request("PUT", f"/api/config/session-classes/{name}", body={"params": params})

    def list_models(self, typ: str = "llm") -> dict:
        return self._request("GET", f"/api/config/models?type={typ}")

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """发送 HTTP 请求并解析 JSON 响应"""
        url = f"{self.daemon.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else "{}"
            try:
                return json.loads(err_body)
            except Exception:
                return {"error": f"HTTP {e.code}: {e.reason}"}
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError) as e:
            return {"error": f"daemon 未响应: {e}"}
