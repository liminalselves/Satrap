from __future__ import annotations

import asyncio
import signal

from satrap.core.backend.BackendManager import BackendManager
from satrap.core.config_loader import ConfigLoader
from satrap.core.log import logger


async def cmd_run(args):
    """启动后端服务"""
    config = ConfigLoader.autodetect()
    if args.config:
        from pathlib import Path
        p = Path(args.config)
        config = ConfigLoader.from_yaml(p) if p.suffix in (".yaml", ".yml") else ConfigLoader.from_json(p)
    config = ConfigLoader.merge_env(config)

    backend = BackendManager(config)
    await backend.start()
    logger.info("[CLI] Satrap 后端已启动, 按 Ctrl+C 停止")

    stop_event = asyncio.Event()
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
