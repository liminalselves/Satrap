"""React 管理面板运行时服务地址配置"""
from __future__ import annotations
import os


def build_ui_config(
    *,
    backend_host: str = "127.0.0.1",
    backend_port: int = 19870,
    control_api: str | None = None,
    chat_host: str = "127.0.0.1",
    chat_port: int | None = None,
) -> dict[str, str]:
    """
    构建前端运行时使用的服务地址

    参数:
    - backend_host: 平台后端主机
    - backend_port: 平台后端端口
    - control_api: 控制服务完整地址
    - chat_host: 聊天后端主机
    - chat_port: 聊天后端端口

    返回:
    - dict[str, str]: 前端服务地址
    """
    return {
        "control_api": (control_api or f"http://127.0.0.1:{os.getenv('SATRAP_CONTROL_PORT', '19871')}").rstrip("/"),
        "backend_api": f"http://{_client_host(backend_host)}:{int(backend_port)}",
        "chat_api": f"http://{_client_host(chat_host)}:{int(chat_port if chat_port is not None else os.getenv('SATRAP_CHAT_PORT', '19872'))}",
    }


def _client_host(host: str) -> str:
    """
    将监听通配地址转换为浏览器可访问地址

    参数:
    - host: 服务监听主机

    返回:
    - str: 浏览器可访问的主机
    """
    normalized = str(host or "127.0.0.1").strip()
    if normalized in {"0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return normalized
