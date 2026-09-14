"""
PDF 分页文本与视觉读取

保留原文本提取接口的契约, 仅为交互式文档工具按页生成图像,
限制源文件, 页数, 渲染尺寸和输出总量, 返回带页码的工具媒体结果
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from threading import RLock
import base64
import io

from satrap.core.utils.documents import DEFAULT_MAX_FILE_SIZE, DEFAULT_MAX_PDF_PAGES
from satrap.core.utils.media import MEDIA_RESULT_KEY

_PDF_LOCK = RLock()
# 同步和异步工具可能同时调用本模块, 串行使用 PDFium 原生资源


def read_pdf_pages(path: Path, *, visual: bool, start_page: int = 1, page_count: int = 5, max_length: int = 131072) -> dict[str, Any] | str:
    """
    读取指定 PDF 页面的文字和可选页面图像

    参数:
    - path: 已通过会话路径保护的 PDF 文件
    - visual: 是否生成页面图片
    - start_page: 起始页码, 从 1 开始
    - page_count: 本次页数, 范围 1 至 5
    - max_length: 返回文本字符上限, 默认 131072

    返回:
    - 文本模式返回分页文字, 视觉模式返回可序列化的工具媒体结果
    """
    if isinstance(start_page, bool) or not isinstance(start_page, int) or start_page < 1:
        raise ValueError("start_page 必须是从 1 开始的整数")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or not 1 <= page_count <= 5:
        raise ValueError("page_count 必须是 1 至 5 的整数")
    if path.stat().st_size > DEFAULT_MAX_FILE_SIZE:
        raise ValueError("PDF 文件超过允许大小")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ValueError("PDF 分页读取需要安装 pypdfium2") from exc
    media: list[dict[str, Any]] = []
    text_parts: list[str] = []
    used_bytes = 0
    used_chars = 0
    with _PDF_LOCK, pdfium.PdfDocument(str(path)) as document:
        total = len(document)
        if total > DEFAULT_MAX_PDF_PAGES or start_page > total:
            raise ValueError(f"PDF 共 {total} 页, 页数超限或起始页不存在")
        end_page = min(total, start_page + page_count - 1)
        for number in range(start_page, end_page + 1):
            page = document[number - 1]
            try:
                text_page = page.get_textpage()
                try:
                    text = text_page.get_text_range().strip()
                finally:
                    text_page.close()
                remaining = max(0, max_length - used_chars)
                excerpt = text[:remaining] if text else "[该页没有可提取的文本层]"
                used_chars += len(text)
                text_parts.append(f"# Page {number}\n{excerpt}")
                if visual:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise ValueError("PDF 页面尺寸无效")
                    bitmap = page.render(scale=min(2.0, 1600 / max(width, height)))
                    try:
                        with bitmap.to_pil().convert("RGB") as image:
                            buffer = io.BytesIO()
                            image.save(buffer, format="JPEG", quality=80)
                    finally:
                        bitmap.close()
                    raw = buffer.getvalue()
                    used_bytes += len(raw)
                    if used_bytes > 16 * 1024 * 1024:
                        raise ValueError("PDF 页面图像总量超过 16 MiB, 请减少页数")
                    media.extend([
                        {"type": "text", "text": f"{path.name}, 第 {number} 页"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")}},
                    ])
            finally:
                page.close()
    metadata = {"total_pages": total, "start_page": start_page, "end_page": end_page,
                "next_page": end_page + 1 if end_page < total else None, "text_truncated": used_chars > max_length}
    summary = f"PDF 共 {total} 页, 本次读取 {start_page}-{end_page} 页"
    if end_page < total:
        summary += f", 下一页为 {end_page + 1}"
    if used_chars > max_length:
        summary += ", 文本已截断"
    text = summary + "\n\n" + "\n\n".join(text_parts)
    if not visual:
        return text
    return {MEDIA_RESULT_KEY: 1, "text": text, "media": media, "metadata": metadata}
