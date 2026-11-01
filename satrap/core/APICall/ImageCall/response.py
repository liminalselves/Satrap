"""统一解析图片 JSON, ZIP, 二进制与 SSE, 不依赖入参协议"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from io import BytesIO
import json
import re
from zipfile import BadZipFile, ZipFile
from typing import Any

from .media import MAX_IMAGE_BYTES, MAX_IMAGES, MAX_RESPONSE_BYTES, decode_image
from .types import GeneratedImage, ImageGenError, ImageGenResponse


@dataclass
class ParsedResponse:
    """待下载 URL 与内存图片共同使用的中间结果, 保留输出顺序"""

    entries: list[GeneratedImage | tuple[str, int | None, str | None]] = field(default_factory=list)
    model: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] | None = None
    text: list[str] = field(default_factory=list)

    def finish(self, images: list[GeneratedImage], model: str, request_id: str | None) -> ImageGenResponse:
        """下载完成后构造统一结果, 不保留原始请求响应正文"""
        if not images:
            raise ImageGenError("no_images", request_id=request_id)
        return ImageGenResponse(images=images, model=self.model or model, request_id=self.request_id or request_id,
                                usage=self.usage, text="\n".join(self.text))


def safe_request_id(value: object) -> str | None:
    """仅接收有限长度的请求 ID, 不用于记录上游任意正文"""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else None


def decode_base64(value: str) -> bytes:
    """解码有限大小的 Base64 或 Data URL"""
    if value.startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or ";base64" not in header:
            raise ImageGenError("invalid_base64")
    if len(value) > (MAX_IMAGE_BYTES * 4 // 3 + 8):
        raise ImageGenError("image_size_limit")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ImageGenError("invalid_base64") from None


def _parse_json(payload: object, parsed: ParsedResponse) -> None:
    """识别明确的图片字段, 不递归搜集任意字符串或 URL"""
    if isinstance(payload, list):
        for item in payload:
            _image_entry(item, parsed)
        return
    if not isinstance(payload, dict):
        raise ImageGenError("invalid_response")
    status = payload.get("status") or payload.get("task_status")
    if payload.get("error") or payload.get("statusCode", 200) not in (200, 201):
        raise ImageGenError("provider_error")
    if str(status).lower() in {"failed", "error", "canceled", "cancelled"}:
        raise ImageGenError("provider_error")
    output = payload.get("output")
    if isinstance(output, dict) and (output.get("task_id") or output.get("task_status")):
        raise ImageGenError("async_task_response")
    if payload.get("task_id") or str(status).lower() in {"pending", "running", "processing", "queued", "starting"}:
        raise ImageGenError("async_task_response")
    if isinstance(payload.get("model"), str):
        parsed.model = payload["model"]
    parsed.request_id = safe_request_id(payload.get("request_id")) or parsed.request_id
    if isinstance(payload.get("usage"), dict):
        parsed.usage = payload["usage"]
    for key in ("data", "images"):
        entries = payload.get(key)
        if isinstance(entries, list):
            for item in entries:
                _image_entry(item, parsed, payload.get("seed"))
            return
    if isinstance(output, dict):
        _parse_json(output, parsed)
        return
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("sample"), str):
        _image_entry({"url": result["sample"]}, parsed)
        return
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            for part in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parsed.text.append(part["text"])
                else:
                    _image_entry(part, parsed)
        return
    _image_entry(payload, parsed)


def _image_entry(item: object, parsed: ParsedResponse, fallback_seed: object = None) -> None:
    """读取单个已知输出条目, 同一条目只采用一个图片来源"""
    if isinstance(item, str):
        item = {"url": item} if item.startswith(("https://", "http://")) else {"image": item}
    if not isinstance(item, dict):
        return
    if item.get("error"):
        raise ImageGenError("provider_error")
    seed = item.get("seed", fallback_seed)
    seed = seed if type(seed) is int else None
    revised_prompt = item.get("revised_prompt")
    revised_prompt = revised_prompt if isinstance(revised_prompt, str) else None
    encoded = item.get("b64_json") or item.get("base64")
    image = item.get("image")
    if not encoded and isinstance(image, str) and not image.startswith(("https://", "http://")):
        encoded = image
    if isinstance(encoded, str):
        parsed.entries.append(decode_image(decode_base64(encoded), seed=seed, revised_prompt=revised_prompt))
    else:
        url = item.get("url") or item.get("image_url") or image
        if isinstance(url, dict):
            url = url.get("url")
        if isinstance(url, str):
            if url.startswith("data:"):
                parsed.entries.append(decode_image(decode_base64(url), seed=seed, revised_prompt=revised_prompt))
            elif url.startswith(("https://", "http://")):
                parsed.entries.append((url, seed, revised_prompt))
    if len(parsed.entries) > MAX_IMAGES:
        raise ImageGenError("image_count_limit")


def parse_response(data: bytes, content_type: str) -> ParsedResponse:
    """
    根据内容和已知结构统一解析生成结果

    参数:
    - data: 受大小限制的响应字节
    - content_type: 上游 Content-Type

    返回:
    - 保留图片顺序和可选元数据的中间结果
    """
    if len(data) > MAX_RESPONSE_BYTES:
        raise ImageGenError("response_size_limit")
    parsed = ParsedResponse()
    if data.startswith(b"PK\x03\x04") or "zip" in content_type:
        try:
            with ZipFile(BytesIO(data)) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir() and entry.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
                if len(entries) > MAX_IMAGES or sum(entry.file_size for entry in entries) > MAX_RESPONSE_BYTES:
                    raise ImageGenError("response_size_limit")
                for entry in entries:
                    if entry.file_size > MAX_IMAGE_BYTES:
                        raise ImageGenError("image_size_limit")
                    parsed.entries.append(decode_image(archive.read(entry)))
        except ImageGenError:
            raise
        except (BadZipFile, RuntimeError, OSError):
            raise ImageGenError("invalid_zip") from None
    elif "text/event-stream" in content_type or data.lstrip().startswith((b"data:", b"event:")):
        try:
            events = data.decode("utf-8").replace("\r\n", "\n").split("\n\n")
            for event in events:
                lines = event.splitlines()
                kind = next((line[6:].strip() for line in lines if line.startswith("event:")), "")
                text = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
                if not text or text == "[DONE]":
                    continue
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    continue
                kind = str(payload.get("type", kind))
                if "error" in kind.lower() or payload.get("error"):
                    raise ImageGenError("provider_error")
                if kind.lower() in {"final", "image_generation.completed", "image_edit.completed"} or kind.lower().endswith(".completed"):
                    _parse_json(payload, parsed)
                elif isinstance(payload.get("usage"), dict):
                    parsed.usage = payload["usage"]
        except (UnicodeError, json.JSONDecodeError):
            raise ImageGenError("invalid_sse") from None
    elif "json" in content_type or data.lstrip().startswith((b"{", b"[")):
        try:
            _parse_json(json.loads(data), parsed)
        except (UnicodeError, json.JSONDecodeError):
            raise ImageGenError("invalid_json") from None
    else:
        parsed.entries.append(decode_image(data))
    if not parsed.entries:
        raise ImageGenError("no_images")
    return parsed
