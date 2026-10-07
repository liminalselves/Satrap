"""
插件热路径基准的公共测量工具

提供同步与异步计时, tracemalloc 分配峰值, 进程 RSS 与句柄增量, 按名调用计数, SQLite 查询计数,
事件循环延迟采样和结果等价哈希; 只服务新基准脚本, 不改动既有基准
"""
from __future__ import annotations

import importlib.metadata
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
import tracemalloc
from contextlib import contextmanager
import statistics
import subprocess
import importlib
from datetime import datetime, timezone
import platform
import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Any, TypeVar, cast
import psutil
import json
import time
import sys
import gc
import os

ROOT = Path(__file__).resolve().parents[2]

_T = TypeVar("_T")


def summarize(samples: list[float]) -> dict[str, Any]:
    """
    汇总耗时样本

    参数:
    - samples: 毫秒样本, 至少一个

    返回:
    - 原始样本, 中位数, p95, 最小值和最大值
    """
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))]
    return {"samples": [round(v, 4) for v in samples], "median": round(statistics.median(samples), 4),
            "p95": round(p95, 4), "min": round(ordered[0], 4), "max": round(ordered[-1], 4)}


def _rss_bytes(process: Any) -> int:
    """
    读取进程常驻内存字节数

    参数:
    - process: psutil 进程对象, 第三方库无类型存根

    返回:
    - 常驻内存字节数
    """
    return int(process.memory_info().rss)


def _handles() -> int:
    """读取当前进程句柄数, Windows 使用 num_handles, 其他平台使用 num_fds"""
    process = cast(Any, psutil.Process())
    return int(process.num_handles()) if sys.platform == "win32" else int(process.num_fds())


def _memory(call: Callable[[], _T]) -> tuple[dict[str, Any], _T]:
    """
    单独执行一次并记录 Python 分配峰值, 进程 RSS 与句柄增量

    参数:
    - call: 无参调用

    返回:
    - 内存指标与本次调用结果
    """
    gc.collect()
    process = cast(Any, psutil.Process())
    rss, handles = _rss_bytes(process), _handles()
    tracemalloc.start(10)
    try:
        result = call()
        _, peak = tracemalloc.get_traced_memory()
        top = tracemalloc.take_snapshot().statistics("lineno")[:5]
    finally:
        tracemalloc.stop()
    gc.collect()
    return {"python_peak_bytes": peak, "rss_delta_bytes": _rss_bytes(process) - rss,
            "handle_delta": _handles() - handles,
            "top_allocations": [f"{item.traceback[0].filename.replace(str(ROOT), '')}:{item.traceback[0].lineno} {item.size}B" for item in top]}, result


def measure(call: Callable[[], _T], repeats: int) -> tuple[dict[str, Any], _T]:
    """
    同步计时: 预热一次, 每次采样前回收, 内存追踪单独运行避免影响计时

    参数:
    - call: 无参调用
    - repeats: 正式采样次数

    返回:
    - 耗时与内存指标, 以及内存轮的调用结果
    """
    call()
    times: list[float] = []
    for _ in range(repeats):
        gc.collect()
        start = time.perf_counter()
        call()
        times.append((time.perf_counter() - start) * 1000)
    memory, result = _memory(call)
    return {"elapsed_ms": summarize(times), **memory}, result


async def measure_async(call: Callable[[], Awaitable[_T]], repeats: int) -> tuple[dict[str, Any], _T]:
    """
    异步计时, 规则与 measure 一致; 内存轮在当前事件循环内执行

    参数:
    - call: 返回协程的无参调用
    - repeats: 正式采样次数

    返回:
    - 耗时与内存指标, 以及内存轮的调用结果
    """
    await call()
    times: list[float] = []
    for _ in range(repeats):
        gc.collect()
        start = time.perf_counter()
        await call()
        times.append((time.perf_counter() - start) * 1000)
    gc.collect()
    process = cast(Any, psutil.Process())
    rss, handles = _rss_bytes(process), _handles()
    tracemalloc.start(10)
    try:
        result = await call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    gc.collect()
    return {"elapsed_ms": summarize(times), "python_peak_bytes": peak,
            "rss_delta_bytes": _rss_bytes(process) - rss, "handle_delta": _handles() - handles}, result


