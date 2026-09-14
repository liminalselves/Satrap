"""多模态图片输入的解析, 下载与格式转换工具"""
from __future__ import annotations

import mimetypes
from pathlib import Path
import base64
from typing import Any, cast
import io
import os

from satrap.core.log import logger


ChatContent = str | list[dict[str, Any]]
ChatMessage = dict[str, Any]

DEFAULT_IMAGE_TOKEN_COST = 1024
DEFAULT_MAX_IMAGE_SIDE = 1600
DEFAULT_TARGET_BYTES = 4 * 1024 * 1024


def is_data_image_url(value: str) -> bool:
    """
    判断字符串是否为图片 data URL

    参数:
    - value: 输入值

    返回:
    - bool: 判断字符串是否为图片 data URL
    """
    return value.startswith("data:image/") and ";base64," in value


def is_remote_url(value: str) -> bool:
    """
    判断字符串是否为远程 URL

    参数:
    - value: 输入值

    返回:
    - bool: 判断字符串是否为远程 URL
    """
    return value.startswith(("http://", "https://"))


def is_image_content_part(part: object) -> bool:
    """
    判断 content 片段是否为图片片段

    参数:
    - part: part 输入值, 允许任意对象, 非字典返回 False

    返回:
    - bool: 判断 content 片段是否为图片片段
    """
    if not isinstance(part, dict):
        return False
    part_dict = cast(dict[object, object], part)
    return bool(part_dict.get("type") == "image_url")


def is_text_content_part(part: object) -> bool:
    """
    判断 content 片段是否为文本片段

    参数:
    - part: part 输入值, 允许任意对象, 非字典返回 False

    返回:
    - bool: 判断 content 片段是否为文本片段
    """
    if not isinstance(part, dict):
        return False
    part_dict = cast(dict[object, object], part)
    return bool(part_dict.get("type") == "text")


def content_text_projection(content: ChatContent | None) -> str:
    """
    将多模态 content 转为可读文本摘要

    参数:
    - content: 内容

    返回:
    - str: 将多模态 content 转为可读文本摘要
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if is_text_content_part(part):
            parts.append(str(part.get("text", "")))
        elif is_image_content_part(part):
            parts.append("[图片]")
        elif isinstance(part, dict) and part.get("type") == "video_url":
            parts.append("[视频]")
        else:
            ptype = part.get("type", "unknown") if isinstance(part, dict) else "unknown"
            parts.append(f"[{ptype}]")
    return " ".join(p for p in parts if p).strip()


def estimate_content_image_count(content: ChatContent | None) -> int:
    """
    统计 content 中的图片数量

    参数:
    - content: 内容

    返回:
    - int: 统计 content 中的图片数量
    """
    if not isinstance(content, list):
        return 0
    return sum(1 for part in content if is_image_content_part(part))


def estimate_content_media_tokens(content: ChatContent | None) -> int:
    """
    估算媒体占用的上下文预算, 不对 Base64 文本进行分词

    参数:
    - content: 消息内容

    返回:
    - 图片按既有固定成本估算, 视频暂按八张图片预留; 实际成本由模型 usage 校准
    """
    videos = sum(part.get("type") == "video_url" for part in content) if isinstance(content, list) else 0
    return (estimate_content_image_count(content) + videos * 8) * DEFAULT_IMAGE_TOKEN_COST


def _guess_mime_type(path: str) -> str:
    """
    根据路径推断图片 MIME 类型

    参数:
    - path: 路径

    返回:
    - str: 根据路径推断图片 MIME 类型
    """
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type and mime_type.startswith("image/"):
        return mime_type
    ext = Path(path).suffix.lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    if ext in mapping:
        return mapping[ext]
    raise ValueError(f"不支持的图片格式: {ext}")


def _image_bytes_to_data_url(image_bytes: bytes, mime_type: str) -> str:
    """
    将图片字节编码为 data URL

    参数:
    - image_bytes: 图片字节数据
    - mime_type: mime类型

    返回:
    - str: 将图片字节编码为 data URL
    """
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def encode_local_image_to_data_url(
    image_path: str,
    max_side: int = DEFAULT_MAX_IMAGE_SIDE,
    target_bytes: int = DEFAULT_TARGET_BYTES,
) -> str:
    """
    将本地图片编码为可发送给视觉模型的 data URL

    参数:
    - image_path: image路径
    - max_side: 最大side
    - target_bytes: 目标bytes

    返回:
    - str: 将本地图片编码为可发送给视觉模型的 data URL
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    mime_type = _guess_mime_type(image_path)
    raw_size = os.path.getsize(image_path)
    if raw_size <= target_bytes and mime_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        with open(image_path, "rb") as f:
            return _image_bytes_to_data_url(f.read(), mime_type)

    try:
        from PIL import Image   # 仅在读取图片尺寸时加载可选图像依赖
    except ImportError as exc:
        raise RuntimeError("处理大图需要安装 pillow") from exc

    with Image.open(image_path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        quality = 85
        while quality >= 45:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= target_bytes or quality == 45:
                return _image_bytes_to_data_url(data, "image/jpeg")
            quality -= 10

    raise RuntimeError(f"图片压缩失败: {image_path}")


def build_image_content_parts(
    img_urls: list[str] | None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """
    构建 OpenAI 兼容图片 content 片段

    参数:
    - img_urls: 图片 URL 列表
    - strict: 是否使用严格模式

    返回:
    - list[dict[str, Any]]: 构建 OpenAI 兼容图片 content 片段
    """
    if not img_urls:
        return []

    parts: list[dict[str, Any]] = []
    for img_url in img_urls:
        try:
            if is_remote_url(img_url) or is_data_image_url(img_url):
                url = img_url
            else:
                url = encode_local_image_to_data_url(img_url)
            parts.append({"type": "image_url", "image_url": {"url": url}})
        except Exception as exc:
            if strict:
                raise
            logger.warning(f"[图像处理] 跳过无效图片: {exc}")
    return parts


def build_multimodal_content(
    text: ChatContent,
    img_urls: list[str] | None = None,
    strict: bool = False,
) -> ChatContent:
    """
    构建文本和图片混合 content

    参数:
    - text: 待处理文本
    - img_urls: 图片 URL 列表
    - strict: 是否使用严格模式

    返回:
    - ChatContent: 构建文本和图片混合 content
    """
    image_parts = build_image_content_parts(img_urls, strict=strict)
    if not image_parts:
        return text

    if isinstance(text, list):
        return [dict(part) for part in text] + image_parts
    return [{"type": "text", "text": text}] + image_parts


def normalize_chat_messages(
    messages: list[ChatMessage],
    img_urls: list[str] | None = None,
    strict: bool = False,
) -> list[ChatMessage]:
    """
    归一化消息列表并把图片追加到最后一条 user 消息

    参数:
    - messages: 消息列表
    - img_urls: 图片 URL 列表
    - strict: 是否使用严格模式

    返回:
    - list[ChatMessage]: 归一化消息列表并把图片追加到最后一条 user 消息
    """
    normalized = [dict(message) for message in messages]
    if not img_urls:
        return normalized

    for index in range(len(normalized) - 1, -1, -1):
        if normalized[index].get("role") == "user":
            original_content = normalized[index].get("content", "")
            normalized[index]["content"] = build_multimodal_content(
                original_content,
                img_urls=img_urls,
                strict=strict,
            )
            break
    return normalized
