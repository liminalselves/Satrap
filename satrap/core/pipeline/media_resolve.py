"""
入站媒体解析与 OneBot 回源刷新

在唤醒与限流之后把选中进入模型的 OneBot 上报媒体解析为本地文件: 优先直连上报地址,
图片在地址过期时经实现动作 get_image 刷新后重试, 视频没有对应的回源动作只做直连;
解析结果写回组件的 resolved_path, 临时文件随事件清理, 失败只记录状态供投影降级,
下载始终经事件级出站策略, 不在通用 LLM 媒体层做平台回源
"""
from __future__ import annotations

from time import monotonic
from typing import Any, cast
import asyncio
import tempfile
import os

from satrap.core.pipeline.input_projection import MediaItem, MediaSelection
from satrap.core.platform.onebot.admin import (
    AdminActionRejected,
    PlatformAdminError,
    UnsupportedAdminAction,
)
from satrap.core.pipeline.attachments import _admin_action, _download
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.event import MessageEvent
from satrap.core.utils.media import MAX_MEDIA_BYTES

from satrap.core.log import logger

MEDIA_FETCH_TIMEOUT = 20.0
"""单条媒体下载超时秒数, 与附件下载保持一致"""
MEDIA_TOTAL_TIMEOUT = 60.0
"""每事件媒体解析总预算秒数, 超出后其余媒体直接降级不再获取"""

_IMAGE_SUFFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tiff"),
    (b"MM\x00*", ".tiff"),
    (b"\x00\x00\x01\x00", ".ico"),
)
"""图片文件头到扩展名的映射, 供落地文件推断 MIME"""
_FTYP_IMAGE_BRANDS: tuple[tuple[bytes, str], ...] = (
    (b"avif", ".avif"),
    (b"avis", ".avif"),
    (b"heic", ".heic"),
    (b"heix", ".heic"),
    (b"mif1", ".heic"),
)
"""ftyp 容器中属于图片的品牌, 需先于视频判定"""


def media_suffix(data: bytes, media_type: str) -> str:
    """
    按文件头推断落地文件扩展名

    参数:
    - data: 媒体字节
    - media_type: image 或 video

    返回:
    - str: 可直接被 MIME 推断识别的扩展名; 无法识别的图片返回空串, 由调用方判定失败
    """
    if data[:4] != b"RIFF" and data[4:8] == b"ftyp":
        brand = data[8:12]
        for candidate, suffix in _FTYP_IMAGE_BRANDS:
            if brand == candidate:
                return suffix
        return ".mp4"
    for magic, suffix in _IMAGE_SUFFIXES:
        if data.startswith(magic):
            return suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.lstrip()[:4] in {b"<svg", b"<?xm"}:
        return ".svg"
    return "" if media_type == "image" else ".mp4"


class MediaResult:
    """单条媒体的解析结果"""

    def __init__(self, source: str, media_type: str, status: str, reason: str = "", path: str = "") -> None:
        """
        初始化 MediaResult

        参数:
        - source: 选中时的来源, 同时作为与投影对齐的键
        - media_type: image 或 video
        - status: resolved 或 failed
        - reason: 失败原因或刷新说明, 供诊断码使用
        - path: 解析成功时的本地文件路径
        """
        self.source = source
        self.media_type = media_type
        self.status = status
        self.reason = reason
        self.path = path

    def __repr__(self) -> str:
        """返回不含本地路径的摘要, 避免临时路径进入日志"""
        return f"MediaResult(source={self.source!r}, media_type={self.media_type!r}, status={self.status!r}, reason={self.reason!r})"

    @property
    def kind(self) -> str:
        """诊断通道使用的类别名, 与附件结果保持同一鸭子结构"""
        return self.media_type


async def resolve_media(event: MessageEvent, selection: MediaSelection) -> tuple[MediaResult, ...]:
    """
    把选中集合中的 OneBot 上报媒体解析为本地文件

    参数:
    - event: 已通过唤醒与限流的事件
    - selection: select_media 的选中结果, 与投影使用同一份选中真相

    返回:
    - tuple[MediaResult, ...]: 与选中条目一一对应的结果;
      非 OneBot 适配器不做任何解析并返回空元组, 其他适配器继续沿用模型层的直连行为
    """
    if not selection.items or not isinstance(event.adapter, OneBotAdapter):
        return ()
    settings = event.policy_settings
    trusted = tuple(str(item) for item in cast(list[object], settings.get("media_trusted_hosts", []) or []))
    verify_tls = settings.get("media_insecure_tls", False) is not True
    allow_plaintext = settings.get("media_plaintext_http", False) is True
    deadline = monotonic() + MEDIA_TOTAL_TIMEOUT
    get_image = _admin_action(event, "get_image")
    results: list[MediaResult] = []
    for item in selection.items:
        remaining = deadline - monotonic()
        if remaining <= 0:
            results.append(MediaResult(item.source, item.media_type, "failed", _budget_reason(item.media_type)))
            continue
        try:
            results.append(await asyncio.wait_for(
                _resolve_item(event, item, get_image, trusted, verify_tls, allow_plaintext), remaining,
            ))
        except asyncio.TimeoutError:
            logger.warning(f"[media_resolve] 媒体解析超出事件预算 type={item.media_type}")
            results.append(MediaResult(item.source, item.media_type, "failed", _budget_reason(item.media_type)))
    return tuple(results)


