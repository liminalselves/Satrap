from __future__ import annotations

import sys

from satrap.admin_utils.config_editor import (
    create_default_config,
    find_config_path,
    load_config_document,
    load_raw_config,
    save_config_document,
)
from satrap.cli.common import daemon_client_from_args, parse_kv_pairs, print_json


def _warn_if_backend_running(args):
    """配置文件写入不会热替换运行态, 后端在线时提示重启"""
    client = daemon_client_from_args(args)
    if client.is_alive():
        print("提示: 后端正在运行, 配置文件变更需 reload/restart 后生效.")


def _set_nested(data: dict, dotted_key: str, value):
    """按 dotted key 设置配置值"""
    parts = dotted_key.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
        if not isinstance(cur, dict):
            raise ValueError(f"{dotted_key} 的父级不是对象")
    cur[parts[-1]] = value


def cmd_config_init(args):
    """创建默认配置"""
    _warn_if_backend_running(args)
    config = create_default_config()
    print(f"配置已就绪: {find_config_path()}")
    print(f"API: {config.api_host}:{config.api_port}")


def cmd_config_path(args):
    print(find_config_path())


def cmd_config_show(args):
    print_json(load_config_document(find_config_path()))


def cmd_config_raw(args):
    print(load_raw_config(find_config_path()), end="")


def cmd_config_set(args):
    _warn_if_backend_running(args)
    path = find_config_path()
    data = load_config_document(path)
    try:
        updates = parse_kv_pairs(args.set)
        for key, value in updates.items():
            _set_nested(data, key, value)
        save_config_document(path, data)
        print(f"配置已保存: {path}")
    except Exception as e:
        print(f"配置失败: {e}")
        sys.exit(1)


def dispatch(args):
    if args.action == "init":
        cmd_config_init(args)
    elif args.action == "path":
        cmd_config_path(args)
    elif args.action == "show":
        cmd_config_show(args)
    elif args.action == "raw":
        cmd_config_raw(args)
    elif args.action == "set":
        cmd_config_set(args)
    else:
        print(f"未知操作: {args.action}")
        sys.exit(1)
