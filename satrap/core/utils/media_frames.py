"""有界媒体解码调度和可重建图片缓存, 支持跨平台取消与超时"""
from __future__ import annotations

from collections.abc import Iterator
from contextvars import ContextVar
from contextlib import contextmanager
import subprocess
from threading import BoundedSemaphore, Event
import tempfile
from pathlib import Path
import hashlib
from typing import TypedDict, cast
import shutil
import json
import math
import time
import sys
import os

from satrap.core.utils.media_store import atomic_write, current_store
from satrap.core.storage.file_lock import FileLock

from satrap.core.log import logger


SAMPLER_VERSION = 3
MAX_CACHE_BYTES = 256 * 1024 * 1024
CACHE_TTL = 24 * 60 * 60
DECODE_TIMEOUT = 30.0
_SLOTS = BoundedSemaphore(2)
_CANCEL: ContextVar[Event | None] = ContextVar("media_decode_cancel", default=None)


class MediaRangeRequired(ValueError):
    """保持采样间隔需要分段读取, 不能静默减少采样画面"""


class Sample(TypedDict):
    """一个用于模型输入的采样画面"""

    data: bytes
    time: float


class Samples(TypedDict):
    """有明确区间和时间信息的完整采样结果"""

    frames: list[Sample]
    duration: float
    start: float
    end: float
    interval: float
    kind: str


@contextmanager
def cancellable_media() -> Iterator[None]:
    """为本轮媒体任务绑定取消信号, 异步轮次取消后后台线程终止子进程"""
    event = Event()
    token = _CANCEL.set(event)
    try:
        yield
    finally:
        event.set()
        _CANCEL.reset(token)


def _prune(root: Path, protected: Path | None = None) -> None:
    """
    在缓存锁内按过期时间和最近访问时间淘汰完整缓存目录

    参数:
    - root: 当前平台的抽帧缓存根目录
    - protected: 本次刚生成且尚未读完的缓存目录
    """
    entries: list[tuple[float, int, Path]] = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or entry.name.startswith("."):
            continue
        size = sum(item.stat().st_size for item in entry.iterdir() if item.is_file())
        entries.append((entry.stat().st_mtime, size, entry))
    total = sum(size for _, size, _ in entries)
    for modified, size, entry in sorted(entries):
        if entry != protected and (time.time() - modified > CACHE_TTL or total > MAX_CACHE_BYTES):
            shutil.rmtree(entry)
            total -= size


