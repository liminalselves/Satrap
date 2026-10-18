"""
编程文件修改的准备快照与提交

同步和异步工具共享精确修改内容, 审批后再次核验原文件,
只提交审批时冻结的结果, 避免等待期间覆盖外部修改
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
import difflib
import hashlib
import os

from .contracts import prepare_replacements

from satrap.core.log import logger


@dataclass(frozen=True)
class FileChange:
    """一次文件修改的原始字节快照, 预期结果及工具回执"""

    path: Path
    before: bytes | None
    after: str
    result: str

    def action(self) -> dict[str, object]:
        """
        生成包含实际内容的审批材料

        返回:
        - 原始文件摘要, 修改前后文本和完整 diff
        """
        previous = (self.before or b"").decode("utf-8")
        return {"path": str(self.path), "existed": self.before is not None,
                "before_sha256": hashlib.sha256(self.before or b"").hexdigest(),
                "before": previous, "after": self.after,
                "diff": "".join(difflib.unified_diff(previous.splitlines(True), self.after.splitlines(True),
                                                   fromfile=str(self.path), tofile=str(self.path)))}

    def commit(self, check: Callable[[], None]) -> str:
        """
        紧邻实际写入核验权限和文件快照

        参数:
        - check: 等待后重新校验权限和宿主边界的回调

        返回:
        - 成功回执或明确取消和写入失败信息
        """
        try:
            # Step.1 等待后核验边界与原文件
            check()
            current = self.path.read_bytes() if self.path.exists() else None
            if current != self.before:
                raise PermissionError("文件在审批期间发生变化, 请重新读取后修改")
            # Step.2 提交冻结的完整结果
            self.path.parent.mkdir(parents=True, exist_ok=True)
            check()
            self.path.write_text(self.after, encoding="utf-8", newline="")
            return self.result
        except (OSError, ValueError) as error:
            logger.warning(f"[satrap_coding] 文件提交失败, 路径={self.path}, 原因={error}")
            return f"执行已取消: {error}"


def prepare_change(path: Path, operation: str, *, content: str = "", append: bool = False,
                   old: str = "", new: str = "", replace_all: bool = False,
                   replacements: Sequence[object] | None = None) -> FileChange:
    """
    在审批前计算实际文件变化, 保留原始字节用于等待后的冲突检查

    参数:
    - path: 已完成边界校验的目标
    - operation: write_file, edit_file 或 search_replace
    - content: write_file 写入的原文
    - append: 是否追加, 默认覆盖
    - old: edit_file 待匹配原文
    - new: edit_file 替换文本
    - replace_all: edit_file 是否替换全部
    - replacements: search_replace 全部替换项

    返回:
    - 不可变修改快照; 输入或读取失败抛给工具入口处理
    """
    # Step.1 保存原始字节并按文本工具语义读取
    before = path.read_bytes() if path.exists() else None
    original = (before or b"").decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    # Step.2 计算具体操作的完整修改结果
    if operation == "write_file":
        if not isinstance(content, str) or not isinstance(append, bool):
            raise ValueError("content 必须为文本, append 必须为布尔值")
        updated = ((before or b"").decode("utf-8") if append else "") + content.replace("\n", os.linesep)
        result = f"已{'追加' if append else '写入'}: {path}"
    else:
        if before is None:
            raise ValueError(f"文件不存在: {path}")
        if operation == "edit_file":
            if not isinstance(old, str) or not old or old not in original:
                raise ValueError(f"未找到匹配文本: {str(old)[:80]}")
            updated = original.replace(old, new, -1 if replace_all else 1)
            result = f"已编辑: {path} ({original.count(old)} 处匹配, 修改 {'全部' if replace_all else '首处'})"
        else:
            pairs = prepare_replacements(original, replacements)
            if isinstance(pairs, str):
                raise ValueError(pairs)
            updated = original
            counts: list[int] = []
            for previous, following, all_matches in pairs:
                counts.append(updated.count(previous))
                updated = updated.replace(previous, following, -1 if all_matches else 1)
            detail = ", ".join(f"'{previous[:20]}' {count} 处" for (previous, _, _), count in zip(pairs, counts))
            result = f"已批量替换: {path} ({detail})"
        updated = updated.replace("\n", os.linesep)
    # Step.3 返回不可变快照用于审批和提交
    return FileChange(path, before, updated, result)
