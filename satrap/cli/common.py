"""CLI 命令共享的参数, 连接与解析辅助函数"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any
import json
import os

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config.loader import ConfigLoader
from satrap.cli.client import DEFAULT_CHAT_PORT, DEFAULT_CONTROL_PORT, DaemonClient, DaemonInfo
from satrap.cli.output import CliError, print_json, warn
from satrap.core.type import safe_getattr, safe_getattr_bool

__all__ = [
    "load_cli_config",
    "daemon_client_from_args",
    "control_client_from_args",
    "chat_client_from_args",
    "offline_requested",
    "force_offline",
    "ensure_offline_allowed",
    "parse_kv_pairs",
    "coerce_value",
    "print_json",
]


def load_cli_config(args: Namespace) -> BackendConfig:
    """
    加载 CLI 配置并应用 API 覆盖

    参数:
    - args: 额外位置参数

    返回:
    - BackendConfig: 加载 CLI 配置并应用 API 覆盖
    """
    config = ConfigLoader.autodetect()
    config_path = safe_getattr(args, "config")
    if config_path:
        path = Path(str(config_path))
        config = ConfigLoader.from_yaml(path) if path.suffix.lower() in (".yaml", ".yml") else ConfigLoader.from_json(path)
    config = ConfigLoader.merge_env(config)
    if safe_getattr(args, "api_host"):
        config.api_host = args.api_host
    if safe_getattr(args, "api_port"):
        config.api_port = int(args.api_port)
    return config


def daemon_client_from_args(args: Namespace, timeout: float = 2) -> DaemonClient:
    """
    按 CLI 参数创建后端 client

    参数:
    - args: 额外位置参数
    - timeout: 超时时间

    返回:
    - DaemonClient: 按 CLI 参数创建后端 client
    """
    return DaemonClient(daemon=DaemonInfo.from_config(load_cli_config(args)), timeout=timeout)


def control_client_from_args(args: Namespace, timeout: float = 5) -> DaemonClient:
    """
    创建控制服务 client (默认 127.0.0.1:19871, 可用 SATRAP_CONTROL_PORT 覆盖)

    参数:
    - args: 额外位置参数
    - timeout: 超时时间

    返回:
    - DaemonClient: 控制服务 client
    """
    port = int(os.getenv("SATRAP_CONTROL_PORT", str(DEFAULT_CONTROL_PORT)))
    return DaemonClient(
        daemon=DaemonInfo(host="127.0.0.1", port=port),
        timeout=timeout,
        health_path="/status",
        service_label="控制服务",
    )


def chat_client_from_args(args: Namespace, timeout: float = 5) -> DaemonClient:
    """
    创建聊天服务 client (默认 127.0.0.1:19872, 可用 SATRAP_CHAT_PORT 覆盖)

    参数:
    - args: 额外位置参数
    - timeout: 超时时间

    返回:
    - DaemonClient: 聊天服务 client
    """
    port = int(os.getenv("SATRAP_CHAT_PORT", str(DEFAULT_CHAT_PORT)))
    return DaemonClient(
        daemon=DaemonInfo(host="127.0.0.1", port=port),
        timeout=timeout,
        health_path="/api/chat/health",
        service_label="Chat 服务",
    )


def offline_requested(args: Namespace) -> bool:
    """
    是否显式请求离线模式

    参数:
    - args: 额外位置参数

    返回:
    - bool: 是否显式请求离线模式
    """
    return safe_getattr_bool(args, "offline")


def force_offline(args: Namespace) -> bool:
    """
    是否允许在线时强制离线写入

    参数:
    - args: 额外位置参数

    返回:
    - bool: 是否允许在线时强制离线写入
    """
    return safe_getattr_bool(args, "force_offline")


def ensure_offline_allowed(args: Namespace, action: str = "写入本地配置") -> None:
    """
    后端在线时阻止普通离线写入 (抛 CliError, 由分发层统一渲染)

    参数:
    - args: 额外位置参数
    - action: 操作类型
    """
    client = daemon_client_from_args(args)
    if client.is_alive() and not force_offline(args):
        raise CliError(
            f"后端正在运行, 为避免 CLI 与后端抢写配置, 已拒绝离线{action}",
            hint="请改用默认在线模式, 或先执行 `satrap stop`; 确认风险后可加 --force-offline",
        )
    if client.is_alive() and force_offline(args):
        warn("正在后端运行时强制离线写入, 运行态可能不会立即同步")


def parse_kv_pairs(groups: list[list[str]] | None) -> dict[str, Any]:
    """
    解析 argparse 中的 key=value 参数组

    参数:
    - groups: groups 输入值

    返回:
    - dict[str, Any]: 解析 argparse 中的 key=value 参数组
    """
    updates: dict[str, Any] = {}
    for group in groups or []:
        for item in group:
            if "=" not in item:
                raise ValueError(f"无效格式: {item}, 请使用 key=value")
            key, value = item.split("=", 1)
            updates[key.strip()] = coerce_value(value.strip())
    return updates


def coerce_value(value: str) -> Any:
    """
    将 CLI 字符串尽量转换为 JSON 标量

    参数:
    - value: 输入值

    返回:
    - Any: 将 CLI 字符串尽量转换为 JSON 标量
    """
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "None"):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
