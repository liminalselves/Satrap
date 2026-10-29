"""
模型视觉输入与工具媒体结果

固定本地及远程媒体内容, 共用能力检查和消息转换,
工具图片在历史中归属工具消息, 仅在请求边界展开为补充用户消息
"""
from __future__ import annotations

from urllib.parse import urlsplit, parse_qs, urlencode
from typing import Any
from pathlib import Path
import mimetypes
import hashlib
import base64
import copy
import io

from satrap.core.utils.media_store import REFERENCE_PREFIX, current_store, media_scope
from satrap.core.utils.media_frames import MediaRangeRequired, sample_frames

from satrap.core.utils.outbound import safe_sync_get
from satrap.core.utils.vision import content_text_projection

MAX_MEDIA_BYTES = 32 * 1024 * 1024
MAX_MEDIA_ITEMS = 16
MEDIA_RESULT_KEY = "satrap_media_result"


def sniff_media_mime(data: bytes, declared: str) -> str:
    """
    以文件头确认常见媒体 MIME, 不需要解码动画或读取完整文件

    参数:
    - data: 文件头或有界原始媒体字节
    - declared: 来源 MIME, 用于没有专用文件头映射的静态格式

    返回:
    - 实际 MIME, 未识别格式保留来源声明
    """
    mime = declared
    if data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        mime = "video/x-msvideo"
    elif data[4:8] == b"ftyp" and data[8:12] not in {b"avif", b"avis", b"heic", b"heix", b"mif1"}:
        mime = "video/mp4"
    elif data.startswith(b"\x1aE\xdf\xa3"):
        mime = "video/webm"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    return mime


def inspect_media(data: bytes, declared: str) -> tuple[str, str, bool]:
    """
    确认实际媒体类别和动画状态, 不把平台标签误当成最终格式

    参数:
    - data: 有界原媒体字节
    - declared: 来源 MIME

    返回:
    - 实际 MIME, image/video 类别和动画状态
    """
    mime = sniff_media_mime(data, declared)
    animated = False
    if mime in {"image/gif", "image/webp", "image/png"} and data.startswith((b"GIF", b"RIFF", b"\x89PNG")):
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("图片超过 4000 万像素限制")
            animated = getattr(image, "is_animated", False) is True
    kind = "video" if mime.startswith("video/") else "image" if mime.startswith("image/") else "unknown"
    return mime, kind, animated


def media_size(part: dict[str, Any]) -> int:
    """
    根据真实内容计算来源大小, 内部引用不能绕过原文件预算

    参数:
    - part: 已冻结的单个媒体片段

    返回:
    - 原媒体字节数
    """
    url = part[part["type"]]["url"]
    return len(current_store().read(url)[0]) if url.startswith(REFERENCE_PREFIX) else len(base64.b64decode(url.partition(",")[2]))


