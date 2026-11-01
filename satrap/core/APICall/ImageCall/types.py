"""图片生成的参数, 输入和返回类型"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping, Sequence, TypeAlias

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, kw_only=True)
class ImageInput:
    """显式标记图片来源, 不根据字符串内容猜测路径或 URL"""

    kind: Literal["path", "bytes", "url"]
    value: str | Path | bytes = field(repr=False)
    mime_type: str | None = None

    @classmethod
    def from_path(cls, path: str | Path) -> ImageInput:
        """从调用方已授权的本地文件构造输入"""
        return cls(kind="path", value=Path(path))

    @classmethod
    def from_bytes(cls, data: bytes, *, mime_type: str | None = None) -> ImageInput:
        """从图片字节构造输入, 实际格式仍通过解码验证"""
        return cls(kind="bytes", value=data, mime_type=mime_type)

    @classmethod
    def from_url(cls, url: str) -> ImageInput:
        """从显式 HTTP 或 HTTPS 图片地址构造输入"""
        return cls(kind="url", value=url)


@dataclass(frozen=True, kw_only=True)
class CharacterPrompt:
    """角色提示词与可选归一化位置"""

    prompt: str
    negative_prompt: str = ""
    centers: Sequence[tuple[float, float]] | None = None


@dataclass(frozen=True, kw_only=True)
class PreciseReference:
    """NovelAI 角色或风格参考, 按对象组织以保持字段对应"""

    image: ImageInput
    kind: Literal["character", "style", "character_style"] = "character"
    strength: float | None = None
    fidelity: float | None = None


@dataclass(frozen=True, kw_only=True)
class VibeEncoding:
    """可复用的 Vibe 编码, 不包含原始图片"""

    data: bytes = field(repr=False)
    model: str
    information_extracted: float


@dataclass(frozen=True, kw_only=True)
class VibeReference:
    """已编码的 Vibe 与本次参考强度"""

    encoding: VibeEncoding
    strength: float | None = None


@dataclass(frozen=True, kw_only=True)
class ImageGenParams:
    """公共绘图参数, None 表示继承默认值, 空列表用于清除默认列表"""

    model: str | None = None
    size: str | tuple[int, int] | None = None
    n: int | None = None
    negative_prompt: str | None = None
    seed: int | None = None
    output_format: Literal["png", "jpeg", "webp"] | None = None
    native: Mapping[str, JsonValue] | None = field(default=None, repr=False)


@dataclass(frozen=True, kw_only=True)
class NovelAIParams(ImageGenParams):
    """NovelAI 专有参数, 由请求层转换为上游结构"""

    sampler: str | None = None
    steps: int | None = None
    scale: float | None = None
    cfg_rescale: float | None = None
    noise_schedule: str | None = None
    strength: float | None = None
    noise: float | None = None
    extra_noise_seed: int | None = None
    sm: bool | None = None
    sm_dyn: bool | None = None
    dynamic_thresholding: bool | None = None
    skip_cfg_above_sigma: float | None = None
    color_correct: bool | None = None
    add_original_image: bool | None = None
    params_version: int | None = None
    characters: Sequence[CharacterPrompt] | None = None
    use_coords: bool | None = None
    use_order: bool | None = None
    precise_references: Sequence[PreciseReference] | None = None
    vibes: Sequence[VibeReference] | None = None


@dataclass(frozen=True, kw_only=True)
class GeneratedImage:
    """生成图片字节与解码后的元信息, 不包含临时签名 URL"""

    data: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int
    seed: int | None = None
    revised_prompt: str | None = None


@dataclass(frozen=True, kw_only=True)
class ImageGenResponse:
    """统一生成结果, 用量缺失时保留 None"""

    images: list[GeneratedImage]
    model: str
    request_id: str | None = None
    usage: Mapping[str, JsonValue] | None = None
    text: str = ""


class ImageGenError(RuntimeError):
    """不携带上游响应正文或凭据的图片调用异常"""

    def __init__(self, code: str, *, status: int | None = None, request_id: str | None = None) -> None:
        self.code = code
        self.status = status
        self.request_id = request_id
        super().__init__(f"图片请求失败: {code}, status={status}")
