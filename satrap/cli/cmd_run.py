from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from satrap.cli.backend_lock import BackendInstanceLock
from satrap.cli.client import DaemonClient, DaemonInfo
from satrap.core.backend.BackendManager import BackendManager
from satrap.core.config_loader import ConfigLoader
from satrap.core.log import logger


def load_run_config(args):
    """加载 run 命令配置并应用命令行覆盖"""
    config = ConfigLoader.autodetect()
    if args.config:
        p = Path(args.config)
        config = ConfigLoader.from_yaml(p) if p.suffix in (".yaml", ".yml") else ConfigLoader.from_json(p)
    config = ConfigLoader.merge_env(config)
    if getattr(args, "api_host", None):
        config.api_host = args.api_host
    if getattr(args, "api_port", None):
        config.api_port = int(args.api_port)
    return config


async def cmd_run(args):
    """启动后端服务"""
    config = load_run_config(args)

    daemon = DaemonInfo.from_config(config)
    client = DaemonClient(daemon=daemon, timeout=1)
    if client.is_alive():
        logger.warning(f"[CLI] 后端已在运行: {daemon.base_url}")
        print(f"后端已在运行: {daemon.base_url}")
        return

    lock = BackendInstanceLock()
    if not lock.acquire(config.api_host, config.api_port):
        logger.warning("[CLI] 后端锁已被占用, 取消启动")
        print("后端已在运行或正在启动, 取消启动")
        sys.exit(1)

    try:
        stop_event = asyncio.Event()
        backend = BackendManager(config)
        backend.set_shutdown_event(stop_event)
        await backend.start()
        logger.info("[CLI] Satrap 后端已启动, 按 Ctrl+C 停止")

        loop = asyncio.get_event_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(getattr(signal, sig_name), stop_event.set)
            except (NotImplementedError, AttributeError):
                pass

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await backend.stop()
            logger.info("[CLI] Satrap 后端已停止")
    finally:
        lock.release()