def project_parts(parts: list[dict[str, Any]], model: object) -> list[dict[str, Any]]:
    """
    按模型能力生成媒体请求副本, 原生视频与兼容抽帧不重复发送

    参数:
    - parts: 原消息内容片段
    - model: 当前模型的明确能力和采样参数

    返回:
    - 标准文本, image_url 和 video_url 片段, 原列表不变
    """
    result: list[dict[str, Any]] = []
    for part in parts:
        field = part.get("type")
        if field not in {"image_url", "video_url"}:
            result.append(copy.deepcopy(part))
            continue
        source = part.get("_satrap_media_reference", part[field]["url"])
        if not visual_enabled(model) and getattr(model, "supports_native_video_input", False) is not True:
            result.append({"type": "text", "text": "[视频]" if field == "video_url" else "[图片]"})
            continue
        if source.startswith(REFERENCE_PREFIX):
            data, mime = current_store().read(source)
        elif source.startswith("data:"):
            header, _, encoded = source.partition(",")
            data, mime = base64.b64decode(encoded, validate=True), header[5:-7]
        else:
            if field == "image_url" and Path(urlsplit(source).path).suffix.lower() not in {".gif", ".webp"}:
                result.append(copy.deepcopy(part))
                continue
            frozen = freeze_media(source, field.removesuffix("_url"))
            result.extend(project_parts([frozen], model))
            continue
        mime, kind, animated = inspect_media(data, mime)
        if kind == "image" and not visual_enabled(model):
            result.append({"type": "text", "text": "[图片]"})
            continue
        if kind == "video" and getattr(model, "supports_native_video_input", False) is True:
            if getattr(model, "native_video_transport", "data_url") == "url":
                if not source.startswith(REFERENCE_PREFIX):
                    raise ValueError("当前原生视频要求公网 URL, 本地媒体没有公网来源; 请改用 Data URL 或抽帧方式")
                url = current_store().public_url(source)
                result.append({"type": "video_url", "video_url": {"url": url}, "_satrap_media_reference": source})
                # 请求副本保留已验证的引用, 避免工作流与 LLM 两次投影时重复下载
            else:
                result.append({"type": "video_url", "video_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}})
        elif kind == "video" or animated:
            if not visual_enabled(model):
                result.append({"type": "text", "text": "[视频, 当前模型不支持图片兼容输入]"})
                continue
            query = parse_qs(urlsplit(source).query) if source.startswith(REFERENCE_PREFIX) else {}
            start = float(query.get("start", ["0"])[0])
            end = float(query["end"][0]) if "end" in query else None
            try:
                sampled = sample_frames(data, "video" if kind == "video" else "animation", getattr(model, "video_frame_interval", 2.0), start, end)
            except MediaRangeRequired as error:
                reference = current_store().put(data, mime)
                result.append({"type": "text", "text": f"[媒体尚未读取画面] media_id={reference.removeprefix(REFERENCE_PREFIX)}; {error}; 若 read_media 可用, 按 start_time/end_time 分段读取; 否则启用 base_take 或提供短片段"})
                continue
            label = "视频" if kind == "video" else "动画图片"
            source_id = hashlib.sha256(data).hexdigest()[:12]
            result.append({"type": "text", "text": f"{label} media_id={hashlib.sha256(data).hexdigest()}: 总时长 {sampled['duration']:.2f} 秒, 本次 {sampled['start']:.2f} 至 {sampled['end']:.2f} 秒, 间隔 {sampled['interval']} 秒, {len(sampled['frames'])} 帧; 仅含采样画面, 未处理音轨"})
            for index, frame in enumerate(sampled["frames"], 1):
                result.extend([
                    {"type": "text", "text": f"{source_id} 第 {index} 帧, 时间 {frame['time']:.3f} 秒"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(frame["data"]).decode("ascii")}},
                ])
        else:
            result.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}})
    return result


def project_context_media(messages: list[dict[str, Any]], model: object, database: str, scope: str) -> list[dict[str, Any]]:
    """
    在预算计算前按上下文实际存储绑定并展开媒体

    参数:
    - messages: 历史或待提交消息副本
    - model: 本次模型
    - database: 上下文数据库路径
    - scope: 工作流标识

    返回:
    - 可用于 token 估算和模型请求的媒体副本
    """
    with media_scope(database, scope):
        return project_messages(messages, visual_enabled(model), model)


def persist_request_media(messages: list[dict[str, Any]], database: str, scope: str) -> list[dict[str, Any]]:
    """
    将执行恢复记录中的内联媒体替换为去重引用, 不重复持久化 Base64

    参数:
    - messages: 已准备的真实模型请求
    - database: 执行记录数据库
    - scope: 执行工作流标识

    返回:
    - 仅替换媒体来源的请求副本, 原请求保持不变
    """
    result = copy.deepcopy(messages)
    with media_scope(database, scope):
        for message in result:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                field = part.get("type")
                if field not in {"image_url", "video_url"}:
                    continue
                url = part[field]["url"]
                if url.startswith("data:"):
                    header, _, encoded = url.partition(",")
                    data = base64.b64decode(encoded, validate=True)
                    part[field]["url"] = current_store().put(data, header[5:-7])
    return result


