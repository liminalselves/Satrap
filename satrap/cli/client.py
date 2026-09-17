"""CLI 与 Satrap 各本地服务通信的 HTTP 客户端 (后端 / 控制服务 / 聊天服务)"""
from __future__ import annotations

import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
import json
import os

from satrap.core.server_auth import load_or_create_api_token

if TYPE_CHECKING:
    from satrap.core.backend.BackendManager import BackendConfig

DEFAULT_BACKEND_PORT = 19870
DEFAULT_CONTROL_PORT = 19871
DEFAULT_CHAT_PORT = 19872


class DaemonUnavailable(Exception):
    """目标服务未运行或不可达"""


class DaemonError(Exception):
    """目标服务返回了错误响应"""


@dataclass
class DaemonInfo:
    """后端进程连接信息"""

    host: str = "127.0.0.1"
    port: int = DEFAULT_BACKEND_PORT

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
            from satrap.core.config.loader import ConfigLoader   # 探测失败时仍允许客户端退回默认连接信息

            config = ConfigLoader.autodetect()
            return cls.from_config(config)
        except Exception:
            host = os.getenv("SATRAP_API_HOST", "127.0.0.1")
            port = int(os.getenv("SATRAP_API_PORT", str(DEFAULT_BACKEND_PORT)))
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
    """HTTP 客户端, 与后端 daemon 通信; 请求失败抛 DaemonUnavailable/DaemonError"""

    def __init__(
        self,
        daemon: DaemonInfo | None = None,
        timeout: float = 5,
        token: str | None = None,
        health_path: str = "/api/health",
        service_label: str = "后端",
    ):
        """
        初始化 DaemonClient

        参数:
        - daemon: daemon 输入值
        - timeout: 超时时间
        - token: API Bearer 令牌, 未提供时读取共享运行时令牌
        - health_path: 存活探测路径
        - service_label: 服务名称 (用于错误话术)
        """
        self.daemon = daemon or DaemonInfo.detect()
        self.timeout = timeout
        self.token = token or load_or_create_api_token()
        self.health_path = health_path
        self.service_label = service_label

    # ---------- 存活与生命周期 ----------

    def is_alive(self) -> bool:
        """
        检测服务是否在线 (探测场景, 不抛异常)

        返回:
        - bool: 检测服务是否在线
        """
        try:
            self._request("GET", self.health_path)
            return True
        except Exception:
            return False

    def require_alive(self) -> None:
        """
        要求服务在线, 否则抛 DaemonUnavailable
        """
        if not self.is_alive():
            raise DaemonUnavailable(f"{self.service_label}未运行: {self.daemon.base_url}")

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

    def start_backend(self) -> dict[str, Any]:
        """
        通过控制服务启动后端

        返回:
        - dict[str, Any]: 通过控制服务启动后端
        """
        return self._request("POST", "/start")

    def health(self) -> dict[str, Any]:
        """
        检查服务健康状态

        返回:
        - dict[str, Any]: 检查服务健康状态
        """
        return self._request("GET", self.health_path)

    # ---------- 会话类配置 ----------

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

    # ---------- 会话实例 ----------

    def list_session_instances(self) -> list[dict[str, Any]]:
        """
        列出全部平台的持久化会话实例

        返回:
        - list[dict[str, Any]]: 会话实例列表
        """
        data = self._request("GET", "/api/sessions")
        sessions = data.get("sessions", [])
        return list(sessions) if isinstance(sessions, list) else []

    def create_session_instance(
        self,
        session_type: str,
        *,
        provider: str = "",
        platform_id: str = "",
        session_id: str = "",
        llm_name: str = "",
        params: dict[str, Any] | None = None,
        activate: bool = False,
    ) -> dict[str, Any]:
        """
        创建会话实例

        参数:
        - session_type: 会话定义名称
        - provider: 会话 Provider
        - platform_id: 平台实例 ID
        - session_id: 可选自定义会话 ID
        - llm_name: 可选模型覆盖
        - params: 可选参数覆盖
        - activate: 是否立即激活

        返回:
        - dict[str, Any]: 创建结果
        """
        return self._request("POST", "/api/sessions", body={
            "session_type": session_type,
            "session_provider": provider,
            "platform_id": platform_id,
            "session_id": session_id,
            "llm_name": llm_name,
            "params": params or {},
            "activate": activate,
        })

    def delete_session_instance(self, session_id: str, platform_id: str = "") -> dict[str, Any]:
        """
        删除会话实例

        参数:
        - session_id: 会话 ID
        - platform_id: 平台实例 ID

        返回:
        - dict[str, Any]: 删除结果
        """
        query = f"?platform_id={self._quote(platform_id)}" if platform_id else ""
        return self._request("DELETE", f"/api/sessions/{self._quote(session_id)}{query}")

    def bulk_delete_session_instances(
        self,
        mode: str,
        session_refs: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """
        批量删除会话实例

        参数:
        - mode: empty / single / selected
        - session_refs: selected 模式下的目标引用

        返回:
        - dict[str, Any]: 删除结果
        """
        body: dict[str, Any] = {"mode": mode}
        if session_refs is not None:
            body["session_refs"] = session_refs
        return self._request("POST", "/api/sessions/bulk-delete", body=body)

    def restart_session_instance(self, session_id: str, platform_id: str) -> dict[str, Any]:
        """
        按冷配置重启会话实例

        参数:
        - session_id: 会话 ID
        - platform_id: 平台实例 ID

        返回:
        - dict[str, Any]: 重启结果
        """
        return self._request(
            "POST",
            f"/api/sessions/{self._quote(session_id)}/restart?platform_id={self._quote(platform_id)}",
        )

    # ---------- 模型配置 ----------

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

    # ---------- Edictum 命名配置 ----------

    def list_edictum_types(self) -> dict[str, Any]:
        """
        列出 Edictum 类型

        返回:
        - dict[str, Any]: 列出 Edictum 类型
        """
        return self._request("GET", "/api/config/edictum/types")

    def list_edictum_configs(self) -> dict[str, Any]:
        """
        列出 Edictum 命名配置

        返回:
        - dict[str, Any]: 列出 Edictum 命名配置
        """
        return self._request("GET", "/api/config/edictum/sessions")

    def get_edictum_config(self, name: str) -> dict[str, Any]:
        """
        获取 Edictum 命名配置

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 获取 Edictum 命名配置
        """
        return self._request("GET", f"/api/config/edictum/sessions/{self._quote(name)}")

    def create_edictum_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        创建 Edictum 命名配置

        参数:
        - payload: 配置内容

        返回:
        - dict[str, Any]: 创建结果
        """
        return self._request("POST", "/api/config/edictum/sessions", body=payload)

    def update_edictum_config(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        更新 Edictum 命名配置

        参数:
        - name: 名称
        - payload: 待更新字段

        返回:
        - dict[str, Any]: 更新结果
        """
        return self._request("PUT", f"/api/config/edictum/sessions/{self._quote(name)}", body=payload)

    def set_edictum_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """
        启停 Edictum 命名配置

        参数:
        - name: 名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 更新结果
        """
        action = "enable" if enabled else "disable"
        return self._request("POST", f"/api/config/edictum/sessions/{self._quote(name)}/{action}")

    def delete_edictum_config(self, name: str) -> dict[str, Any]:
        """
        删除 Edictum 命名配置

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 删除结果
        """
        return self._request("DELETE", f"/api/config/edictum/sessions/{self._quote(name)}")

    def preview_edictum_runtime(self, config_name: str = "", session_refs: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """
        预览 Edictum 运行时配置变更影响

        参数:
        - config_name: 命名配置名称
        - session_refs: 目标会话引用

        返回:
        - dict[str, Any]: 预览结果
        """
        body: dict[str, Any] = {"config_name": config_name}
        if session_refs is not None:
            body["session_refs"] = session_refs
        return self._request("POST", "/api/edictum/runtime/preview", body=body)

    def apply_edictum_runtime(self, config_name: str = "", session_refs: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """
        应用 Edictum 运行时配置变更

        参数:
        - config_name: 命名配置名称
        - session_refs: 目标会话引用

        返回:
        - dict[str, Any]: 应用结果
        """
        body: dict[str, Any] = {"config_name": config_name}
        if session_refs is not None:
            body["session_refs"] = session_refs
        return self._request("POST", "/api/edictum/runtime/apply", body=body)

    # ---------- Chat 插件 (聊天服务) ----------

    def list_chat_plugins(self) -> dict[str, Any]:
        """
        列出聊天插件

        返回:
        - dict[str, Any]: 列出聊天插件
        """
        return self._request("GET", "/api/chat/plugins")

    def set_chat_plugin_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """
        启停聊天插件

        参数:
        - name: 名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 设置结果
        """
        action = "enable" if enabled else "disable"
        return self._request("POST", f"/api/chat/plugins/{self._quote(name)}/{action}")

    def set_chat_plugin_capability(self, name: str, kind: str, cap: str, enabled: bool) -> dict[str, Any]:
        """
        设置插件能力独立启停

        参数:
        - name: 插件名称
        - kind: 能力类别
        - cap: 能力名称
        - enabled: 是否启用

        返回:
        - dict[str, Any]: 设置结果
        """
        return self._request(
            "POST",
            f"/api/chat/plugins/{self._quote(name)}/capability",
            body={"kind": kind, "cap": cap, "enabled": enabled},
        )

    def get_chat_plugin_config(self, name: str) -> dict[str, Any]:
        """
        获取插件配置 (schema + 当前值)

        参数:
        - name: 名称

        返回:
        - dict[str, Any]: 获取插件配置
        """
        return self._request("GET", f"/api/chat/plugins/{self._quote(name)}/config")

    def save_chat_plugin_config(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """
        保存插件全局配置

        参数:
        - name: 名称
        - config: 配置信息

        返回:
        - dict[str, Any]: 保存结果
        """
        return self._request("PUT", f"/api/chat/plugins/{self._quote(name)}/config", body={"config": config})

    # ---------- 底层 ----------

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
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else "{}"
            message = f"HTTP {e.code}: {e.reason}"
            try:
                payload = json.loads(err_body)
                if isinstance(payload, dict) and payload.get("error"):
                    message = str(payload["error"])
            except Exception:
                pass
            raise DaemonError(message) from e
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError) as e:
            raise DaemonUnavailable(f"{self.service_label}未响应: {e}") from e
