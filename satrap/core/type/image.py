"""图片生成的参数, 输入和返回类型"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping, Sequence

from satrap.core.type.json_value import JsonValue


@dataclass(frozen=True, kw_only=True)
class ImageInput:
    """显式标记图片来源, 不根据字符串内容猜测路径或 URL"""

    kind: Literal["path", "bytes", "url"]
    """显式指定本地路径, 字节或 HTTP 地址, 不从字符串猜测来源"""
    value: str | Path | bytes = field(repr=False)
    """与 kind 对应的图片来源, 隐藏于对象表示以免泄露内容"""
    mime_type: str | None = None
    """字节来源的可选 MIME 提示, 默认 None, 实际格式仍须解码验证"""

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
    """该角色的正向描述, 纳入 NovelAI 角色提示"""
    negative_prompt: str = ""
    """该角色的排除描述, 默认为空字符串"""
    centers: Sequence[tuple[float, float]] | None = None
    """角色中心的归一化坐标序列, 每个 x 和 y 均在 0 到 1, None 表示未指定"""


@dataclass(frozen=True, kw_only=True)
class PreciseReference:
    """NovelAI 角色或风格参考, 按对象组织以保持字段对应"""

    image: ImageInput
    """运行时提供的参考图片, 发送前读取并转换为协议格式"""
    kind: Literal["character", "style", "character_style"] = "character"
    """提取角色, 风格或二者, 默认为 character"""
    strength: float | None = None
    """精准参考主强度, 须为有限数值, None 时请求使用 1.0"""
    fidelity: float | None = None
    """精准参考次级强度, 须为有限数值, None 时请求使用 1.0"""


@dataclass(frozen=True, kw_only=True)
class VibeEncoding:
    """可复用的 Vibe 编码, 不包含原始图片"""

    data: bytes = field(repr=False)
    """编码接口返回的非空二进制内容, 使用时上限 32 MiB, 不显示于对象表示"""
    model: str
    """生成此编码的模型标识, 必须与使用它的绘图模型一致"""
    information_extracted: float
    """编码时提取参考信息的比例, 使用时须在 0 到 1 之间"""


@dataclass(frozen=True, kw_only=True)
class VibeReference:
    """已编码的 Vibe 与本次参考强度"""

    encoding: VibeEncoding
    """与当前模型匹配的预编码 Vibe, 不直接传入原图"""
    strength: float | None = None
    """本次参考影响强度, 范围 0 到 1, None 时请求使用 0.6"""


@dataclass(frozen=True, kw_only=True)
class ImageGenParams:
    """公共绘图参数, None 表示继承默认值, 空列表用于清除默认列表"""

    model: str | None = None
    """覆盖客户端配置的模型标识, None 继承默认值"""
    size: str | tuple[int, int] | None = None
    """输出尺寸字符串或宽高像素正整数对, None 继承默认值"""
    n: int | None = None
    """请求图片数量, 允许 1 到 32, None 继承默认值, Seedream 通用协议仅支持 1"""
    negative_prompt: str | None = None
    """不希望出现的内容, None 继承默认值, OpenAI Images 和 Seedream 通用协议不支持"""
    seed: int | None = None
    """生成随机种子, 须为 32 位无符号整数, None 继承默认值, OpenAI Images 通用协议不支持"""
    output_format: Literal["png", "jpeg", "webp"] | None = None
    """请求的输出格式, None 继承默认值, NovelAI 不支持 jpeg, Seedream 通用协议不支持"""
    native: Mapping[str, JsonValue] | None = field(default=None, repr=False)
    """协议原生 JSON 扩展, 默认 None, 与默认值递归合并且不能覆盖已构造字段"""


@dataclass(frozen=True, kw_only=True)
class NovelAIParams(ImageGenParams):
    """NovelAI 专有参数, 由请求层转换为上游结构"""

    sampler: str | None = None
    """NovelAI 采样器名称, None 继承默认值, 最终未指定时使用 k_euler_ancestral"""
    steps: int | None = None
    """去噪采样步数, 须为正整数, None 继承默认值, 最终未指定时使用 28"""
    scale: float | None = None
    """提示词引导强度, 须为有限数值, None 继承默认值, 最终未指定时使用 5.0"""
    cfg_rescale: float | None = None
    """NovelAI 的 CFG 重缩放参数, 须为有限数值, None 继承默认值"""
    noise_schedule: str | None = None
    """上游噪声调度名称, None 继承默认值, 最终未指定时不发送"""
    inject_default_positive_prompt: bool | None = None
    """是否在正向提示后追加内置质量词, None 继承默认值, 未启用时不追加"""
    inject_default_negative_prompt: bool | None = None
    """是否在负向提示前加入内置排除词, None 继承默认值, 未启用时不追加"""
    strength: float | None = None
    """图生图重绘强度, 须为有限数值, None 继承默认值, 编辑时未指定使用 0.7"""
    noise: float | None = None
    """图生图噪声量, 须为有限数值, None 继承默认值, 编辑时未指定使用 0.0"""
    extra_noise_seed: int | None = None
    """上游额外噪声种子, None 继承默认值, 持久化配置要求非负整数"""
    sm: bool | None = None
    """是否启用 NovelAI 的 SM 采样选项, None 继承默认值"""
    sm_dyn: bool | None = None
    """是否启用 NovelAI 的动态 SM 选项, None 继承默认值"""
    dynamic_thresholding: bool | None = None
    """是否启用上游动态阈值处理, None 继承默认值"""
    skip_cfg_above_sigma: float | None = None
    """高于此噪声 sigma 时跳过 CFG 的阈值, 须为有限数值, None 继承默认值"""
    color_correct: bool | None = None
    """是否启用上游颜色校正, None 继承默认值"""
    add_original_image: bool | None = None
    """是否要求上游附加原始图片, None 继承默认值"""
    params_version: int | None = None
    """NovelAI 参数结构版本, None 继承默认值, 最终未指定时使用 3"""
    characters: Sequence[CharacterPrompt] | None = None
    """角色正负提示和位置列表, None 继承默认值, 空列表清除默认角色"""
    use_coords: bool | None = None
    """是否在角色提示中启用坐标, None 继承默认值, 最终未指定时为 False"""
    use_order: bool | None = None
    """是否在角色提示中启用顺序, None 继承默认值, 最终未指定时为 True"""
    precise_references: Sequence[PreciseReference] | None = None
    """运行时精准参考列表, None 继承默认值, 空列表清除默认值, 不可与 Vibe 同用"""
    vibes: Sequence[VibeReference] | None = None
    """运行时 Vibe 编码参考列表, None 继承默认值, 空列表清除默认值, 不可与精准参考同用"""


@dataclass(frozen=True, kw_only=True)
class GeneratedImage:
    """生成图片字节与解码后的元信息, 不包含临时签名 URL"""

    data: bytes = field(repr=False)
    """经大小和格式校验的生成图片字节, 不显示于对象表示"""
    mime_type: str
    """从图片实际内容解码得到的 MIME 类型"""
    width: int
    """解码得到的图片宽度, 单位为像素"""
    height: int
    """解码得到的图片高度, 单位为像素"""
    seed: int | None = None
    """上游返回的该图片种子, 未提供时为 None"""
    revised_prompt: str | None = None
    """上游返回的修订提示词, 未提供时为 None"""


@dataclass(frozen=True, kw_only=True)
class ImageGenResponse:
    """统一生成结果, 用量缺失时保留 None"""

    images: list[GeneratedImage]
    """按上游输出顺序排列的图片, 成功响应至少包含一张"""
    model: str
    """上游返回的模型名, 缺失时回退到请求使用的模型"""
    request_id: str | None = None
    """经安全格式筛选的上游请求标识, 缺失时为 None"""
    usage: Mapping[str, JsonValue] | None = None
    """上游返回的用量对象, 未提供时为 None, 不从图片数推算"""
    text: str = ""
    """响应中伴随图片的文本, 无文本时为空字符串"""


class ImageGenError(RuntimeError):
    """不携带上游响应正文或凭据的图片调用异常"""

    def __init__(self, code: str, *, status: int | None = None, request_id: str | None = None) -> None:
        self.code = code
        self.status = status
        self.request_id = request_id
        super().__init__(f"图片请求失败: {code}, status={status}")
