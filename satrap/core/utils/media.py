"""
模型视觉输入与工具媒体结果

固定本地及远程媒体内容, 共用能力检查和消息转换,
工具图片在历史中归属工具消息, 仅在请求边界展开为补充用户消息
"""
from __future__ import annotations

from urllib.parse import urlsplit
from typing import Any
from pathlib import Path
import mimetypes
import base64
import copy

from satrap.core.utils.outbound import safe_sync_get
from satrap.core.utils.vision import content_text_projection

MAX_MEDIA_BYTES = 32 * 1024 * 1024
MAX_MEDIA_ITEMS = 16
MEDIA_RESULT_KEY = "satrap_media_result"


def visual_enabled(model: object) -> bool:
    """
    读取模型显式声明的视觉输入能力

    参数:
    - model: 当前使用的模型实例

    返回:
    - 仅属性值为 True 时启用图像与视频输入
    """
    return getattr(model, "supports_visual_input", False) is True


def freeze_media(source: str, kind: str) -> dict[str, Any]:
    """
    将单个媒体来源固定为模型可读取的 Data URL

    参数:
    - source: 本地路径, HTTP 地址或 Base64 Data URL
    - kind: image 或 video

    返回:
    - image_url 或 video_url 内容片段, 无效来源和超限输入抛出 ValueError
    """
    if kind not in {"image", "video"} or not isinstance(source, str) or not source:
        raise ValueError("媒体类型或来源无效")
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
    if not mime.startswith(kind + "/"):
        raise ValueError(f"媒体格式与 {kind} 类型不符")
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
    if not visual_enabled(model):
        raise ValueError("当前模型未启用图像与视频输入")
    if len(sources) > MAX_MEDIA_ITEMS:
        raise ValueError("单次媒体输入不能超过 16 项")
    parts = [freeze_media(source, kind) for source, kind in sources]
    if sum(len(part[part['type']]['url']) for part in parts) > MAX_MEDIA_BYTES * 4 // 3 + 4096:
        raise ValueError("单次媒体输入总量超过 32 MiB 限制")
    return [{"type": "text", "text": text}, *parts]


def project_messages(messages: list[dict[str, Any]], enabled: bool) -> list[dict[str, Any]]:
    """
    为当前模型创建独立请求消息副本

    参数:
    - messages: 持久化或待发送的消息
    - enabled: 当前模型是否启用视觉输入

    返回:
    - 视觉模型保留媒体, 文本模型使用文字占位, 不修改历史
    """
    result = copy.deepcopy(messages)
    if not enabled:
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
    if not visual_enabled(model):
        raise ValueError("当前模型未启用图像与视频输入")
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
        size += len(frozen[kind]["url"])
        if size > MAX_MEDIA_BYTES * 4 // 3 + 4096:
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
