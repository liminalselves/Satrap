"""CLI 配置查看, 校验与修改命令"""
from __future__ import annotations
import argparse
from typing import Any, cast

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config.document import (
    create_default_config,
    find_config_path,
    load_config_document,
    load_raw_config,
    redact_config_document,
    save_config_document,
    validate_config_document,
)
from satrap.cli.common import daemon_client_from_args, parse_kv_pairs
from satrap.cli.output import CliError, dispatch_action, info, ok, print_json, render_data


def _warn_if_backend_running(args: argparse.Namespace):
    """
    配置文件写入不会热替换运行态, 后端在线时提示重启

    参数:
    - args: 额外位置参数
    """
    client = daemon_client_from_args(args)
    if client.is_alive():
        info("后端正在运行, 配置文件变更需 reload/restart 后生效")


def _set_nested(data: dict[str, Any], dotted_key: str, value: Any):
    """
    按 dotted key 设置配置值

    参数:
    - data: 输入数据
    - dotted_key: dotted密钥
    - value: 输入值
    """
    parts = dotted_key.split(".")
    cur: Any = data
    for part in parts[:-1]:
        child: Any = cur.get(part)
        if child is None:
            child = {}
            cur[part] = child
        elif not isinstance(child, dict):
            raise ValueError(f"{dotted_key} 的父级不是对象")
        else:
            cur = cast(dict[str, Any], child)
    cur[parts[-1]] = value


def cmd_config_init(args: argparse.Namespace):
    """
    创建默认配置

    参数:
    - args: 额外位置参数
    """
    _warn_if_backend_running(args)
    path = find_config_path()
    config = BackendConfig.from_dict(create_default_config(path))
    ok(f"配置已就绪: {path}")
    ok(f"API: {config.api_host}:{config.api_port}")


def cmd_config_path(args: argparse.Namespace):
    """
    处理 config_path 命令

    参数:
    - args: 命令参数
    """
    print(find_config_path())


def cmd_config_show(args: argparse.Namespace):
    """
    处理 config_show 命令 (密钥字段脱敏, 需要原文请用 config raw)

    参数:
    - args: 命令参数
    """
    print_json(redact_config_document(load_config_document(find_config_path())))


def cmd_config_raw(args: argparse.Namespace):
    """
    处理 config_raw 命令 (原始文本, 不脱敏)

    参数:
    - args: 命令参数
    """
    print(load_raw_config(find_config_path()), end="")


def cmd_config_validate(args: argparse.Namespace):
    """
    校验当前配置文件, 不落盘

    参数:
    - args: 命令参数
    """
    path = find_config_path()
    try:
        data = load_config_document(path)
    except Exception as e:
        raise CliError(f"配置解析失败: {e}") from e
    try:
        validate_config_document(data)
    except ValueError as e:
        raise CliError(f"配置校验未通过: {e}", hint=f"配置文件: {path}") from e
    render_data({"ok": True, "path": str(path)}, lambda: ok(f"配置校验通过: {path}"))


def cmd_config_set(args: argparse.Namespace):
    """
    处理 config_set 命令

    参数:
    - args: 命令参数
    """
    _warn_if_backend_running(args)
    path = find_config_path()
    data = load_config_document(path)
    try:
        updates = parse_kv_pairs(args.set)
        for key, value in updates.items():
            _set_nested(data, key, value)
        validate_config_document(data)
        save_config_document(path, data)
    except Exception as e:
        raise CliError(f"配置失败: {e}") from e
    ok(f"配置已保存: {path}")


def dispatch(args: argparse.Namespace):
    """
    分派命令

    参数:
    - args: 命令参数
    """
    dispatch_action({
        "init": cmd_config_init,
        "path": cmd_config_path,
        "show": cmd_config_show,
        "raw": cmd_config_raw,
        "set": cmd_config_set,
        "validate": cmd_config_validate,
    }, args)
