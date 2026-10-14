"""复制实际配置规模到临时目录, 仅经 HTTP GET 测量读取, 不接触运行平台"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import threading
import time
from typing import Any
from unittest.mock import patch
from urllib.request import Request, urlopen
from uuid import uuid4

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


async def probe(root: Path, source_root: Path, report_path: Path, source_env: dict[str, str]) -> None:
    """
    以当前实现和实际参数副本复测读取, 拒绝所有源目录写入

    参数:
    - root: 已验证的临时工作目录
    - source_root: 原配置所在的项目根目录
    - report_path: 只含统计值的报告文件
    - source_env: 隔离前的配置环境覆盖
    """
    from satrap.core.backend import control_server as control
    from satrap.core.backend.BackendManager import BackendConfig, BackendManager
    from satrap.core.backend.http_api import BackendHTTPServer
    from satrap.core.config.document import find_config_path, load_config_document
    from satrap.core.framework.BackGroundManager import ModelConfigManager
    from satrap.edictum.config import EdictumConfigManager
    from satrap.edictum.plugin_catalog import PluginCatalog

    source = find_config_path(source_root)
    document = load_config_document(source)
    if not document:
        raise RuntimeError("缺少实际源配置, 不使用默认配置代替")
    tracked = {source.resolve()}
    for key, filename, env_key in (
        ("model_config_path", "model_config.json", "SATRAP_MODEL_CONFIG_PATH"),
        ("session_class_config_path", "session_class_config.json", "SATRAP_SESSION_CONFIG_PATH"),
        ("edictum_config_path", "edictum_session_config.json", "SATRAP_EDICTUM_CONFIG_PATH"),
    ):
        raw = document.get(key) or source_env.get(env_key)
        original = Path(str(raw)) if raw else Path(source_env.get("SATRAP_CONFIG_ROOT", str(source_root / ".satrap" / "config"))) / filename
        if not original.is_absolute():
            original = source_root / original
        if not original.is_file():
            raise RuntimeError(f"缺少实际源配置: {key}")
        target = root / "config" / filename
        shutil.copy2(original, target)
        document[key] = str(target)
        tracked.add(original.resolve())
    document["data_root"] = str(root / "data")
    main = root / "main.json"
    main.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    source_config = Path(source_env.get("SATRAP_CONFIG_ROOT", str(source_root / ".satrap" / "config")))
    tracked.update(path.resolve() for path in source_config.rglob("*.json") if path.is_file())
    if (report_path.suffix != ".json" or report_path in tracked
            or report_path.is_relative_to(source_config.resolve()) or report_path.is_relative_to(source_root / ".satrap")):
        raise RuntimeError("报告不能覆盖源配置或写入原运行目录")
    before = {path: (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns) for path in tracked}

    def guard(event, args):
        """
        拒绝临时目录和报告以外的写入, 不拦截原元数据读取

        参数:
        - event: Python 审计事件
        - args: 原操作参数
        """
        paths = []
        if event == "open" and not isinstance(args[0], int):
            if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                paths = [args[0]]
        elif event in {"os.remove", "os.rmdir", "os.rename", "os.mkdir", "sqlite3.connect"}:
            paths = list(args[:2]) if event == "os.rename" else [args[0]]
        for value in paths:
            path = Path(value).resolve()
            if event == "os.mkdir" and path.is_dir():
                continue
            if not path.is_relative_to(root) and path != report_path:
                raise RuntimeError("只读复测拒绝源目录写入")

    sys.addaudithook(guard)
    parses: dict[str, int] = {}
    normalized = []
    lock = threading.Lock()
    original_load, original_normalize = PluginCatalog._load_entry, EdictumConfigManager._normalize_entry
    slow = False
    original_read = control._read_plugin_global_config

    def load(path):
        """
        记录原目录解析, 保留原返回

        参数:
        - path: 实际插件目录

        返回:
        - 原目录条目
        """
        with lock:
            key = str(path)
            parses[key] = parses.get(key, 0) + 1
        return original_load(path)

    def normalize(manager, entry, catalog=None):
        """
        记录实际命名配置归一化

        参数:
        - manager: 原配置管理器
        - entry: 原命名条目
        - catalog: 原固定目录

        返回:
        - 原归一化结果
        """
        with lock:
            normalized.append(1)
        return original_normalize(manager, entry, catalog)

    def read(*args, **kwargs):
        """
        在原读取前增加可控慢存储

        参数:
        - args: 原参数
        - kwargs: 原关键字参数

        返回:
        - 原读取结果
        """
        if slow:
            time.sleep(.2)
        return original_read(*args, **kwargs)

    def deny_save(*args, **kwargs):
        """
        拒绝读取途中触发的保存, 不用写入来修复测试配置

        参数:
        - args: 原保存参数
        - kwargs: 原保存关键字参数
        """
        raise RuntimeError("只读复测禁止配置保存")

    backend = BackendManager(BackendConfig.from_dict(document))
    backend.config.source_path = str(main)
    backend._running = True
    backend_http = BackendHTTPServer(backend, port=0, static_dir=root)
    await backend_http.start()
    if backend_http._server is None:
        raise RuntimeError("隔离健康接口未启动")
    backend_port = backend_http._server.sockets[0].getsockname()[1]
    with ExitStack() as patches:
        patches.enter_context(patch.object(control, "CONFIG_PATH", main))
        patches.enter_context(patch.object(control, "BACKEND_PID_FILE", root / "runtime" / "absent.pid"))
        patches.enter_context(patch.object(control, "_configured_backend_address", lambda: ("127.0.0.1", backend_port)))
        patches.enter_context(patch.object(PluginCatalog, "_load_entry", staticmethod(load)))
        patches.enter_context(patch.object(EdictumConfigManager, "_normalize_entry", normalize))
        patches.enter_context(patch.object(EdictumConfigManager, "_save_locked", deny_save))
        patches.enter_context(patch.object(ModelConfigManager, "_save_locked", deny_save))
        patches.enter_context(patch.object(control, "_read_plugin_global_config", read))
        listener = await asyncio.start_server(control._handle_request, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=12) as clients:
            def request(path):
                """
                只发送 GET, 不输出配置或凭据

                参数:
                - path: 预先指定的读取路径

                返回:
                - 实际 JSON 与 HTTP 耗时
                """
                began = time.perf_counter()
                wire = Request(f"http://127.0.0.1:{port}{path}", headers={"Authorization": "Bearer " + os.environ["SATRAP_API_TOKEN"]})
                with urlopen(wire, timeout=10) as response:
                    result = json.load(response)
                return result, (time.perf_counter() - began) * 1000

            try:
                report: dict[str, Any] = {"reads": {}}
                for path in ("/config/plugins", "/config/plugins/friend_manager/config", "/config/edictum/sessions", "/config/administrator-groups", "/config/administrator-groups/snapshot"):
                    samples = []
                    for _ in range(6):
                        _payload, elapsed = await loop.run_in_executor(clients, request, path)
                        samples.append(elapsed)
                    report["reads"][path] = {"first_ms": round(samples[0], 3), "warm_median_ms": round(statistics.median(samples[1:]), 3)}
                initial = (dict(parses), len(normalized))
                slow = True
                pending = [loop.run_in_executor(clients, request, "/config/plugins/friend_manager/config") for _ in range(6)]
                status_samples = []
                for _ in range(30):
                    _payload, elapsed = await loop.run_in_executor(clients, request, "/status")
                    status_samples.append(elapsed)
                    await asyncio.sleep(.01)
                await asyncio.gather(*pending)
                slow = False
                assert initial == (parses, len(normalized))
                assert all(count == 1 for count in parses.values())
                service = await asyncio.to_thread(control._edictum_config_service)
                configs = service.list_configs()
                report["scale"] = {"plugins": len(parses), "named_configs": len(configs), "plugin_references": sum(len(item.get("plugins", [])) for item in configs.values())}
                report["parsing"] = {"metadata": sum(parses.values()), "named_normalizations": len(normalized), "concurrent_additional_metadata": 0, "concurrent_additional_normalizations": 0}
                p95 = statistics.quantiles(status_samples, n=100)[94]
                report["status_during_slow_reads"] = {"samples": 30, "p95_ms": round(p95, 3), "max_ms": round(max(status_samples), 3)}
                assert p95 < 100
                assert before == {path: (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns) for path in tracked}
                report["source_unchanged"] = True
                report["runtime_status"] = "isolated_backend_without_adapters"
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False))
            finally:
                listener.close()
                await listener.wait_closed()
                await backend_http.stop()


def main() -> None:
    """准备实际配置副本和隔离环境, 报告只保留统计值"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    source_env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="satrap-read-scale-") as directory:
        root = Path(directory).resolve()
        if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise RuntimeError("临时目录越出预期根目录")
        source_config = Path(source_env.get("SATRAP_CONFIG_ROOT", str(args.source_root / ".satrap" / "config")))
        shutil.copytree(source_config, root / "config")
        for name in ("DATA", "RUNTIME", "CREDENTIALS", "LOG"):
            os.environ[f"SATRAP_{name}_ROOT"] = str(root / name.lower())
        os.environ["SATRAP_CONFIG_ROOT"] = str(root / "config")
        os.environ["SATRAP_API_TOKEN"] = "isolated-read-probe-" + uuid4().hex
        for key in ("SATRAP_MODEL_CONFIG_PATH", "SATRAP_SESSION_CONFIG_PATH", "SATRAP_EDICTUM_CONFIG_PATH"):
            os.environ.pop(key, None)
        logging.disable(logging.CRITICAL)
        try:
            asyncio.run(probe(root, args.source_root.resolve(), args.report.resolve(), source_env))
        finally:
            from satrap.core.log import logger
            logger.close()
            logging.shutdown()


if __name__ == "__main__":
    main()
