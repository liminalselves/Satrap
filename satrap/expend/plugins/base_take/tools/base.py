"""base_take 共享工具核心, 统一文档能力选择与记忆操作"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from satrap.expend.plugins.base_take.core.docread import extract_text
from satrap.expend.tools.memory_store import MemoryStore
from .utils import _resolve_doc_path, _doc_workspace_root, _doc_upload_root
from satrap.core.utils.TCBuilder.tool_base import _ToolBase


class _DocumentCore(_ToolBase):
    recovery_policy = "retry"

    def __init__(self, workspace_root: Path) -> None:
        """
        初始化 ReadDocumentTool

        参数:
        - workspace_root: 工作区根目录
        """
        super().__init__()
        self._base_root = workspace_root
        """安装期基线工作区根 (cfg/全局); 项目会话经会话鸭子属性在调用时覆盖"""

    def _read_document(self, path: str, max_length: int = 131072, mode: str = "auto", start_page: int = 1, page_count: int = 5) -> str | dict[str, Any]:
        """
        按会话模型能力读取文档文本和 PDF 页面

        参数:
        - path: 路径
        - max_length: 最大长度
        - mode: auto 随模型能力选择, text 只读文字, visual 返回页面图像
        - start_page: PDF 起始页码, 默认 1
        - page_count: PDF 本次读取页数, 默认 5, 最多 5

        返回:
        - 文本或工具媒体结果, 路径和读取错误返回错误文本
        """
        try:
            abs_path = _resolve_doc_path(
                path, _doc_workspace_root(self), _doc_upload_root(self)
            )
        except ValueError as e:
            return f"错误: {e}"
        try:
            limit = max(1, min(1_000_000, int(max_length)))
        except (TypeError, ValueError, OverflowError):
            return "错误: max_length 必须是整数"
        if not isinstance(mode, str) or mode not in {"auto", "text", "visual"}:
            return "错误: mode 必须是 auto, text 或 visual"
        from satrap.core.utils.media import visual_enabled
        from satrap.core.utils.pdf_pages import read_pdf_pages

        enabled = visual_enabled(getattr(getattr(self, "_session", None), "llm", None))
        if mode == "visual" and not enabled:
            return "错误: 当前模型未启用图像与视频输入"
        if abs_path.suffix.lower() == ".pdf":
            try:
                return read_pdf_pages(abs_path, visual=enabled and mode != "text", start_page=start_page, page_count=page_count, max_length=limit)
            except Exception as error:
                return f"错误: PDF 读取失败: {error}"
        if mode == "visual":
            return "错误: visual 模式当前仅支持 PDF"
        try:
            text = extract_text(abs_path, max_length=limit + 1)
        except ValueError as e:
            return f"错误: {e}"
        if len(text) > limit:
            return text[:limit] + f"\n... (已截断, 共 {len(text)} 字符)"
        return text or "(文档为空)"


class _MemoryBinding(_ToolBase):
    def __init__(self, store: MemoryStore) -> None:
        """
        初始化 _MemoryToolBase

        参数:
        - store: 存储实例
        """
        super().__init__()
        self.store = store


class _AddMemoryToolCore(_MemoryBinding):
    """添加一条长期记忆 (用户偏好/项目约定/关键决策)"""

    tool_name = "add_memory"
    description = "添加一条当前会话的长期记忆, 记忆仅会注入当前会话的后续上下文"
    params_dict = {
        "title": ("string", "简短记忆标题"),
        "content": ("string", "记忆内容"),
        "tags": ("array", "分类标签"),
        "importance": ("number", "重要程度 1-5, 默认 1"),
    }

    def _execute(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 1,
    ) -> str:
        """
        执行

        参数:
        - title: 标题
        - content: 内容
        - tags: 标签集合
        - importance: 重要度

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("添加")
        result = self.store.add(title, content, tags, importance)
        if result.get("ok"):
            return f"记忆已添加: [{title}] {content}"
        return f"添加失败: {result.get('error')}"


class _UpdateMemoryToolCore(_MemoryBinding):
    """更新一条已有记忆"""

    tool_name = "update_memory"
    description = "按记忆 ID 更新已有长期记忆 (信息变化或修正时使用)"
    params_dict = {
        "memory_id": ("string", "要更新的记忆 ID"),
        "content": ("string", "更新后的内容"),
        "title": ("string", "更新后的标题"),
    }

    def _execute(self, memory_id: str, content: str = "", title: str = "") -> str:
        """
        执行

        参数:
        - memory_id: 记忆id
        - content: 内容
        - title: 标题

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("更新")
        fields: dict[str, Any] = {}
        if content:
            fields["content"] = content
        if title:
            fields["title"] = title
        result = self.store.update(memory_id, **fields)
        if result.get("ok"):
            return f"记忆已更新: [{result['title']}] {result['content']}"
        return f"更新失败: {result.get('error')}"


class _DeleteMemoryToolCore(_MemoryBinding):
    """删除一条记忆"""

    tool_name = "delete_memory"
    description = "按记忆 ID 删除一条长期记忆"
    params_dict = {
        "memory_id": ("string", "要删除的记忆 ID"),
    }

    def _execute(self, memory_id: str) -> str:
        """
        执行

        参数:
        - memory_id: 记忆id

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("删除")
        result = self.store.delete(memory_id)
        if result.get("ok"):
            return f"记忆已删除: {memory_id}"
        return f"删除失败: {result.get('error')}"


class _ListMemoriesToolCore(_MemoryBinding):
    """查看全部长期记忆"""

    recovery_policy = "retry"

    tool_name = "list_memories"
    description = "列出当前全部长期记忆 (含 ID, 供 update/delete 定位)"
    params_dict: dict[str, Any] = {}

    def _execute(self) -> str:
        """
        执行

        返回:
        - str: 执行
        """
        memories = self.store.list_all()
        if not memories:
            return "当前没有长期记忆"
        lines = [f"共 {len(memories)} 条记忆:"]
        for m in memories:
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            lines.append(
                f"- {m['id'][:8]} [{m['title']}] {m['content']}{tags} (重要度 {m['importance']})"
            )
        return "\n".join(lines)
