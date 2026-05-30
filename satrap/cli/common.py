from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

from satrap.cli.client import DaemonClient, DaemonInfo
from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config_loader import ConfigLoader


def load_cli_config(args: Namespace) -> BackendConfig:
    """加载 CLI 配置并应用 API 覆盖"""
    config = ConfigLoader.autodetect()
    config_path = getattr(args, "config", None)
    if config_path:
        path = Path(config_path)
        config = ConfigLoader.from_yaml(path) if path.suffix.lower() in (".yaml", ".yml") else ConfigLoader.from_json(path)
    config = ConfigLoader.merge_env(config)
    if getattr(args, "api_host", None):
        config.api_host = args.api_host
    if getattr(args, "api_port", None):
        config.api_port = int(args.api_port)
    return config


def daemon_client_from_args(args: Namespace, timeout: float = 2) -> DaemonClient:
    """按 CLI 参数创建 daemon client"""
    return DaemonClient(daemon=DaemonInfo.from_config(load_cli_config(args)), timeout=timeout)


def offline_requested(args: Namespace) -> bool:
    """是否显式请求离线模式"""
    return bool(getattr(args, "offline", False))


def force_offline(args: Namespace) -> bool:
    """是否允许在线时强制离线写入"""
    return bool(getattr(args, "force_offline", False))


def ensure_offline_allowed(args: Namespace, action: str = "写入本地配置") -> None:
    """后端在线时阻止普通离线写入"""
    client = daemon_client_from_args(args)
    if client.is_alive() and not force_offline(args):
        print(f"错误: 后端正在运行, 为避免 CLI 与后端抢写配置, 已拒绝离线{action}.")
        print("请改用默认在线模式, 或先执行 `satrap stop`; 确认风险后可加 --force-offline.")
        sys.exit(1)
    if client.is_alive() and force_offline(args):
        print("警告: 正在后端运行时强制离线写入, 运行态可能不会立即同步.")


def parse_kv_pairs(groups: list[list[str]] | None) -> dict[str, Any]:
    """解析 argparse 中的 key=value 参数组"""
    updates: dict[str, Any] = {}
    for group in groups or []:
        for item in group:
            if "=" not in item:
                raise ValueError(f"无效格式: {item}, 请使用 key=value")
            key, value = item.split("=", 1)
            updates[key.strip()] = coerce_value(value.strip())
    return updates


def coerce_value(value: str) -> Any:
    """将 CLI 字符串尽量转换为 JSON 标量"""
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


def print_json(data: Any) -> None:
    """输出 JSON"""
    print(json.dumps(data, ensure_ascii=False, indent=2))
