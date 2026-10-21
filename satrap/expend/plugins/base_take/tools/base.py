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
import math
import mimetypes

from satrap.expend.plugins.base_take.core.docread import extract_text
from satrap.core.plugin_interop.environment import EnvironmentSnapshot, environment_snapshot
from satrap.core.utils.TCBuilder.tool_base import _ToolBase
from satrap.core.file_access.reads import FileRead, prepare_file_read
from satrap.core.utils.documents import SUPPORTED_EXTENSIONS
from satrap.core.utils.media import MAX_MEDIA_BYTES, MEDIA_RESULT_KEY, visual_enabled
from satrap.core.utils.media_store import REFERENCE_PREFIX, store_for
from satrap.edictum import PluginHost, AsyncSimpleSession, SimpleSession
from .utils import _doc_workspace_root

from satrap.core.log import logger

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}

IMAGE_SUFFIXES = {".png", ".apng", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _bounded_media_bytes(path: Path) -> bytes:
    """
    有界读取已授权媒体, 防止文件在初次大小检查后增长

    参数:
    - path: 已通过宿主复核的本地路径

    返回:
    - 不超过 32 MiB 的完整媒体字节
    """
    with path.open("rb") as stream:
        data = stream.read(MAX_MEDIA_BYTES + 1)
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise ValueError("媒体为空或超过 32 MiB")
    return data


@dataclass(frozen=True)
class DocumentRead:
    """固定文档路径及其解析范围"""

    file: FileRead
    max_length: int
    mode: str
    start_page: int
    page_count: int
    start_time: float = 0.0
    end_time: float | None = None


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

    def _prepare_document(self, path: str, max_length: int, mode: str, start_page: int, page_count: int, start_time: float = 0.0, end_time: float | None = None) -> DocumentRead:
        """
        校验解析参数并固定本次单文件读取

        参数:
        - path: 显式文件路径, 相对路径基于工作区
        - max_length: 文本字符上限
        - mode: auto, text 或 visual
        - start_page: PDF 起始页码
        - page_count: PDF 本次读取页数
        - start_time: 动画或视频起始秒数
        - end_time: 可选结束秒数

        返回:
        - 固定路径和规范化参数, 无效输入在审批前拒绝
        """
        try:
            limit = max(1, min(1_000_000, int(max_length)))
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("max_length 必须是整数") from error
        if not isinstance(mode, str) or mode not in {"auto", "text", "visual"}:
            raise ValueError("mode 必须是 auto, text 或 visual")
        if isinstance(start_time, bool) or not isinstance(start_time, (int, float)) or not math.isfinite(start_time) or start_time < 0:
            raise ValueError("start_time 必须是非负有限数字")
        if end_time is not None and (isinstance(end_time, bool) or not isinstance(end_time, (int, float)) or not math.isfinite(end_time) or end_time <= start_time):
            raise ValueError("end_time 必须是大于 start_time 的有限数字")
        environment = self._environment()
        read = prepare_file_read(
            self, path, workspace=_doc_workspace_root(self, environment), environment=environment,
            current_environment=self._environment,
            parameters={"max_length": limit, "mode": mode, "start_page": start_page, "page_count": page_count, "start_time": start_time, "end_time": end_time},
        )
        suffix = read.path.suffix.lower()
        model = getattr(self._session, "llm", None)
        enabled = visual_enabled(model)
        video_enabled = enabled or getattr(model, "supports_native_video_input", False) is True
        if mode == "visual" and not (video_enabled if suffix in VIDEO_SUFFIXES else enabled):
            raise ValueError("当前模型未启用图像与视频输入")
        if suffix in IMAGE_SUFFIXES and (not enabled or mode == "text"):
            raise ValueError("图片需要启用模型的图像与视频输入, 并使用 auto 或 visual 模式; 本工具不提供文字模型 OCR")
        if suffix in VIDEO_SUFFIXES and getattr(model, "supports_native_video_input", False) is True and (start_time != 0 or end_time is not None):
            raise ValueError("原生视频模式不支持按抽帧区间读取, 请读取完整视频")
        if suffix in VIDEO_SUFFIXES and (not video_enabled or mode == "text"):
            raise ValueError("视频需要图片或原生视频能力, 并使用 auto 或 visual 模式")
        if suffix not in IMAGE_SUFFIXES | VIDEO_SUFFIXES and suffix != ".pdf" and mode == "visual":
            raise ValueError("visual 模式仅支持图片和 PDF")
        if suffix not in IMAGE_SUFFIXES | VIDEO_SUFFIXES and suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的文档类型: {suffix}")
        if read.path.stat().st_size > MAX_MEDIA_BYTES:
            raise ValueError("文档或图片文件超过 32 MiB 限制")
        if suffix == ".pdf":
            if isinstance(start_page, bool) or not isinstance(start_page, int) or start_page < 1:
                raise ValueError("start_page 必须是从 1 开始的整数")
            if isinstance(page_count, bool) or not isinstance(page_count, int) or not 1 <= page_count <= 5:
                raise ValueError("page_count 必须是 1 至 5 的整数")
        return DocumentRead(read, limit, mode, start_page, page_count, start_time, end_time)

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
        if abs_path.suffix.lower() in VIDEO_SUFFIXES:
            raw = _bounded_media_bytes(abs_path)
            mime = mimetypes.guess_type(abs_path.name)[0] or "video/mp4"
            return {MEDIA_RESULT_KEY: 1, "text": f"视频: {abs_path.name}", "media": [{"type": "video_url", "video_url": {"url": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"), "start_time": options.start_time, "end_time": options.end_time}}]}
        if mode == "visual" and not enabled:
            return "错误: 当前模型未启用图像与视频输入"
        if abs_path.suffix.lower() in IMAGE_SUFFIXES:
            if not enabled or mode == "text":
                logger.warning(f"[base_take] 图片读取未启用视觉能力或使用了 text 模式: {abs_path}")
                return "错误: 图片需要启用模型的图像与视频输入, 并使用 auto 或 visual 模式; 本工具不提供文字模型 OCR"
            try:
                return self._read_image(abs_path, options.start_time, options.end_time)
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

    def _read_image(self, path: Path, start_time: float = 0.0, end_time: float | None = None) -> dict[str, object]:
        """
        将已校验路径的本地图片固定为工具视觉结果

        参数:
        - path: 已通过宿主文件边界和审批的固定图片路径
        - start_time: 动画起始秒数
        - end_time: 可选结束秒数

        返回:
        - 带文件名和尺寸的 PNG 媒体结果; 动画保留原媒体供请求边界采样, 多页静态图片取第一页, 超限或无效图片抛出异常
        """
        from PIL import ImageOps, Image

        if path.stat().st_size > MAX_MEDIA_BYTES:
            raise ValueError("图片文件超过 32 MiB 限制")
        with Image.open(path) as original:
            if original.width * original.height > 40_000_000:
                raise ValueError("图片超过 4000 万像素限制")
            width, height = original.size
            first_frame = (getattr(original, "is_animated", False) is True) if original.format in {"GIF", "WEBP", "PNG"} else getattr(original, "n_frames", 1) > 1
            if first_frame and original.format in {"GIF", "WEBP", "PNG"}:
                mime = Image.MIME.get(original.format, "image/gif")
                return {MEDIA_RESULT_KEY: 1, "text": f"动画图片: {path.name}, 按模型配置采样", "media": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(_bounded_media_bytes(path)).decode("ascii"), "start_time": start_time, "end_time": end_time}}]}
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


class _MediaCore(_ToolBase):
    """只允许按当前会话持有的媒体 ID 读取, 不接受路径或任意网络地址"""

    tool_name = "read_media"
    description = "按 media_id 分段查看当前会话已收到的视频或动画; start_time/end_time 单位秒, 保持模型配置的采样间隔, 超限时缩短区间; 连续读取前先用文字记录已看区间的观察; 不读取其它会话或任意路径"
    params_dict = {
        "media_id": ("string", "当前会话媒体说明给出的 media_id"),
        "start_time": ("number", "起始秒数, 默认 0"),
        "end_time": ("number", "结束秒数; 建议按小区间读取, 不填表示结尾"),
    }
    recovery_policy = "retry"
    _session: SimpleSession | AsyncSimpleSession

    def _read_media(self, media_id: str, start_time: float, end_time: float | None) -> dict[str, Any]:
        """
        复核媒体持有权并将时间范围绑定到工具媒体结果

        参数:
        - media_id: 本会话已登记媒体的内容标识
        - start_time: 起始秒数
        - end_time: 可选结束秒数

        返回:
        - 可在当前工作流冻结的媒体结果, 无效范围或越权时抛出 ValueError
        """
        if isinstance(start_time, bool) or not isinstance(start_time, (int, float)) or not math.isfinite(start_time) or start_time < 0:
            raise ValueError("start_time 必须是非负有限数字")
        if end_time is not None and (isinstance(end_time, bool) or not isinstance(end_time, (int, float)) or not math.isfinite(end_time) or end_time <= start_time):
            raise ValueError("end_time 必须大于 start_time")
        context = self._session.ctx
        reference = REFERENCE_PREFIX + media_id
        data, mime = store_for(context.db_path, context.conversation_id).read(reference)
        if mime.startswith("video/") and getattr(self._session.llm, "supports_native_video_input", False) is True and (start_time != 0 or end_time is not None):
            raise ValueError("当前模型使用原生视频输入, 不支持按抽帧区间读取; 请直接读取完整视频")
        field = "video_url" if mime.startswith("video/") else "image_url"
        return {MEDIA_RESULT_KEY: 1, "text": f"媒体 {media_id}, 请求区间 {start_time} 至 {end_time if end_time is not None else '结尾'} 秒",
                "media": [{"type": field, field: {"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
                                                  "start_time": start_time, "end_time": end_time}}]}