def limit_history_media(history: list[dict[str, Any]], pending: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    保留最新完整媒体组, 旧画面以明确占位替代, 不裁掉新输入的一部分采样

    参数:
    - history: 已展开的历史请求副本
    - pending: 已展开的本轮临时消息, 工具循环中旧结果已经向模型展示

    返回:
    - 满足单轮媒体数量和字节预算的两个副本; 新媒体组本身超限时明确失败
    """
    combined = history + pending
    newest_tool = max((index for index, message in enumerate(combined) if index >= len(history) and message.get("role") == "tool"), default=-1)
    image_count = media_bytes = 0
    result: list[dict[str, Any]] = []
    for index in range(len(combined) - 1, -1, -1):
        message = copy.deepcopy(combined[index])
        content = message.get("content")
        if isinstance(content, list):
            media = [part for part in content if part.get("type") in {"image_url", "video_url"}]
            images = sum(part["type"] == "image_url" for part in media)
            size = sum(len(part[part["type"]]["url"]) for part in media)
            if image_count + images > MAX_MEDIA_ITEMS or media_bytes + size > MAX_MEDIA_BYTES * 4 // 3 + 4096:
                if index >= len(history) and (newest_tool < 0 or index >= newest_tool):
                    raise ValueError("本轮新读取的媒体超过 16 张图片或 32 MiB, 请减少附件或缩短读取区间")
                message["content"] = [part for part in content if part.get("type") not in {"image_url", "video_url"}] + [
                    {"type": "text", "text": "[此处历史画面本轮未附加: 媒体预算优先保留较新的完整区间; 请依据已有文字观察, 需要时按 media_id 重新读取]"},
                ]
            else:
                image_count += images
                media_bytes += size
        result.append(message)
    result.reverse()
    return result[:len(history)], result[len(history):]


def visual_enabled(model: object) -> bool:
    """
    读取模型显式声明的视觉输入能力

    参数:
    - model: 当前使用的模型实例

    返回:
    - 仅属性值为 True 时启用图片输入
    """
    return getattr(model, "supports_visual_input", False) is True


def freeze_media(source: str, kind: str) -> dict[str, Any]:
    """
    将单个媒体来源固定为模型可读取的 Data URL

    参数:
    - source: 本地路径, HTTP 地址, Base64 Data URL 或当前工作流的媒体引用
    - kind: image 或 video

    返回:
    - 静态图片的 Data URL 或媒体内部引用, 类型按实际内容确认; 无效来源和超限输入抛出 ValueError
    """
    if kind not in {"image", "video"} or not isinstance(source, str) or not source:
        raise ValueError("媒体类型或来源无效")
    source_url = None
    if source.startswith(REFERENCE_PREFIX):
        data, mime = current_store().read(source)
        actual = "video" if mime.startswith("video/") else "image"
        field = actual + "_url"
        return {"type": field, field: {"url": source}}
    if source.startswith("data:"):
        if len(source) > MAX_MEDIA_BYTES * 4 // 3 + 1024:
            raise ValueError("媒体内容超过 32 MiB 限制")
        header, separator, encoded = source.partition(",")
        if not separator or not header.endswith(";base64"):
            raise ValueError("媒体必须使用 Base64 Data URL")
        mime = header[5:-7]
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ValueError("媒体 Base64 编码无效") from exc
    elif source.startswith(("http://", "https://")):
        response = safe_sync_get(source, max_response_bytes=MAX_MEDIA_BYTES)
        response.raise_for_status()
        source_url = response.url
        data = response.content
        mime = next((value.split(";", 1)[0].strip() for key, value in response.headers.items()
                     if key.lower() == "content-type"), "")
        if not mime.startswith(kind + "/"):
            mime = mimetypes.guess_type(urlsplit(response.url).path)[0] or ""
    else:
        path = Path(source)
        with path.open("rb") as file:
            data = file.read(MAX_MEDIA_BYTES + 1)
        mime = mimetypes.guess_type(path.name)[0] or ""
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise ValueError("媒体为空或超过 32 MiB 限制")
    mime, kind, animated = inspect_media(data, mime)
    if kind not in {"image", "video"}:
        raise ValueError("无法识别媒体格式")
    if animated or kind == "video":
        field = kind + "_url"
        return {"type": field, field: {"url": current_store().put(data, mime, source_url=source_url)}}
    url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    field = f"{kind}_url"
    return {"type": field, field: {"url": url}}


def user_media_content(text: str, images: list[str] | None, videos: list[str] | None, model: object) -> str | list[dict[str, Any]]:
    """
    构造携带固定媒体内容的用户消息

    参数:
    - text: 用户文本
    - images: 图片来源, None 表示无图片
    - videos: 视频来源, None 表示无视频
    - model: 当前模型, 新媒体输入要求启用视觉能力

    返回:
    - 纯文本或多模态内容, 不支持视觉或输入超限时明确失败
    """
    sources = [(source, "image") for source in images or []] + [(source, "video") for source in videos or []]
    if not sources:
        return text
    if len(sources) > MAX_MEDIA_ITEMS:
        raise ValueError("单次媒体输入不能超过 16 项")
    parts = [freeze_media(source, kind) for source, kind in sources]
    for part in parts:
        video = part["type"] == "video_url"
        if not visual_enabled(model) and not (video and getattr(model, "supports_native_video_input", False) is True):
            raise ValueError("当前模型未启用相应的图片或视频输入能力")
    if sum(media_size(part) for part in parts) > MAX_MEDIA_BYTES:
        raise ValueError("单次媒体输入总量超过 32 MiB 限制")
    return [{"type": "text", "text": text}, *parts]


def project_messages(messages: list[dict[str, Any]], enabled: bool, model: object | None = None) -> list[dict[str, Any]]:
    """
    为当前模型创建独立请求消息副本

    参数:
    - messages: 持久化或待发送的消息
    - enabled: 当前模型是否启用图片输入
    - model: 实际模型, 提供时按其能力展开动画与视频

    返回:
    - 视觉模型保留媒体, 文本模型使用文字占位, 不修改历史
    """
    result = copy.deepcopy(messages)
    if model is not None:
        for message in result:
            if isinstance(message.get("content"), list):
                message["content"] = project_parts(message["content"], model)
    elif not enabled:
        for message in result:
            if isinstance(message.get("content"), list):
                message["content"] = content_text_projection(message["content"])
    return result


def freeze_tool_result(result: Any, model: object) -> Any:
    """
    在工具步骤落库前固定媒体, 普通工具结果保持原契约

    参数:
    - result: 工具动态返回值, 仅处理带媒体标识的字典
    - model: 当前模型, 媒体结果要求启用视觉能力

    返回:
    - 固定来源后的工具结果副本, 无效媒体和超限结果抛出 ValueError
    """
    if not isinstance(result, dict) or result.get(MEDIA_RESULT_KEY) != 1:
        return result
    media = result.get("media")
    if not isinstance(media, list):
        raise ValueError("工具媒体结果必须包含 media 列表")
    parts: list[dict[str, Any]] = []
    count = size = 0
    for part in media:
        if not isinstance(part, dict):
            raise ValueError("工具媒体内容片段无效")
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append({"type": "text", "text": part["text"]})
            continue
        if kind not in {"image_url", "video_url"} or not isinstance(part.get(kind), dict):
            raise ValueError("工具媒体类型无效")
        count += 1
        if count > MAX_MEDIA_ITEMS:
            raise ValueError("单次媒体输入不能超过 16 项")
        frozen = freeze_media(part[kind].get("url"), kind.removesuffix("_url"))
        actual = frozen["type"]
        url = frozen[actual]["url"]
        if url.startswith(REFERENCE_PREFIX):
            bounds = {key: part[kind][name] for key, name in (("start", "start_time"), ("end", "end_time")) if part[kind].get(name) is not None}
            if bounds:
                frozen[actual]["url"] = url.split("?", 1)[0] + "?" + urlencode(bounds)
        if not visual_enabled(model) and not (actual == "video_url" and getattr(model, "supports_native_video_input", False) is True):
            raise ValueError("当前模型未启用相应的图片或视频输入能力")
        size += media_size(frozen)
        if size > MAX_MEDIA_BYTES:
            raise ValueError("工具媒体总量超过 32 MiB 限制")
        parts.append(frozen)
    return {**result, "media": parts}


def expand_tool_media(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    将工具媒体转换为兼容接口的补充用户消息

    参数:
    - messages: 已完成能力检查的请求消息, 不修改原始历史

    返回:
    - 每组工具回复结束后追加关联图片, 保证工具调用及回复连续配对
    """
    result: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    for original in messages:
        if original.get("role") != "tool" and attachments:
            result.extend(attachments)
            attachments = []
        message = copy.deepcopy(original)
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, list):
            message["content"] = content_text_projection(content)
            if any(part.get("type") in {"image_url", "video_url"} for part in content):
                attachments.append({"role": "user", "content": [
                    {"type": "text", "text": f"以下内容来自工具 {message.get('tool_call_id', '')}, 属于工具结果"},
                    *content,
                ]})
        result.append(message)
    result.extend(attachments)
    return result
