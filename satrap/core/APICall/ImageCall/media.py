"""图片输入验证, 遮罩转换和内存编码"""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from satrap.core.utils import _is_loopback_api_host
from .types import GeneratedImage, ImageGenError, ImageInput

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGES = 32


def validate_url(url: str, *, allow_insecure: bool = False) -> str:
    """
    检查完整地址且保留原字符串, 不拼接或裁剪路径

    参数:
    - url: 完整请求地址
    - allow_insecure: 是否允许非回环 HTTP 地址

    返回:
    - 验证后的原始地址
    """
    if not isinstance(url, str) or not url or url != url.strip():
        raise ValueError("请求地址不能为空或包含首尾空白")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("请求地址必须是完整 HTTP 或 HTTPS URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("请求地址不能包含用户信息或片段")
    if parsed.scheme == "http" and not allow_insecure and not _is_loopback_api_host(parsed.hostname):
        raise ValueError("非回环 HTTP 地址需要 allow_insecure_base_url=True")
    return url


def decode_image(data: bytes, *, seed: int | None = None, revised_prompt: str | None = None) -> GeneratedImage:
    """
    验证真实图片内容并提取尺寸, 不重新编码生成图片

    参数:
    - data: 图片字节
    - seed: 上游可选随机种子
    - revised_prompt: 上游可选改写提示词

    返回:
    - 已验证的图片结果
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageGenError("image_size_limit")
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ImageGenError("image_pixel_limit")
            mime = Image.MIME.get(image.format or "", "")
            if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"}:
                raise ImageGenError("unsupported_image_format")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ImageGenError("invalid_image") from None
    return GeneratedImage(data=data, mime_type=mime, width=width, height=height, seed=seed, revised_prompt=revised_prompt)


def local_image(source: ImageInput) -> bytes:
    """读取本地输入, URL 必须由客户端显式下载"""
    if not isinstance(source, ImageInput):
        raise ValueError("图片必须通过 ImageInput 显式指定来源")
    if source.kind == "path":
        if not isinstance(source.value, (str, Path)):
            raise ValueError("路径图片必须提供本地文件路径")
        path = Path(source.value)
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("图片文件不存在或超过大小上限")
        data = path.read_bytes()
    elif source.kind == "bytes" and isinstance(source.value, bytes):
        data = source.value
    else:
        raise ValueError("图片来源类型不正确")
    try:
        decoded = decode_image(data)
    except ImageGenError:
        raise ValueError("输入不是有效图片或超过图片大小限制") from None
    if source.mime_type is not None and source.mime_type != decoded.mime_type:
        raise ValueError("声明的图片 MIME 与实际内容不符")
    return data


def b64(data: bytes) -> str:
    """将二进制编码为请求使用的 Base64"""
    return base64.b64encode(data).decode("ascii")


def data_url(data: bytes) -> str:
    """根据真实内容构造 Data URL"""
    return f"data:{decode_image(data).mime_type};base64,{b64(data)}"


def mask_png(data: bytes, *, transparent: bool) -> bytes:
    """将白色表示编辑区域的统一遮罩转换为厂商所需 PNG"""
    decode_image(data)
    with Image.open(BytesIO(data)) as source:
        mask = source.convert("L")
        if transparent:
            target = Image.new("RGBA", source.size, (0, 0, 0, 255))
            target.putalpha(ImageOps.invert(mask))
        else:
            target = mask.convert("RGB")
        output = BytesIO()
        target.save(output, format="PNG")
        return output.getvalue()


def precise_image(data: bytes) -> bytes:
    """将精准参考等比缩放并黑边填充到 NovelAI 支持的参考画布"""
    decoded = decode_image(data)
    ratio = decoded.width / decoded.height
    size = (1536, 1024) if ratio > 1.2 else (1024, 1536) if ratio < 1 / 1.2 else (1472, 1472)
    with Image.open(BytesIO(data)) as image:
        target = ImageOps.pad(image.convert("RGB"), size, method=Image.Resampling.LANCZOS, color="black")
        output = BytesIO()
        target.save(output, format="PNG")
        return output.getvalue()
