"""CLI 后端状态, 启动, 停止与重启命令"""
from __future__ import annotations
import argparse
import asyncio
from pathlib import Path
import json
import secrets
import subprocess
import time
import sys
import os

from satrap.cli.cmd_run import cmd_run, load_run_config
from satrap.cli.common import control_client_from_args, daemon_client_from_args
from satrap.cli.output import CliError, dispatch_action, info, ok
from satrap.core.utils.paths import get_data_dir


def cmd_status(args: argparse.Namespace):
    """
    显示后端状态 (查询命令, 后端未运行也正常退出)

    参数:
    - args: 额外位置参数
    """
    client = daemon_client_from_args(args, timeout=2)
    print(f"API: {client.daemon.base_url}")
    try:
        health = client.health()
    except Exception as e:
        print(f"状态: 未运行 ({e})")
        return
    print(f"状态: {'运行中' if health.get('running') else '未运行'}")
    print(f"组件: model={health.get('model_config')} session_class={health.get('session_class_config')} pipeline={health.get('pipeline')}")
    adapters = health.get("adapters", {})
    if adapters:
        print("平台实例:")
        for aid, adapter_info in adapters.items():
            print(f"  {aid}: {adapter_info.get('config_type', '-')}/{adapter_info.get('status', '-')}, started={adapter_info.get('started')}")
    else:
        print("平台实例: (空)")


def cmd_start(args: argparse.Namespace):
    """
    后台启动后端: 优先委托控制服务, 否则由 CLI 直接拉起并写入运行时身份记录

    参数:
    - args: 额外位置参数
    """
    client = daemon_client_from_args(args, timeout=2)
    if client.is_alive():
        ok(f"后端已在运行: {client.daemon.base_url}")
        return

    control = control_client_from_args(args, timeout=5)
    if control.is_alive():
        result = control.start_backend()
        ok(str(result.get("message") or "后端启动请求已发送 (控制服务)"))
        return

    _spawn_backend(args)


def _spawn_backend(args: argparse.Namespace):
    """
    由 CLI 直接后台拉起后端进程, 写入与控制服务兼容的运行时身份记录

    参数:
    - args: 额外位置参数
    """
    config = load_run_config(args)
    runtime_id = secrets.token_urlsafe(24)
    cmd = [
        sys.executable,
        "-m",
        "satrap.main",
        "--api-host", str(config.api_host),
        "--api-port", str(config.api_port),
        "run",
    ]
    startupinfo = None
    creationflags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(Path.cwd()),
            env={**os.environ, "SATRAP_BACKEND_RUNTIME_ID": runtime_id},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except OSError as e:
        raise CliError(f"后端启动失败: {e}") from e

    # 与控制服务 backend.pid 同构, 便于控制服务/面板接管该进程
    pid_file = get_data_dir() / "backend.pid"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(
            json.dumps({
                "pid": process.pid,
                "runtime_id": runtime_id,
                "host": config.api_host,
                "port": int(config.api_port),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass

    client = daemon_client_from_args(args, timeout=2)
    deadline = time.time() + 15
    while time.time() < deadline:
        if client.is_alive():
            ok(f"后端已启动: {client.daemon.base_url} (pid={process.pid})")
            return
        if process.poll() is not None:
            raise CliError("后端进程已退出, 启动失败", hint="可执行 `satrap run` 前台启动查看日志")
        time.sleep(0.5)
    ok(f"后端启动中, 请稍后使用 `satrap status` 确认 (pid={process.pid})")


def cmd_stop(args: argparse.Namespace):
    """
    停止后端

    参数:
    - args: 额外位置参数
    """
    client = daemon_client_from_args(args, timeout=2)
    if not client.is_alive():
        raise CliError(
            f"后端未运行: {client.daemon.base_url}",
            hint="可用 `satrap status` 查看状态, 或 `satrap start` 后台启动",
        )
    client.shutdown()
    deadline = time.time() + 8
    while time.time() < deadline:
        if not client.is_alive():
            ok("后端已停止")
            return
        time.sleep(0.5)
    raise CliError("已发送停止请求, 但后端仍在响应")


def cmd_restart(args: argparse.Namespace):
    """
    重启后端 (停止后前台运行, 需后台重启请用 stop + start)

    参数:
    - args: 额外位置参数
    """
    client = daemon_client_from_args(args, timeout=2)
    if client.is_alive():
        client.shutdown()
        deadline = time.time() + 8
        while time.time() < deadline and client.is_alive():
            time.sleep(0.5)
        if client.is_alive():
            raise CliError("后端未在超时时间内停止")
    asyncio.run(cmd_run(args))


def dispatch(args: argparse.Namespace):
    """
    分派命令

    参数:
    - args: 命令参数
    """
    dispatch_action({
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
    }, args, key="command")
