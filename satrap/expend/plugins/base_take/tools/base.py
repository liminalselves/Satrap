"""base_take 共享工具核心, 统一文档能力选择"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from satrap.expend.plugins.base_take.core.docread import extract_text
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
