"""绘图默认值合并和各协议入参构造, 不参与返回格式选择"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
import json
import math
import secrets
from typing import Any, Mapping, Sequence

from .media import MAX_IMAGES, b64, data_url, decode_image, mask_png, precise_image
from .types import ImageGenParams, ImageInput, NovelAIParams


def merge_native(target: dict[str, Any], extra: Mapping[str, Any], *, conflict: bool = True) -> None:
    """递归合并原生字段, 生成后的请求不允许被原生字段覆盖"""
    for key, value in extra.items():
        if not isinstance(key, str):
            raise ValueError("native 字段名必须是字符串")
        if key in target and isinstance(target[key], dict) and isinstance(value, Mapping):
            merge_native(target[key], value, conflict=conflict)
        elif key in target and conflict:
            raise ValueError(f"native 与已构造参数冲突: {key}")
        else:
            target[key] = deepcopy(value)


def merge_params(default: ImageGenParams | None, override: ImageGenParams | None, api_type: str) -> ImageGenParams:
    """本次非 None 字段覆盖默认值, 原生字典按层级合并, 列表整体替换"""
    cls = NovelAIParams if api_type == "novelai" else ImageGenParams
    values: dict[str, Any] = {}
    native: dict[str, Any] = {}
    allowed = {field.name for field in fields(cls)}
    for params in (default, override):
        if params is None:
            continue
        if not isinstance(params, ImageGenParams):
            raise ValueError("params 必须是 ImageGenParams 或 NovelAIParams")
        for field in fields(params):
            value = getattr(params, field.name)
            if value is None:
                continue
            if field.name not in allowed:
                raise ValueError("当前协议不接受 NovelAI 专有参数")
            if field.name == "native":
                if not isinstance(value, Mapping):
                    raise ValueError("native 必须是 JSON 对象")
                merge_native(native, value, conflict=False)
            else:
                values[field.name] = deepcopy(value)
    try:
        json.dumps(native, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("native 必须仅包含有效 JSON 值") from None
    return cls(**values, native=native)


def validate_params(params: ImageGenParams, api_type: str, model: str) -> None:
    """在发送任何请求前校验公共值和已知模型不支持的能力"""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model 不能为空")
    if params.n is not None and (type(params.n) is not int or not 1 <= params.n <= MAX_IMAGES):
        raise ValueError("n 必须是 1 到 32 的整数")
    if params.seed is not None and (type(params.seed) is not int or not 0 <= params.seed < 2**32):
        raise ValueError("seed 必须是 32 位无符号整数")
    if params.size is not None:
        if isinstance(params.size, tuple):
            if len(params.size) != 2 or any(type(v) is not int or v <= 0 for v in params.size):
                raise ValueError("像素尺寸必须是两个正整数")
        elif not isinstance(params.size, str) or not params.size:
            raise ValueError("size 必须是尺寸字符串或像素元组")
    if params.negative_prompt is not None and not isinstance(params.negative_prompt, str):
        raise ValueError("negative_prompt 必须是字符串")
    if params.output_format not in {None, "png", "jpeg", "webp"}:
        raise ValueError("不支持的 output_format")
    if api_type == "openai_images" and (params.seed is not None or params.negative_prompt is not None):
        raise ValueError("OpenAI Images 协议不支持公共 seed 或 negative_prompt, 自定义扩展可通过 native 指定")
    if api_type == "seedream" and (params.n or 1) != 1:
        raise ValueError("Seedream 组图不等同 n, 请通过 native 显式配置组图能力")
    if api_type == "seedream" and params.negative_prompt is not None:
        raise ValueError("Seedream 协议不支持 negative_prompt")
    if isinstance(params, NovelAIParams):
        for name in ("scale", "cfg_rescale", "strength", "noise", "skip_cfg_above_sigma"):
            value = getattr(params, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)):
                raise ValueError(f"{name} 必须是有限数值")
        if params.steps is not None and (type(params.steps) is not int or params.steps <= 0):
            raise ValueError("steps 必须是正整数")
        if params.precise_references and params.vibes:
            raise ValueError("精准参考和 Vibe Transfer 不能同时使用")
        if params.precise_references and not model.startswith("nai-diffusion-4-5-"):
            raise ValueError("精准参考仅支持 NovelAI 4.5")
        if params.vibes and model.startswith("nai-diffusion-5-"):
            raise ValueError("NovelAI 5 暂不支持 Vibe Transfer")
        if params.output_format == "jpeg":
            raise ValueError("NovelAI 输出格式仅支持 png 或 webp")
        for character in params.characters or ():
            if not isinstance(character.prompt, str) or not isinstance(character.negative_prompt, str):
                raise ValueError("角色提示词必须是字符串")
            for center in character.centers or ():
                if len(center) != 2 or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1 for v in center):
                    raise ValueError("角色位置必须是 0 到 1 的归一化坐标")
        for reference in params.precise_references or ():
            if reference.kind not in {"character", "style", "character_style"}:
                raise ValueError("未知精准参考类型")
            for value in (reference.strength, reference.fidelity):
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                    raise ValueError("参考强度和精度必须是有限数值")
        for reference in params.vibes or ():
            encoding = reference.encoding
            if encoding.model != model or not encoding.data or len(encoding.data) > 32 * 1024 * 1024:
                raise ValueError("Vibe 编码为空, 过大或与当前模型不匹配")
            if not 0 <= encoding.information_extracted <= 1:
                raise ValueError("Vibe 信息提取值必须在 0 到 1 之间")
            if reference.strength is not None and (not math.isfinite(reference.strength) or not 0 <= reference.strength <= 1):
                raise ValueError("Vibe 强度必须在 0 到 1 之间")


def input_sources(images: Sequence[ImageInput], mask: ImageInput | None, params: ImageGenParams) -> list[ImageInput]:
    """收集要读取的输入, 同一次请求由客户端按来源去重"""
    sources = list(images)
    if mask is not None:
        sources.append(mask)
    if isinstance(params, NovelAIParams):
        sources.extend(reference.image for reference in params.precise_references or ())
    if len(sources) > MAX_IMAGES:
        raise ValueError("输入图片数量超过上限")
    if any(not isinstance(source, ImageInput) for source in sources):
        raise ValueError("所有图片都必须显式使用 ImageInput")
    return sources


def source_key(source: ImageInput) -> tuple[object, ...]:
    """请求内缓存键, 不保存到日志或持久化存储"""
    return source.kind, source.value, source.mime_type


def build_request(
    api_type: str, model: str, prompt: str, params: ImageGenParams,
    images: Sequence[ImageInput], mask: ImageInput | None, resolved: Mapping[tuple[object, ...], bytes],
) -> dict[str, Any]:
    """
    构造一个生成或编辑请求, 不改变 URL

    参数:
    - api_type: 入参协议
    - model: 实际模型
    - prompt: 提示词或编辑指令
    - params: 合并后的参数
    - images: 编辑底图
    - mask: 白色表示编辑区域的遮罩
    - resolved: 已读取的输入图片

    返回:
    - 可传给 HTTP 客户端的请求关键字参数
    """
    def image_bytes(source: ImageInput) -> bytes:
        return resolved[source_key(source)]

    if mask is not None and decode_image(image_bytes(mask)).width != decode_image(image_bytes(images[0])).width:
        raise ValueError("遮罩与第一张底图尺寸必须一致")
    if mask is not None and decode_image(image_bytes(mask)).height != decode_image(image_bytes(images[0])).height:
        raise ValueError("遮罩与第一张底图尺寸必须一致")
    body: dict[str, Any] = {"model": model, "prompt": prompt}
    size = params.size
    if size is not None:
        body["size"] = f"{size[0]}x{size[1]}" if isinstance(size, tuple) else size
    if api_type == "novelai":
        assert isinstance(params, NovelAIParams)
        parameters: dict[str, Any] = {
            "params_version": 3, "width": 832, "height": 1216, "steps": 28,
            "scale": 5.0, "sampler": "k_euler_ancestral", "n_samples": params.n or 1,
            "seed": params.seed if params.seed is not None else secrets.randbits(32),
        }
        if size is not None:
            if isinstance(size, str):
                try:
                    size = tuple(int(part) for part in size.split("x"))
                except ValueError:
                    raise ValueError("NovelAI size 必须指定宽高像素") from None
            if len(size) != 2 or any(value <= 0 for value in size):
                raise ValueError("NovelAI size 必须指定宽高像素")
            parameters.update(width=size[0], height=size[1])
        for name in ("sampler", "steps", "scale", "cfg_rescale", "noise_schedule", "strength", "noise", "extra_noise_seed",
                     "sm", "sm_dyn", "dynamic_thresholding", "skip_cfg_above_sigma", "color_correct", "add_original_image", "params_version"):
            value = getattr(params, name)
            if value is not None:
                parameters[name] = value
        if params.output_format is not None:
            parameters["image_format"] = params.output_format
        negative = params.negative_prompt or ""
        parameters["negative_prompt"] = negative
        if model.startswith(("nai-diffusion-4", "nai-diffusion-5")) or params.characters:
            positive_chars, negative_chars = [], []
            for character in params.characters or ():
                centers = [{"x": x, "y": y} for x, y in character.centers or ()]
                positive_chars.append({"char_caption": character.prompt, "centers": centers})
                negative_chars.append({"char_caption": character.negative_prompt, "centers": centers})
            parameters["v4_prompt"] = {"caption": {"base_caption": prompt, "char_captions": positive_chars},
                                       "use_coords": params.use_coords if params.use_coords is not None else False,
                                       "use_order": params.use_order if params.use_order is not None else True}
            parameters["v4_negative_prompt"] = {"caption": {"base_caption": negative, "char_captions": negative_chars}}
        body = {"input": prompt, "model": model, "action": "generate", "parameters": parameters}
        if images:
            if len(images) != 1:
                raise ValueError("NovelAI 编辑只接受一张底图, 参考图请通过 precise_references 或 vibes 传入")
            parameters["image"] = b64(image_bytes(images[0]))
            parameters.setdefault("strength", 0.7)
            parameters.setdefault("noise", 0.0)
            body["action"] = "img2img"
            if mask is not None:
                if not model.endswith("-inpainting"):
                    raise ValueError("NovelAI 局部重绘需要显式指定对应的 inpainting 模型")
                body["action"] = "infill"
                parameters["mask"] = b64(mask_png(image_bytes(mask), transparent=False))
                parameters["img2img"] = {key: parameters.pop(key) for key in ("strength", "noise")}
        references = params.precise_references or ()
        if references:
            parameters["director_reference_images"] = [b64(precise_image(image_bytes(reference.image))) for reference in references]
            parameters["director_reference_descriptions"] = [
                {"caption": {"base_caption": "character&style" if reference.kind == "character_style" else reference.kind,
                             "char_captions": []}} for reference in references
            ]
            parameters["director_reference_strength_values"] = [reference.strength if reference.strength is not None else 1.0 for reference in references]
            parameters["director_reference_secondary_strength_values"] = [reference.fidelity if reference.fidelity is not None else 1.0 for reference in references]
            parameters["director_reference_information_extracted"] = [1.0 for _ in references]
        vibes = params.vibes or ()
        if vibes:
            parameters["reference_image_multiple"] = [b64(reference.encoding.data) for reference in vibes]
            parameters["reference_strength_multiple"] = [reference.strength if reference.strength is not None else 0.6 for reference in vibes]
            parameters["reference_information_extracted_multiple"] = [reference.encoding.information_extracted for reference in vibes]
    elif api_type == "seedream":
        if mask is not None:
            raise ValueError("Seedream 通用协议不支持 mask, 特定交互编辑参数通过 native 显式传入")
        if images:
            values = [data_url(image_bytes(source)) for source in images]
            body["image"] = values[0] if len(values) == 1 else values
        if params.seed is not None:
            body["seed"] = params.seed
        if params.output_format is not None:
            raise ValueError("Seedream 通用协议不支持 output_format, 特定模型扩展请使用 native")
    else:
        body["n"] = params.n or 1
        if params.output_format is not None:
            body["output_format"] = params.output_format
    merge_native(body, params.native or {})
    if api_type == "openai_images" and images:
        if "image" in body or "images" in body or "mask" in body:
            raise ValueError("native 不能覆盖编辑图片或遮罩")
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for index, source in enumerate(images):
            data = image_bytes(source)
            mime = decode_image(data).mime_type
            suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime)
            if suffix is None:
                raise ValueError("OpenAI 编辑仅接受 PNG, JPEG 或 WebP 图片")
            files.append(("image[]" if len(images) > 1 else "image", (f"image-{index}.{suffix}", data, mime)))
        if mask is not None:
            files.append(("mask", ("mask.png", mask_png(image_bytes(mask), transparent=True), "image/png")))
        form = {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value) for key, value in body.items() if value is not None}
        return {"data": form, "files": files}
    return {"json": body}
