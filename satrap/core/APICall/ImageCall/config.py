"""图片模型配置校验和 JSON 参数解码, 不发送网络请求"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
import json
from typing import TYPE_CHECKING, Any, overload, Literal

from .media import validate_url
from .request import build_request, validate_params
from .types import CharacterPrompt, ImageGenParams, NovelAIParams

if TYPE_CHECKING:
    from satrap.core.type import ImgConfig
    from .sync import ImageGen
    from .async_ import AsyncImageGen

_RUNTIME_MEDIA_FIELDS = {"image", "images", "mask", "precise_references", "vibes", "reference_image", "reference_image_multiple", "director_reference_images"}


def image_params_from_json(payload: object, api_type: str, model: str | None = None) -> ImageGenParams:
    """
    将可持久化 JSON 参数解码为调用层对象, 拒绝运行时图片内容

    参数:
    - payload: 默认绘图参数对象或 None
    - api_type: 请求协议
    - model: 用于能力校验的模型 ID

    返回:
    - 独立的公共或 NovelAI 参数
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("default_params 必须是 JSON 对象或 null")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("default_params 必须仅包含有效 JSON 值") from None
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("default_params 超过 64 KiB, 图片请在调用时传入")

    def check_media(value: object) -> None:
        """配置仅保存参数, 底图, 遮罩和参考图由运行时提供"""
        if isinstance(value, dict):
            if _RUNTIME_MEDIA_FIELDS.intersection(value):
                raise ValueError("模型默认参数不能保存图片或 Vibe 内容, 请在调用时传入")
            for item in value.values():
                check_media(item)
        elif isinstance(value, list):
            for item in value:
                check_media(item)

    check_media(payload)
    cls = NovelAIParams if api_type == "novelai" else ImageGenParams
    unknown = set(payload) - {field.name for field in fields(cls)}
    if unknown:
        raise ValueError("default_params 包含未知字段: " + ", ".join(sorted(unknown)))
    values: dict[str, Any] = deepcopy(payload)
    size = values.get("size")
    if isinstance(size, list):
        values["size"] = tuple(size)
    characters = values.get("characters")
    if characters is not None:
        if not isinstance(characters, list):
            raise ValueError("characters 必须是数组或 null")
        parsed = []
        for item in characters:
            if not isinstance(item, dict) or set(item) - {"prompt", "negative_prompt", "centers"} or not isinstance(item.get("prompt"), str):
                raise ValueError("角色提示词结构无效")
            centers = item.get("centers")
            if centers is not None and (not isinstance(centers, list) or any(not isinstance(center, (list, tuple)) for center in centers)):
                raise ValueError("centers 必须是坐标数组或 null")
            parsed.append(CharacterPrompt(prompt=item["prompt"], negative_prompt=item.get("negative_prompt", ""),
                                          centers=[tuple(center) for center in centers] if centers is not None else None))
        values["characters"] = parsed
    for name in ("sm", "sm_dyn", "dynamic_thresholding", "color_correct", "add_original_image", "use_coords", "use_order"):
        if values.get(name) is not None and not isinstance(values[name], bool):
            raise ValueError(f"{name} 必须是布尔值或 null")
    for name in ("params_version", "extra_noise_seed"):
        if values.get(name) is not None and (type(values[name]) is not int or values[name] < 0):
            raise ValueError(f"{name} 必须是非负整数或 null")
    for name in ("sampler", "noise_schedule"):
        if values.get(name) is not None and (not isinstance(values[name], str) or not values[name]):
            raise ValueError(f"{name} 必须是非空字符串或 null")
    if values.get("model") is not None and not isinstance(values["model"], str):
        raise ValueError("default_params.model 必须是字符串或 null")
    native = values.get("native")
    if native is not None and not isinstance(native, dict):
        raise ValueError("native 必须是 JSON 对象或 null")
    params = cls(**values)
    validate_params(params, api_type, params.model if params.model is not None else model or "unconfigured")
    build_request(api_type, params.model or model or "unconfigured", "配置参数校验", params, (), None, {})
    return params


def validate_img_config(config: ImgConfig) -> None:
    """验证完整地址, 超时和可持久化参数, 允许尚未填写连接信息的空配置"""
    from satrap.core.utils.validation import positive_timeout

    if config.api_type not in {"openai_images", "seedream", "novelai"}:
        raise ValueError("api_type 必须是 openai_images, seedream 或 novelai")
    for name in ("lock_api_key", "allow_insecure_base_url"):
        if not isinstance(getattr(config, name), bool):
            raise ValueError(f"{name} 必须是布尔值")
    for name in ("model", "base_url", "edit_base_url", "api_key"):
        value = getattr(config, name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} 必须是字符串或 null")
    for url in (config.base_url, config.edit_base_url):
        if url:
            validate_url(url, allow_insecure=config.allow_insecure_base_url)
    config.timeout = positive_timeout(config.timeout, "timeout")
    image_params_from_json(config.default_params, config.api_type, config.model)


@overload
def build_imagegen_from_config(cfg: ImgConfig, *, async_: Literal[False] = False) -> ImageGen: ...
@overload
def build_imagegen_from_config(cfg: ImgConfig, *, async_: Literal[True]) -> AsyncImageGen: ...
@overload
def build_imagegen_from_config(cfg: ImgConfig, *, async_: bool) -> ImageGen | AsyncImageGen: ...
def build_imagegen_from_config(cfg: ImgConfig, *, async_: bool = False) -> ImageGen | AsyncImageGen:
    """
    由持久化配置构造图片客户端, 完整保留地址和嵌套参数

    参数:
    - cfg: 命名图片模型配置
    - async_: 是否构造异步客户端

    返回:
    - 同步或异步图片客户端
    """
    from .sync import ImageGen
    from .async_ import AsyncImageGen

    validate_img_config(cfg)
    if not cfg.model or not cfg.api_key or not cfg.base_url:
        raise ValueError("图片模型配置缺少 model, api_key 或完整 base_url")
    cls = AsyncImageGen if async_ else ImageGen
    return cls(api_key=cfg.api_key, base_url=cfg.base_url, model=cfg.model, api_type=cfg.api_type,
               edit_base_url=cfg.edit_base_url or None, timeout=cfg.timeout,
               default_params=image_params_from_json(cfg.default_params, cfg.api_type, cfg.model),
               lock_api_key=cfg.lock_api_key, allow_insecure_base_url=cfg.allow_insecure_base_url)
