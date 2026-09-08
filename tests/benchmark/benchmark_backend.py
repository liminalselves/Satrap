"""后端审计性能基线; 固定离线负载, 分开测量耗时, Python 分配峰值和查询数量"""
from __future__ import annotations

import importlib.metadata
from unittest.mock import AsyncMock, Mock, patch
import tracemalloc
import statistics
import subprocess
import threading
import argparse
from datetime import datetime, timezone
import platform
import tempfile
import asyncio
import hashlib
import logging
from pathlib import Path
import socket
from types import SimpleNamespace
from typing import Any, cast
import inspect
import json
import time
import sys
import gc
import os

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

with patch("logging.FileHandler", lambda *args, **kwargs: logging.NullHandler()):
    from satrap.core.framework.SessionManager import SessionManager
    from satrap.core.log import logger
    from satrap.core.type import UserCall
    from satrap.core.utils import outbound
    from satrap.display.recorder import DisplayRecorder
    from satrap.expend.plugins.satrap_coding.tools import AsyncReadFileTool, ReadFileTool

SOURCE_FILES = [
    "satrap/core/framework/SessionManager.py",
    *[path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "satrap/core/utils/outbound").glob("*.py"))],
    *[path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "satrap/expend/plugins/satrap_coding/tools").glob("*.py"))],
    "satrap/display/recorder.py",
    "satrap/display/service.py",
    "satrap/core/database/__init__.py",
    "satrap/core/storage/maintenance.py",
    "satrap/core/utils/minihttp.py",
    "satrap/expend/plugins/satrap_coding/core/command_gate.py",
]


