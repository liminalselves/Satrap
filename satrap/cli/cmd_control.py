from __future__ import annotations

import asyncio
import sys
import time

from satrap.cli.common import daemon_client_from_args, load_cli_config
from satrap.cli.cmd_run import cmd_run


def cmd_status(args):
    """显示后端状态"""
    client = daemon_client_from_args(args, timeout=2)
    health = client.health()
    print(f"API: {client.daemon.base_url}")
    if health.get("error"):
        print(f"状态: 未运行 ({health['error']})")
        return
    print(f"状态: {'运行中' if health.get('running') else '未运行'}")
    print(f"组件: model={health.get('model_config')} session_class={health.get('session_class_config')} pipeline={health.get('pipeline')}")
    adapters = health.get("adapters", {})
    if adapters:
        print("平台实例:")
        for aid, info in adapters.items():
            print(f"  {aid}: {info.get('config_type', '-')}/{info.get('status', '-')}, started={info.get('started')}")
    else:
        print("平台实例: (空)")


def cmd_stop(args):
    """停止后端"""
    client = daemon_client_from_args(args, timeout=2)
    if not client.is_alive():
        print(f"后端未运行: {client.daemon.base_url}")
        return
    result = client.shutdown()
    if "error" in result:
        print(f"停止失败: {result['error']}")
        sys.exit(1)
    deadline = time.time() + 8
    while time.time() < deadline:
        if not client.is_alive():
            print("后端已停止")
            return
        time.sleep(0.5)
    print("已发送停止请求, 但后端仍在响应")
    sys.exit(1)


def cmd_restart(args):
    """重启后端"""
    client = daemon_client_from_args(args, timeout=2)
    if client.is_alive():
        result = client.shutdown()
        if "error" in result:
            print(f"停止失败: {result['error']}")
            sys.exit(1)
        deadline = time.time() + 8
        while time.time() < deadline and client.is_alive():
            time.sleep(0.5)
        if client.is_alive():
            print("后端未在超时时间内停止")
            sys.exit(1)
    load_cli_config(args)
    asyncio.run(cmd_run(args))


def dispatch(args):
    if args.command == "status":
        cmd_status(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "restart":
        cmd_restart(args)