@dataclass
class CallCounter:
    """按 模块:属性 包装目标并计数, 目标不存在时记为 n/a 以便新旧版本共用脚本"""

    targets: list[str]
    counts: dict[str, int | str] = field(default_factory=dict[str, int | str])

    @contextmanager
    def active(self) -> Iterator[CallCounter]:
        """
        在作用域内包装全部目标, 退出时恢复原对象

        返回:
        - 当前计数器
        """
        restore: list[tuple[object, str, object]] = []
        try:
            for target in self.targets:
                module_name, _, attribute = target.partition(":")
                owner: object = importlib.import_module(module_name)
                *path, name = attribute.split(".")
                try:
                    for part in path:
                        owner = getattr(owner, part)
                    original: object = getattr(owner, name)
                except AttributeError:
                    self.counts[target] = "n/a"
                    continue
                self.counts[target] = 0
                restore.append((owner, name, original))
                setattr(owner, name, self._wrap(target, original))
            yield self
        finally:
            for owner, name, original in reversed(restore):
                setattr(owner, name, original)

    def _wrap(self, target: str, original: object) -> Callable[..., object]:
        """
        生成计数包装

        参数:
        - target: 计数键
        - original: 原可调用对象

        返回:
        - 包装后的可调用对象
        """
        call = original if callable(original) else None
        if call is None:
            raise TypeError(f"计数目标不可调用: {target}")

        def wrapper(*args: object, **kwargs: object) -> object:
            count = self.counts[target]
            self.counts[target] = count + 1 if isinstance(count, int) else count
            return call(*args, **kwargs)
        return wrapper

    def snapshot(self) -> dict[str, int | str]:
        """返回当前计数副本并清零, 便于按场景读取"""
        result = dict(self.counts)
        self.counts = {key: 0 if isinstance(value, int) else value for key, value in self.counts.items()}
        return result


class _CountingCursor(sqlite3.Cursor):
    """累计 fetch 返回行数, 计数表由连接注入"""

    stats: dict[str, int] = {}

    def fetchall(self) -> list[Any]:
        rows = super().fetchall()
        self.stats["rows"] = self.stats.get("rows", 0) + len(rows)
        return rows

    def fetchone(self) -> Any:
        row = super().fetchone()
        self.stats["rows"] = self.stats.get("rows", 0) + (row is not None)
        return row


@contextmanager
def count_sql() -> Iterator[dict[str, int]]:
    """
    统计作用域内新建 SQLite 连接执行的语句数, 返回行数和连接数

    返回:
    - 实时更新的 queries, rows, connections 计数; 直接迭代游标的行不计入 rows
    """
    stats = {"queries": 0, "rows": 0, "connections": 0}
    original = sqlite3.connect

    class _Cursor(_CountingCursor):
        """绑定本次统计表"""

    _Cursor.stats = stats

    class _Connection(sqlite3.Connection):
        """使 execute 返回计数游标"""

        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            return self.cursor(_Cursor).execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        stats["connections"] += 1
        kwargs.setdefault("factory", _Connection)
        connection = original(*args, **kwargs)
        connection.set_trace_callback(lambda _statement: stats.__setitem__("queries", stats["queries"] + 1))
        return connection

    setattr(sqlite3, "connect", connect)
    try:
        yield stats
    finally:
        setattr(sqlite3, "connect", original)


async def loop_lag(work: Callable[[], Awaitable[object]], interval: float = 0.005) -> dict[str, float]:
    """
    执行协程期间以固定间隔探测事件循环延迟

    参数:
    - work: 被测协程工厂
    - interval: 探测间隔秒数

    返回:
    - 最大与 p95 延迟毫秒; 探测先完成一轮睡眠再进入被测协程, 开始前就发生的阻塞同样能测到
    """
    lags: list[float] = []
    done = asyncio.Event()
    started = asyncio.Event()

    async def probe() -> None:
        while not done.is_set():
            start = time.perf_counter()
            await asyncio.sleep(interval)
            lags.append(max(0.0, time.perf_counter() - start - interval) * 1000)
            started.set()

    task = asyncio.create_task(probe())
    await started.wait()
    try:
        await work()
    finally:
        done.set()
        await task
    summary = summarize(lags or [0.0])
    return {"max_ms": summary["max"], "p95_ms": summary["p95"]}


def result_hash(value: object) -> str:
    """
    计算规范化结果哈希, 优化前后不一致即判定语义变化

    参数:
    - value: 可 JSON 序列化的场景输出, 不可序列化对象按 repr 处理

    返回:
    - sha256 十六进制摘要
    """
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr).encode("utf-8")).hexdigest()


def source_hashes(files: list[str]) -> dict[str, str]:
    """
    记录被测源码指纹, 未提交工作区也能精确追溯

    参数:
    - files: 相对仓库根的路径, 缺失文件记为 missing

    返回:
    - 路径到 sha256 的映射
    """
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() if (ROOT / name).exists() else "missing" for name in files}


def environment(script: Path, sources: dict[str, str]) -> dict[str, Any]:
    """
    采集运行环境与版本信息

    参数:
    - script: 基准脚本路径
    - sources: 已计算的源码指纹

    返回:
    - 环境描述字典
    """
    def version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "missing"

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8").strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--", "satrap"], cwd=ROOT, text=True, encoding="utf-8").strip())
    return {"python": sys.version, "platform": platform.platform(), "processor": platform.processor(),
            "cpu_count": os.cpu_count(), "sqlite": sqlite3.sqlite_version, "psutil": version("psutil"),
            "git_head": head, "satrap_dirty": dirty, "source_sha256": sources,
            "benchmark_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def write_result(output: Path, payload: dict[str, Any]) -> None:
    """
    以 UTF-8 写出结果, 拒绝覆盖已有基线

    参数:
    - output: 输出路径
    - payload: 结果主体, 自动补充 schema_version 与 created_at
    """
    if output.exists():
        raise FileExistsError(f"输出已存在, 请使用新文件名保留旧基线: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), **payload}
    output.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