def summary(values):
    """保留原始样本和中位数, 最小值及最大值"""
    return {
        "samples": [round(v, 4) for v in values],
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


async def measure(call, repeats):
    """预热一次, 单独测量耗时和 tracemalloc 峰值, 避免追踪影响计时"""
    await call()
    times = []
    for _ in range(repeats):
        gc.collect()
        start = time.perf_counter()
        await call()
        times.append((time.perf_counter() - start) * 1000)
    gc.collect()
    tracemalloc.start()
    try:
        result = await call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {"elapsed_ms": summary(times), "python_peak_bytes": peak}, result


async def file_baselines(root, repeats):
    """使用固定长度 ASCII 行, 测量首部及尾部分页的同步和异步路径"""
    cases = []
    session = SimpleNamespace(coding_workspace_root=root)
    for size_mib in (1, 8, 32):
        path = root / f"file-{size_mib}.txt"
        line = b"audit-line " + b"x" * 68 + b"\n"
        line_count = size_mib * 1024 * 1024 // len(line)
        with path.open("wb") as stream:
            for _ in range(line_count // 1024):
                stream.write(line * 1024)
            stream.write(line * (line_count % 1024))
        for asynchronous in (False, True):
            tool = AsyncReadFileTool() if asynchronous else ReadFileTool()
            tool._bind(cast(Any, session))
            for offset in (0, line_count - 20):
                async def call():
                    value = tool.execute(str(path), offset=offset, limit=20)
                    result = await value if inspect.isawaitable(value) else value
                    if result.count("audit-line") != 20:
                        raise RuntimeError(f"分页结果校验失败: {result[:200]}")
                    return result

                metrics, output = await measure(call, repeats)
                cases.append({
                    "size_bytes": path.stat().st_size, "line_count": line_count,
                    "mode": "async" if asynchronous else "sync", "offset": offset, "limit": 20,
                    "output_bytes": len(output.encode("utf-8")), **metrics,
                })
    return cases


async def history_baselines(root, repeats):
    """批量构造真实 SQLite 表, 测量生产历史查询且统计独立查询语句数"""
    cases = []
    for turns in (100, 1000):
        for variants in (1, 3):
            recorder = DisplayRecorder(str(root / f"history-{turns}-{variants}.db"), "audit")
            conn = recorder._get_conn()
            try:
                conn.executemany(
                    "INSERT INTO display_turns (id, conversation_id, turn_index, user_input, answer, active_variant, created_at) VALUES (?, 'audit', ?, 'question', ?, 0, 1)",
                    [(i + 1, i, "a" * 512) for i in range(turns)],
                )
                conn.executemany(
                    "INSERT INTO display_turn_variants (turn_id, variant_index, answer, context_messages, created_at) VALUES (?, ?, ?, '[]', 1)",
                    [(i + 1, v, "a" * 512) for i in range(turns) for v in range(variants)],
                )
                conn.executemany(
                    "INSERT INTO display_tool_calls (turn_id, variant_index, seq, name, arguments, success, call_id, created_at) VALUES (?, ?, ?, 'audit-tool', '{}', 1, 'audit-call', 1)",
                    [(i + 1, v, seq) for i in range(turns) for v in range(variants) for seq in range(2)],
                )
                conn.commit()

                async def call():
                    result = recorder.list_turns()
                    if len(result) != turns or any(len(row["variants"]) != variants for row in result):
                        raise RuntimeError("历史返回轮次或版本数量错误")
                    return result

                metrics, result = await measure(call, repeats)
                statements = []
                conn.set_trace_callback(statements.append)
                await call()
                conn.set_trace_callback(None)
                cases.append({
                    "turns": turns, "variants_per_turn": variants, "tools_per_variant": 2,
                    "select_statements": sum(s.lstrip().upper().startswith("SELECT") for s in statements),
                    "response_json_bytes": len(json.dumps(result).encode("utf-8")), **metrics,
                })
            finally:
                recorder.close()
    return cases


async def loop_sample(call):
    """先启动心跳再调用被测协程, 测量事件循环调度延迟"""
    lags = []
    running = True

    async def heartbeat():
        while running:
            start = time.perf_counter()
            await asyncio.sleep(0.005)
            lags.append(max(0.0, time.perf_counter() - start - 0.005) * 1000)

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    start = time.perf_counter()
    try:
        await call()
        elapsed = (time.perf_counter() - start) * 1000
        await asyncio.sleep(0.02)   # 让被阻塞的心跳恢复并记录滞后
    finally:
        running = False
        await pulse
    return elapsed, max(lags, default=0.0)


async def event_loop_baselines(repeats):
    """使用 100 ms 的可控同步延迟, 不访问网络或真实模型"""
    delay = 0.1

    def run(message):
        time.sleep(delay)
        return "audit-answer"

    entry = SimpleNamespace(session=SimpleNamespace(run=run), async_operation_lock=asyncio.Lock(), sync_operation_lock=threading.RLock())
    manager = SessionManager.__new__(SessionManager)
    manager.pool = Mock(list_entries=lambda: {"audit": entry}, release=lambda value: None)
    manager._resolve_or_create_session_config = Mock(return_value=SimpleNamespace(session_id="audit"))
    manager._acquire_or_create_entry_async = AsyncMock(return_value=entry)
    manager._prepare_session_async = AsyncMock()
    manager._sync_runtime_to_store = Mock(return_value=None)
    manager.cleanup_idle_sessions_async = AsyncMock()

    async def session_call():
        result = await manager.handle_call_async(UserCall(session_id="audit", message="test"))
        if result != "audit-answer":
            raise RuntimeError(f"同步会话基线没有执行目标函数: {result!r}")

    def delayed_dns(*args, **kwargs):
        time.sleep(delay)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

    async def dns_call():
        response = outbound.OutboundHTTPResponse("https://audit.invalid/", 200, {}, b"ok")
        with patch.object(outbound.socket, "getaddrinfo", delayed_dns), patch.object(
            outbound, "_async_request_once", AsyncMock(return_value=response),
        ):
            result = await outbound.safe_async_get("https://audit.invalid/")
            if result.content != b"ok":
                raise RuntimeError("DNS 基线响应错误")

    async def control():
        await asyncio.sleep(delay)

    cases = []
    for name, call in [("async_sleep_control", control), ("sync_session", session_call), ("sync_dns", dns_call)]:
        await loop_sample(call)
        samples = [await loop_sample(call) for _ in range(repeats)]
        cases.append({
            "case": name, "injected_delay_ms": delay * 1000, "heartbeat_interval_ms": 5,
            "elapsed_ms": summary([row[0] for row in samples]),
            "max_loop_lag_ms": summary([row[1] for row in samples]),
        })
    return cases


def source_hashes():
    """记录审计实现指纹, 使未提交工作区也能精确追溯"""
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


async def main():
    """运行基线并以 UTF-8 保存结构化结果, 临时负载自动清理"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("repeats 至少为 3")
    if args.output.exists():
        parser.error("输出已存在, 请使用新文件名保留旧基线")
    logger.std_out = False
    logger.file_out = False
    before = source_hashes()
    with tempfile.TemporaryDirectory(prefix="satrap-backend-baseline-") as folder:
        root = Path(folder)
        print("Measuring event loop", flush=True)
        event_loop = await event_loop_baselines(args.repeats)
        print("Measuring paginated files", flush=True)
        files = await file_baselines(root, args.repeats)
        print("Measuring chat history", flush=True)
        history = await history_baselines(root, args.repeats)
        gc.collect()
    if before != source_hashes():
        raise RuntimeError("基线期间源代码发生变化, 本次结果不应保存")
    result = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version, "platform": platform.platform(), "processor": platform.processor(),
            "cpu_count": os.cpu_count(), "sqlite": __import__("sqlite3").sqlite_version,
            "dependencies": {name: importlib.metadata.version(name) for name in ("aiohttp", "numpy", "faiss-cpu", "pytest")},
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8").strip(),
            "source_sha256": before,
            "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "method": {"repeats": args.repeats, "warmups": 1, "memory": "one separate tracemalloc run; Python allocations only, not RSS", "cache": "warm OS cache; no cold-cache claim", "network": "none; DNS and HTTP mocked"},
        "event_loop": event_loop, "file_pagination": files, "history": history,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
