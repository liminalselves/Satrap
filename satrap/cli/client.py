"""CLI 与 Satrap 后端守护进程通信的 HTTP 客户端"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from satrap.core.backend.BackendManager import BackendConfig


@dataclass
class DaemonInfo:
    """后端进程连接信息"""
    host: str = "127.0.0.1"
    port: int = 19870

    @property
    def base_url(self) -> str:
        """
        获取基础 URL

        返回:
        - str: 获取基础 URL
        """
        return f"http://{self.host}:{self.port}"

    @classmethod
    def detect(cls) -> DaemonInfo:
        """
        从配置文件和环境变量检测 daemon 地址

        返回:
        - DaemonInfo: 从配置文件和环境变量检测 daemon 地址
        """
        try:
            from satrap.core.config_loader import ConfigLoader   # 探测失败时仍允许客户端退回默认连接信息

            config = ConfigLoader.autodetect()
            return cls.from_config(config)
        except Exception:
            host = os.getenv("SATRAP_API_HOST", "127.0.0.1")
            port = int(os.getenv("SATRAP_API_PORT", "19870"))
            return cls(host=host, port=port)

    @classmethod
    def from_config(cls, config: BackendConfig) -> DaemonInfo:
        """
        从 BackendConfig 构建 daemon 地址

        参数:
        - config: 配置信息

        返回:
        - DaemonInfo: 从 BackendConfig 构建 daemon 地址
        """
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
        """
        初始化 DaemonClient

        参数:
        - daemon: daemon 输入值
        - timeout: 超时时间
        """
        self.daemon = daemon or DaemonInfo.detect()
        self.timeout = timeout

    def is_alive(self) -> bool:
        """
        检测 daemon 是否在线

        返回:
        - bool: 检测 daemon 是否在线
        """
        try:
            resp = self._request("GET", "/api/health")
            return resp.get("running", False)
        except Exception:
            return False

    def reload_config(self) -> dict[str, Any]:
        """
        重新加载配置

        返回:
        - dict[str, Any]: 重新加载配置
        """
        return self._request("POST", "/api/config/reload")

    def shutdown(self) -> dict[str, Any]:
        """
        关闭

        返回:
        - dict[str, Any]: 关闭
        """
        return self._request("POST", "/api/shutdown")

    def list_session_classes(self) -> dict[str, Any]:
        """
        列出会话类列表

        返回:
        - dict[str, Any]: 列出会话类列表
        """
        return self._request("GET", "/api/config/session-classes")

    def register_session_class(
        self,
        name: str,
        class_path: str,
        description: str = "",
        context_key: str = "",
        model_key: str = "",
    ) -> dict[str, Any]:
        """
        注册会话类

        参数:
        - name: 名称
        - class_path: 类path
        - description: 说明文本
        - context_key: 上下文密钥
        - model_key: 模型key

        返回:
        - dict[str, Any]: 注册会话类
        """
        return self._request(
            "POST",
            "/api/config/session-classes",
            body={
                "name": name,
                "class_path": class_path,
                "description": description,
                "context_key": context_key,
                "model_key": model_key,
            },
        )

    def enable_session_class(self, name: str) -> dict[str, Any]:
        """
        启用会话类

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 启用会话类
        """
        return self._request("POST", f"/api/config/session-classes/{self._quote(name)}/enable")

    def disable_session_class(self, name: str) -> dict[str, Any]:
        """
        停用会话类

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 停用会话类
        """
        return self._request("POST", f"/api/config/session-classes/{self._quote(name)}/disable")

    def get_session_class(self, name: str) -> dict[str, Any]:
        """
        获取会话类

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 获取会话类
        """
        return self._request("GET", f"/api/config/session-classes/{self._quote(name)}")

    def set_session_class_params(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        设置会话类参数

        参数:
        - name: 名称
        - params: 参数集合

        返回:
        - dict[str, Any]: 设置会话类参数
        """
        return self._request("PUT", f"/api/config/session-classes/{self._quote(name)}", body={"params": params})

    def unregister_session_class(self, name: str) -> dict[str, Any]:
        """
        注销会话类

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 注销会话类
        """
        return self._request("DELETE", f"/api/config/session-classes/{self._quote(name)}")

    def list_models(self, typ: str = "llm") -> dict[str, Any]:
        """
        列出模型列表

        参数:
        - typ: 资源类型

        返回:
        - dict[str, Any]: 列出模型列表
        """
        return self._request("GET", f"/api/config/models?type={typ}")

    def set_model(self, typ: str, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        设置模型

        参数:
        - typ: 资源类型
        - name: 名称
        - params: 参数集合

        返回:
        - dict[str, Any]: 设置模型
        """
        return self._request("POST", f"/api/config/models/{self._quote(typ)}/{self._quote(name)}", body=params)

    def update_model(self, typ: str, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        更新模型

        参数:
        - typ: 资源类型
        - name: 名称
        - params: 参数集合

        返回:
        - dict[str, Any]: 更新模型
        """
        return self._request("PATCH", f"/api/config/models/{self._quote(typ)}/{self._quote(name)}", body=params)

    def remove_model(self, typ: str, name: str) -> dict[str, Any]:
        """
        移除模型

        参数:
        - typ: 资源类型
        - name: 名称

        返回:
        - dict[str, Any]: 移除模型
        """
        return self._request("DELETE", f"/api/config/models/{self._quote(typ)}/{self._quote(name)}")

    def health(self) -> dict[str, Any]:
        """
        检查服务健康状态

        返回:
        - dict[str, Any]: 检查服务健康状态
        """
        return self._request("GET", "/api/health")

    @staticmethod
    def _quote(value: str) -> str:
        """
        URL path segment 转义

        参数:
        - value: 输入值

        返回:
        - str: URL path segment 转义
        """
        return urllib.parse.quote(value, safe="")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        发送 HTTP 请求并解析 JSON 响应

        参数:
        - method: HTTP 方法
        - path: 路径
        - body: 请求体

        返回:
        - dict[str, Any]: 发送 HTTP 请求并解析 JSON 响应
        """
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