async def _resolve_item(
    event: MessageEvent, item: MediaItem, get_image: Any,
    trusted: tuple[str, ...], verify_tls: bool, allow_plaintext: bool,
) -> MediaResult:
    """
    解析单条媒体: 直连上报地址, 图片失败后经实现动作刷新再试

    参数:
    - event: 当前事件
    - item: 选中条目
    - get_image: 适配器 admin 上的图片回源动作, 实现不提供时为 None
    - trusted / verify_tls / allow_plaintext: 出站下载参数

    返回:
    - MediaResult: 成功时 status 为 resolved, 失败时保留原因码不再重试
    """
    source = item.source
    is_http = source.lower().startswith(("http://", "https://"))

    # Step.1 直连上报地址, 地址仍有效时无需触发平台动作
    if is_http:
        try:
            return await _download_into(event, item, source, trusted, verify_tls, allow_plaintext)
        except Exception as error:
            logger.debug(f"[media_resolve] 直连失败 type={item.media_type}: {type(error).__name__}")

    # Step.2 图片经实现动作取回刷新后的地址再试, 视频无对应动作只走降级
    if item.media_type == "image" and get_image is not None:
        fresh = await _refresh_image_url(get_image, source)
        if fresh and fresh != source:
            try:
                result = await _download_into(event, item, fresh, trusted, verify_tls, allow_plaintext)
                return MediaResult(source, "image", "resolved", "image_url_refreshed", result.path)
            except Exception as error:
                logger.warning(f"[media_resolve] 刷新后下载仍失败: {type(error).__name__}")
                return MediaResult(source, "image", "failed", "image_unavailable")
        return MediaResult(source, "image", "failed", "image_unavailable")

    # Step.3 无可用回源手段: 图片与视频分别给出可区分的原因码
    if item.media_type == "image":
        return MediaResult(source, "image", "failed", "image_download_failed" if is_http else "image_unavailable")
    return MediaResult(source, "video", "failed", "video_download_failed" if is_http else "video_unsupported_by_implementation")


async def _refresh_image_url(get_image: Any, source: str) -> str:
    """
    经实现动作取回刷新后的图片地址

    参数:
    - get_image: 适配器 admin 上的图片回源动作
    - source: 上报的图片标识或地址

    返回:
    - str: 刷新后的地址; 实现不支持、图片不在缓存或响应异常时返回空串
    """
    try:
        info = cast(dict[str, Any], await get_image(source))
    except UnsupportedAdminAction:
        logger.debug("[media_resolve] 当前实现不支持 get_image")
        return ""
    except AdminActionRejected as error:
        logger.debug(f"[media_resolve] get_image 未能取回图片: {error}")
        return ""
    except PlatformAdminError as error:
        logger.warning(f"[media_resolve] get_image 失败: {type(error).__name__}")
        return ""
    except Exception as error:
        logger.warning(f"[media_resolve] get_image 异常: {type(error).__name__}")
        return ""
    if not isinstance(info, dict):
        return ""
    return str(cast(dict[str, Any], info).get("url") or "")


async def _download_into(
    event: MessageEvent, item: MediaItem, url: str,
    trusted: tuple[str, ...], verify_tls: bool, allow_plaintext: bool,
) -> MediaResult:
    """
    下载单条媒体并落地为本地文件

    参数:
    - event: 当前事件, 提供临时文件登记
    - item: 选中条目, 成功后写回 resolved_path
    - url: 待下载地址
    - trusted / verify_tls / allow_plaintext: 出站下载参数

    返回:
    - MediaResult: 成功结果; 地址不安全、超限或响应异常时抛出, 由调用方降级
    """
    data = await _download(url, MAX_MEDIA_BYTES, trusted, verify_tls, allow_plaintext)
    suffix = media_suffix(data, item.media_type)
    if item.media_type == "image" and not suffix:
        raise ValueError("无法识别图片格式")
    path = _write_temporary(event, data, suffix)
    for component in item.components:
        # 同一来源的组件必须一起写回, 否则投影按来源去重时会出现一个已解析一个仍取原地址
        setattr(component, "resolved_path", path)
    return MediaResult(item.source, item.media_type, "resolved", "", path)


def _write_temporary(event: MessageEvent, data: bytes, suffix: str) -> str:
    """
    写入临时文件并登记清理

    参数:
    - event: 当前事件
    - data: 媒体字节
    - suffix: 已确认的扩展名

    返回:
    - str: 临时文件路径; 写入失败时文件已在内部清理
    """
    handle = tempfile.NamedTemporaryFile(prefix="satrap-media-", suffix=suffix, delete=False)
    path = handle.name
    try:
        try:
            handle.write(data)
        finally:
            handle.close()
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    event.track_temporary_local_file(path)
    return path


def _budget_reason(media_type: str) -> str:
    """
    取媒体类型对应的预算耗尽原因码

    参数:
    - media_type: image 或 video

    返回:
    - str: 该类型的预算耗尽原因码
    """
    return "image_media_budget_dropped" if media_type == "image" else "video_media_budget_dropped"
