"""
base_take 文档和本地图片读取核心

同步和异步工具共用会话路径校验, 按模型能力返回文本或固定的工具媒体结果
"""
from __future__ import annotations

from dataclasses import dataclass
import traceback
from pathlib import Path
from typing import Any
import base64
import io

from satrap.expend.plugins.base_take.core.docread import extract_text
from satrap.core.plugin_interop.environment import EnvironmentSnapshot, environment_snapshot
from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.file_access.reads import FileRead, prepare_file_read
from satrap.core.utils.documents import SUPPORTED_EXTENSIONS
from satrap.core.utils.media import MAX_MEDIA_BYTES, MEDIA_RESULT_KEY, visual_enabled
from satrap.edictum import PluginHost, AsyncSimpleSession, SimpleSession
from .utils import _doc_workspace_root

from satrap.core.log import logger

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


@dataclass(frozen=True)
class DocumentRead:
    """固定文档路径及其解析范围"""

    file: FileRead
    max_length: int
    mode: str
    start_page: int
    page_count: int


class _DocumentCore(_ToolBase):
    recovery_policy = "retry"
    _session: SimpleSession | AsyncSimpleSession | None = None
    _plugin_host: PluginHost | None = None

    def _environment(self) -> EnvironmentSnapshot:
        """
        获取当前操作的宿主环境

        返回:
        - 已核验安装代次的快照, 独立工具读取统一会话绑定
        """
        return self._plugin_host.environment_snapshot() if self._plugin_host is not None else environment_snapshot(self._session)

    def __init__(self, workspace_root: Path) -> None:
        """
        初始化 ReadDocumentTool

        参数:
        - workspace_root: 工作区根目录
        """
        super().__init__()
        self._base_root = workspace_root
        """独立构造时的基线目录; 宿主插件操作使用当前环境快照覆盖"""

    def _prepare_document(self, path: str, max_length: int, mode: str, start_page: int, page_count: int) -> DocumentRead:
        """
        校验解析参数并固定本次单文件读取

        参数:
        - path: 显式文件路径, 相对路径基于工作区
        - max_length: 文本字符上限
        - mode: auto, text 或 visual
        - start_page: PDF 起始页码
        - page_count: PDF 本次读取页数

        返回:
        - 固定路径和规范化参数, 无效输入在审批前拒绝
        """
        try:
            limit = max(1, min(1_000_000, int(max_length)))
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("max_length 必须是整数") from error
        if not isinstance(mode, str) or mode not in {"auto", "text", "visual"}:
            raise ValueError("mode 必须是 auto, text 或 visual")
        environment = self._environment()
        read = prepare_file_read(
            self, path, workspace=_doc_workspace_root(self, environment), environment=environment,
            current_environment=self._environment,
            parameters={"max_length": limit, "mode": mode, "start_page": start_page, "page_count": page_count},
        )
        suffix = read.path.suffix.lower()
        enabled = visual_enabled(getattr(self._session, "llm", None))
        if mode == "visual" and not enabled:
            raise ValueError("当前模型未启用图像与视频输入")
        if suffix in IMAGE_SUFFIXES and (not enabled or mode == "text"):
            raise ValueError("图片需要启用模型的图像与视频输入, 并使用 auto 或 visual 模式; 本工具不提供文字模型 OCR")
        if suffix not in IMAGE_SUFFIXES and suffix != ".pdf" and mode == "visual":
            raise ValueError("visual 模式仅支持图片和 PDF")
        if suffix not in IMAGE_SUFFIXES and suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文档类型: {suffix}")
        if read.path.stat().st_size > MAX_MEDIA_BYTES:
            raise ValueError("文档或图片文件超过 32 MiB 限制")
        if suffix == ".pdf":
            if isinstance(start_page, bool) or not isinstance(start_page, int) or start_page < 1:
                raise ValueError("start_page 必须是从 1 开始的整数")
            if isinstance(page_count, bool) or not isinstance(page_count, int) or not 1 <= page_count <= 5:
                raise ValueError("page_count 必须是 1 至 5 的整数")
        return DocumentRead(read, limit, mode, start_page, page_count)

    def _read_document(self, path: Path, options: DocumentRead) -> str | dict[str, Any]:
        """
        解析通过宿主审批与复核的固定文件

        参数:
        - path: 本次固定的真实文件路径
        - options: 审批绑定的文本或页面范围

        返回:
        - 文本或固定媒体结果; 格式解析失败时返回错误文本
        """
        abs_path, limit = path, options.max_length
        mode, start_page, page_count = options.mode, options.start_page, options.page_count
        from satrap.core.utils.pdf_pages import read_pdf_pages

        enabled = visual_enabled(getattr(getattr(self, "_session", None), "llm", None))
        if mode == "visual" and not enabled:
            return "错误: 当前模型未启用图像与视频输入"
        if abs_path.suffix.lower() in IMAGE_SUFFIXES:
            if not enabled or mode == "text":
                logger.warning(f"[base_take] 图片读取未启用视觉能力或使用了 text 模式: {abs_path}")
                return "错误: 图片需要启用模型的图像与视频输入, 并使用 auto 或 visual 模式; 本工具不提供文字模型 OCR"
            try:
                return self._read_image(abs_path)
            except (OSError, ValueError) as error:
                logger.warning(f"[base_take] 本地图片读取失败: {abs_path}: {error}")
                return f"错误: 图片读取失败: {error}"
            except Exception as error:
                logger.error(f"[base_take] 本地图片读取异常: {abs_path}: {error}\n{traceback.format_exc()}")
                return f"错误: 图片读取失败: {error}"
        if abs_path.suffix.lower() == ".pdf":
            try:
                return read_pdf_pages(abs_path, visual=enabled and mode != "text", start_page=start_page, page_count=page_count, max_length=limit)
            except Exception as error:
                return f"错误: PDF 读取失败: {error}"
        if mode == "visual":
            return "错误: visual 模式仅支持图片和 PDF"
        try:
            text = extract_text(abs_path, max_length=limit + 1)
        except ValueError as e:
            return f"错误: {e}"
        if len(text) > limit:
            return text[:limit] + f"\n... (已截断, 共 {len(text)} 字符)"
        return text or "(文档为空)"

    def _read_image(self, path: Path) -> dict[str, object]:
        """
        将已校验路径的本地图片固定为工具视觉结果

        参数:
        - path: 已通过宿主文件边界和审批的固定图片路径

        返回:
        - 带文件名和尺寸的 PNG 媒体结果; 动图及多页图片仅取第一帧, 超限或无效图片抛出异常
        """
        from PIL import ImageOps, Image

        if path.stat().st_size > MAX_MEDIA_BYTES:
            raise ValueError("图片文件超过 32 MiB 限制")
        with Image.open(path) as original:
            if original.width * original.height > 40_000_000:
                raise ValueError("图片超过 4000 万像素限制")
            width, height = original.size
            first_frame = getattr(original, "n_frames", 1) > 1
            with ImageOps.exif_transpose(original) as oriented:
                oriented.thumbnail((2048, 2048))
                with oriented.convert("RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB") as image:
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
        raw = buffer.getvalue()
        if len(raw) > MAX_MEDIA_BYTES:
            raise ValueError("转换后的图片超过 32 MiB 限制")
        text = f"图片: {path.name}, 原始尺寸 {width} x {height}"
        if first_frame:
            text += ", 本次仅查看第一帧"
        return {MEDIA_RESULT_KEY: 1, "text": text, "media": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}},
        ]}
