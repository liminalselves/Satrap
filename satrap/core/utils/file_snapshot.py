"""文件来源指纹与无缓存捕获的有界重试, 不代替快照缓存的并发发布协议"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar, overload

FileFingerprint = tuple[int, int, int, int]
_T = TypeVar("_T")


@overload
def file_fingerprint(path: Path, *, missing_ok: Literal[False] = False) -> FileFingerprint: ...


@overload
def file_fingerprint(path: Path, *, missing_ok: Literal[True]) -> FileFingerprint | None: ...


def file_fingerprint(path: Path, *, missing_ok: bool = False) -> FileFingerprint | None:
    """
    读取文件时间, 大小与身份, 原子替换也会改变指纹

    参数:
    - path: 实际文件来源
    - missing_ok: 是否将不存在映射为 None, 其它读取失败仍抛出

    返回:
    - mtime, ctime, size 与 inode, 仅允许缺失时返回 None
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino


def capture_stable_source(capture: Callable[[], _T | None], message: str) -> _T:
    """
    对来源失配的捕获最多重试三次, 解析或访问错误不重试

    参数:
    - capture: 构造并核验一次来源, 仅来源变化返回 None
    - message: 持续变化时的领域说明

    返回:
    - 首个通过来源核验的结果, 三次均失配时抛出 RuntimeError
    """
    for _attempt in range(3):
        result = capture()
        if result is not None:
            return result
    raise RuntimeError(message)