def _decode(source: Path, output: Path, kind: str, interval: float, start: float, end: float | None) -> dict[str, object]:
    """
    运行当前解释器的独立解码进程并在取消或超时后回收

    参数:
    - source: 已校验的本地源媒体
    - output: 独占临时输出目录
    - kind: animation 或 video
    - interval: 已校验的采样间隔
    - start: 起始秒数
    - end: 可选结束秒数

    返回:
    - worker 发布的结果元数据; 失败时抛出 ValueError
    """
    if not _SLOTS.acquire(blocking=False):
        raise ValueError("视频处理繁忙, 请稍后重试")
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).with_name("media_frame_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        request = json.dumps({"source": str(source), "output": str(output), "kind": kind,
                              "interval": interval, "start": start, "end": end}).encode("utf-8")
        deadline = time.monotonic() + DECODE_TIMEOUT
        cancel = _CANCEL.get()
        first = True
        while True:
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                raise ValueError("媒体处理已取消或超时, 请缩短读取区间")
            try:
                stdout, stderr = process.communicate(input=request if first else None, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                first = False
        if process.returncode:
            logger.warning(f"[媒体解码] worker 异常退出, code={process.returncode}, stderr={stderr[-512:]!r}")
            raise ValueError("媒体解码进程失败, 请检查格式或缩短读取区间")
        response = cast(dict[str, object], json.loads(stdout.decode("utf-8")))
        if response.get("ok") is not True:
            if response.get("code") == "RangeRequired":
                raise MediaRangeRequired(str(response["error"]))
            raise ValueError(str(response.get("error", "媒体解码失败")))
        return cast(dict[str, object], response["result"])
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
        _SLOTS.release()


def sample_frames(data: bytes, kind: str, interval: float, start: float = 0.0, end: float | None = None) -> Samples:
    """
    按内容及采样参数复用缓存, 返回有界图片字节和实际时间戳

    参数:
    - data: 已固定的原媒体字节
    - kind: animation 或 video
    - interval: 采样间隔秒数
    - start: 起始秒数, 默认 0
    - end: 结束秒数, 默认读取到媒体结尾

    返回:
    - 完整采样结果; 缓存缺失时重新解码, 不返回半成品
    """
    from satrap.core.utils.media_config import validate_video_settings
    validate_video_settings(False, interval)
    if not data or len(data) > 32 * 1024 * 1024:
        raise ValueError("原媒体为空或超过 32 MiB")
    if kind not in {"animation", "video"} or isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0 or (end is not None and (isinstance(end, bool) or not isinstance(end, (int, float)) or not math.isfinite(end) or end <= start)):
        raise ValueError("媒体类型或读取时间区间无效")
    digest = hashlib.sha256(data).hexdigest()
    key = hashlib.sha256(json.dumps([SAMPLER_VERSION, digest, kind, interval, start, end, 1600]).encode()).hexdigest()
    root = current_store().root / "frames"
    root.mkdir(parents=True, exist_ok=True)
    target = root / key
    def read_cached() -> Samples:
        """在全局缓存锁内读取完整结果, 不让淘汰与当前读取交叉"""
        result = cast(dict[str, object], json.loads((target / "result.json").read_text(encoding="utf-8")))
        if "range_error" in result:
            os.utime(target, None)
            _prune(root, target)
            raise MediaRangeRequired(str(result["range_error"]))
        entries = cast(list[dict[str, object]], result["frames"])
        if not 1 <= len(entries) <= 16:
            raise ValueError("采样缓存帧数无效")
        frames: list[Sample] = []
        total = 0
        for index, item in enumerate(entries):
            if item["file"] != f"frame-{index:02d}.jpg":
                raise ValueError("采样缓存文件名无效")
            path = target / str(item["file"])
            if path.is_symlink():
                raise ValueError("采样缓存不允许符号链接")
            with path.open("rb") as stream:
                data = stream.read(24 * 1024 * 1024 + 1)
            total += len(data)
            if total > 24 * 1024 * 1024:
                raise ValueError("采样缓存内容超限")
            frames.append({"data": data, "time": float(cast(float, item["time"]))})
        os.utime(target, None)
        _prune(root, target)
        return {"frames": frames, "duration": float(cast(float, result["duration"])), "start": float(cast(float, result["start"])),
                "end": float(cast(float, result["end"])), "interval": interval, "kind": kind}

    with FileLock(root / (".bucket-" + key[:2] + ".lock")):
        with FileLock(root / ".cache.lock"):
            if (target / "result.json").is_file():
                try:
                    return read_cached()
                except MediaRangeRequired:
                    raise
                except (OSError, ValueError, KeyError, TypeError):
                    shutil.rmtree(target)
        with tempfile.TemporaryDirectory(prefix=".decode-", dir=root) as temporary:
            work = Path(temporary)
            source = work / "source"
            source.write_bytes(data)
            output = work / "output"
            output.mkdir()
            try:
                result = _decode(source, output, kind, interval, start, end)
            except MediaRangeRequired as error:
                with FileLock(root / ".cache.lock"):
                    atomic_write(target / "result.json", json.dumps({"range_error": str(error)}).encode())
                    _prune(root, target)
                raise
            atomic_write(output / "result.json", json.dumps(result).encode())
            with FileLock(root / ".cache.lock"):
                if target.exists():
                    shutil.rmtree(target)
                os.replace(output, target)
                return read_cached()
