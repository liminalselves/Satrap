from __future__ import annotations

from pathlib import Path


def read_log_increment(
    log_file: Path,
    position: int,
    cached_lines: list[str],
    max_lines: int,
    paused: bool,
) -> tuple[int, list[str], list[str]]:
    """读取新增日志并返回当前位置, 缓冲行和展示行"""
    file_size = log_file.stat().st_size
    if file_size < position:
        # 日志被截断或轮转时, 从新文件开头重新读取.
        position = 0
        cached_lines = []

    if paused:
        return position, cached_lines[-max_lines:], cached_lines[-max_lines:]

    with log_file.open("rb") as f:
        f.seek(position)
        data = f.read()
        position = f.tell()
    new_lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)

    if new_lines:
        cached_lines = (cached_lines + new_lines)[-max_lines:]
    else:
        cached_lines = cached_lines[-max_lines:]

    return position, cached_lines, cached_lines[-max_lines:]
