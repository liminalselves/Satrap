"""提供增量日志读取与暂停状态缓冲逻辑"""
from __future__ import annotations

from pathlib import Path


def read_log_increment(
    log_file: Path,
    position: int,
    cached_lines: list[str],
    max_lines: int,
    paused: bool,
) -> tuple[int, list[str], list[str]]:
    """
    读取新增日志并返回读取位置, 缓冲行和展示行

    参数:
    - log_file: 日志文件
    - position: 当前读取位置
    - cached_lines: 已缓冲的日志行
    - max_lines: 最大缓冲行数
    - paused: 是否暂停读取

    返回:
    - tuple[int, list[str], list[str]]: 读取位置, 缓冲行和展示行
    """
    file_size = log_file.stat().st_size
    if file_size < position:
        position = 0
        cached_lines = []
    if paused:
        return position, cached_lines[-max_lines:], cached_lines[-max_lines:]

    with log_file.open("rb") as file:
        file.seek(position)
        data = file.read()
        position = file.tell()
    new_lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    if new_lines:
        cached_lines = (cached_lines + new_lines)[-max_lines:]
    else:
        cached_lines = cached_lines[-max_lines:]
    return position, cached_lines, cached_lines[-max_lines:]
