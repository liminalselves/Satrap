from __future__ import annotations

import base64
import io
import mimetypes
import os
from pathlib import Path
from typing import Any

from satrap.core.log import logger


ChatContent = str | list[dict[str, Any]]
ChatMessage = dict[str, Any]

DEFAULT_IMAGE_TOKEN_COST = 1024
DEFAULT_MAX_IMAGE_SIDE = 1600
DEFAULT_TARGET_BYTES = 4 * 1024 * 1024


def normalize_openai_base_url(base_url: str | None) -> str | None:
    """归一化 OpenAI 兼容客户端 base_url"""
    if not base_url:
        return base_url
    cleaned = base_url.strip().rstrip("/")
    suffixes = ("/chat/completions", "/completions", "/responses")
    for suffix in suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned or base_url


def is_data_image_url(value: str) -> bool:
    """判断字符串是否为图片 data URL"""
    return value.startswith("data:image/") and ";base64," in value


def is_remote_url(value: str) -> bool:
    """判断字符串是否为远程 URL"""
    return value.startswith(("http://", "https://"))


def is_image_content_part(part: Any) -> bool:
    """判断 content 片段是否为图片片段"""
    return isinstance(part, dict) and part.get("type") == "image_url"


def is_text_content_part(part: Any) -> bool:
    """判断 content 片段是否为文本片段"""
    return isinstance(part, dict) and part.get("type") == "text"


def content_text_projection(content: ChatContent | None) -> str:
    """将多模态 content 转为可读文本摘要"""
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
        else:
            ptype = part.get("type", "unknown") if isinstance(part, dict) else "unknown"
            parts.append(f"[{ptype}]")
    return " ".join(p for p in parts if p).strip()


def estimate_content_image_count(content: ChatContent | None) -> int:
    """统计 content 中的图片数量"""
    if not isinstance(content, list):
        return 0
    return sum(1 for part in content if is_image_content_part(part))


def _guess_mime_type(path: str) -> str:
    """根据路径推断图片 MIME 类型"""
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
    """将图片字节编码为 data URL"""
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def encode_local_image_to_data_url(
    image_path: str,
    max_side: int = DEFAULT_MAX_IMAGE_SIDE,
    target_bytes: int = DEFAULT_TARGET_BYTES,
) -> str:
    """将本地图片编码为可发送给视觉模型的 data URL"""
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    mime_type = _guess_mime_type(image_path)
    raw_size = os.path.getsize(image_path)
    if raw_size <= target_bytes and mime_type in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        with open(image_path, "rb") as f:
            return _image_bytes_to_data_url(f.read(), mime_type)

    try:
        from PIL import Image
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
    """构建 OpenAI 兼容图片 content 片段"""
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
    """构建文本和图片混合 content"""
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
    """归一化消息列表并把图片追加到最后一条 user 消息"""
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
