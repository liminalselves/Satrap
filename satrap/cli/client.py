from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satrap.core.backend.BackendManager import BackendConfig


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
        """从配置文件和环境变量检测 daemon 地址"""
        try:
            from satrap.core.config_loader import ConfigLoader

            config = ConfigLoader.autodetect()
            return cls.from_config(config)
        except Exception:
            host = os.getenv("SATRAP_API_HOST", "127.0.0.1")
            port = int(os.getenv("SATRAP_API_PORT", "19870"))
            return cls(host=host, port=port)

    @classmethod
    def from_config(cls, config: BackendConfig) -> DaemonInfo:
        """从 BackendConfig 构建 daemon 地址"""
        host = os.getenv("SATRAP_API_HOST", config.api_host)
        port_raw = os.getenv("SATRAP_API_PORT")
        try:
            port = int(port_raw) if port_raw else int(config.api_port)
        except (TypeError, ValueError):
            port = int(config.api_port)
        return cls(host=host, port=port)


class DaemonClient:
    """HTTP 客户端, 与后端 daemon 通信"""

    def __init__(self, daemon: DaemonInfo | None = None, timeout: float = 5):
        self.daemon = daemon or DaemonInfo.detect()
        self.timeout = timeout

    def is_alive(self) -> bool:
        """检测 daemon 是否在线"""
        try:
            resp = self._request("GET", "/api/health")
            return resp.get("running", False)
        except Exception:
            return False

    def reload_config(self) -> dict:
        return self._request("POST", "/api/config/reload")

    def shutdown(self) -> dict:
        return self._request("POST", "/api/shutdown")

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
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else "{}"
            try:
                return json.loads(err_body)
            except Exception:
                return {"error": f"HTTP {e.code}: {e.reason}"}
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError) as e:
            return {"error": f"daemon 未响应: {e}"}
