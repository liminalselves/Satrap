"""JSON 状态文件的原子写入与损坏隔离原语 (跨进程 FileLock 之外的公共部分)

供手动唤醒状态存储与审批账本共用: 同目录临时文件 + os.replace 保证读到的永远是完整旧/新内容,
损坏文件改名隔离保留原始字节供人工核对, 不删除也不就地覆盖
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import tempfile
import time


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """
    以同目录临时文件 + os.replace 原子写入 JSON

    参数:
    - path: 目标文件
    - payload: 可 JSON 序列化的对象
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", encoding="utf-8", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
