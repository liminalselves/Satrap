"""
base_take 文档和本地图片读取核心

同步和异步工具共用会话路径校验, 按模型能力返回文本或固定的工具媒体结果
"""
from __future__ import annotations
from pathlib import Path
from satrap.edictum import PluginHost, AsyncSimpleSession, SimpleSession
from satrap.core.plugin_interop.environment import EnvironmentSnapshot, environment_snapshot
from satrap.core.plugin_interop.contracts import PluginInteractionError
from typing import Any
import traceback
import base64
import io

from satrap.expend.plugins.base_take.core.docread import extract_text
from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.utils.media import MAX_MEDIA_BYTES, MEDIA_RESULT_KEY, visual_enabled
from .utils import _resolve_doc_path, _doc_workspace_root, _doc_upload_root

from satrap.core.log import logger

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


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

    def _verify_environment(self, expected: EnvironmentSnapshot) -> None:
        """
        在文件读取前后拒绝项目或目录改绑

        参数:
        - expected: 本次操作开始时冻结的环境
        """
        if self._environment() != expected:
            logger.warning("[base_take] 宿主环境已改变, 拒绝返回旧操作的文件内容")
            raise PluginInteractionError("capability_stale", "文件读取期间宿主环境已经变化")


    def __init__(self, workspace_root: Path) -> None:
        """
        初始化 ReadDocumentTool

        参数:
        - workspace_root: 工作区根目录
        """
        super().__init__()
        self._base_root = workspace_root
        """独立构造时的基线目录; 宿主插件操作使用当前环境快照覆盖"""

    def _read_document(self, path: str, max_length: int = 131072, mode: str = "auto", start_page: int = 1, page_count: int = 5, *, environment: EnvironmentSnapshot | None = None) -> str | dict[str, Any]:
        """
        按会话模型能力读取文档文本, 本地图片和 PDF 页面

        参数:
        - path: 路径
        - max_length: 最大长度
        - mode: auto 随模型能力选择, text 只读文字, visual 返回图片或页面图像
        - start_page: PDF 起始页码, 默认 1
        - page_count: PDF 本次读取页数, 默认 5, 最多 5
        - environment: 执行入口冻结的环境, 默认在当前同步操作开始时读取

        返回:
        - 文本或工具媒体结果, 路径和读取错误返回错误文本
        """
        environment = environment if environment is not None else self._environment()
        self._verify_environment(environment)
        try:
            abs_path = _resolve_doc_path(
                path, _doc_workspace_root(self, environment), _doc_upload_root(self, environment)
            )
        except ValueError as e:
            return f"错误: {e}"
        try:
            limit = max(1, min(1_000_000, int(max_length)))
        except (TypeError, ValueError, OverflowError):
            return "错误: max_length 必须是整数"
        if not isinstance(mode, str) or mode not in {"auto", "text", "visual"}:
            return "错误: mode 必须是 auto, text 或 visual"
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
        - path: 当前会话工作区或上传目录内的图片文件

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
