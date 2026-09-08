from __future__ import annotations
from pathlib import Path


def write_file(abs_path: Path, content: str, append: bool) -> str:
    """执行已授权的文件修改并生成结果"""
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(abs_path, mode, encoding="utf-8") as f:
            f.write(content)
        return f"已{'追加' if append else '写入'}: {abs_path}"
    except OSError as e:
        return f"错误: 写入失败: {e}"


def edit_file(
    abs_path: Path, content: str, old: str, new: str, replace_all: bool
) -> str:
    """执行已授权的文件修改并生成结果"""
    try:
        if replace_all:
            updated = content.replace(old, new)
        else:
            updated = content.replace(old, new, 1)
        abs_path.write_text(updated, encoding="utf-8")
        return f"已编辑: {abs_path} ({content.count(old)} 处匹配, 修改 {'全部' if replace_all else '首处'})"
    except OSError as e:
        return f"错误: 写入失败: {e}"


def replace_file(
    abs_path: Path, content: str, pairs: list[tuple[str, str, bool]]
) -> str:
    """执行已授权的文件修改并生成结果"""
    updated = content
    counts: list[int] = []
    for old, new, all_ in pairs:
        counts.append(updated.count(old))
        updated = updated.replace(old, new, -1 if all_ else 1)
    try:
        abs_path.write_text(updated, encoding="utf-8")
    except OSError as e:
        return f"错误: 写入失败: {e}"
    detail = ", ".join(f"'{old[:20]}' {c} 处" for (old, _, _), c in zip(pairs, counts))
    return f"已批量替换: {abs_path} ({detail})"
