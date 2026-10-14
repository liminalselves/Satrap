"""JSON 状态文件的原子写入与损坏隔离原语 (跨进程 FileLock 之外的公共部分)

供手动唤醒状态存储与审批账本共用: 同目录临时文件 + os.replace 保证读到的永远是完整旧/新内容,
损坏文件改名隔离保留原始字节供人工核对, 不删除也不就地覆盖
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from collections.abc import Callable
import json
import os
import tempfile
import time


def atomic_write_text(path: Path, text: str, *, newline: str | None = None,
                      durable: bool = False, replace: Callable[[Path, Path], None] | None = None) -> None:
    """
    以同目录 UTF-8 临时文件原子替换文本, 事务锁由调用方持有

    参数:
    - path: 目标文件
    - text: 已序列化的完整文本
    - newline: 文件换行策略, None 保留平台文本换行
    - durable: 替换前是否刷新临时文件到磁盘
    - replace: 替换操作, 配置文档可提供原有的 Windows 重试策略
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", encoding="utf-8", newline=newline, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        (replace or os.replace)(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any], *, indent: int | None = None) -> None:
    """
    保留调用方的 JSON 缩进, 拒绝非有限数值并原子写入

    参数:
    - path: 目标文件, 调用方负责事务锁
    - payload: 完整 JSON 对象
    - indent: 缩进宽度, None 使用紧凑的默认格式
    """
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=indent))


def quarantine_file(path: Path) -> Path:
    """
    把损坏文件改名隔离, 保留原始字节

    参数:
    - path: 待隔离文件

    返回:
    - Path: 隔离后的新路径
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 0
    while target.exists():
        suffix += 1
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
    os.replace(path, target)
    return target
