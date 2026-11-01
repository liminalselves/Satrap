"""生图参数策略, 共用模型声明与执行校验"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, cast

from satrap.core.APICall.ImageCall import ImageGen, AsyncImageGen, ImageGenParams
from satrap.core.APICall.ImageCall.media import MAX_IMAGES, MAX_IMAGE_PIXELS
from satrap.core.APICall.ImageCall.request import build_request, merge_params, validate_params


@dataclass(frozen=True)
class ImagePolicy:
    """冻结一次安装的提示词, 尺寸和数量策略"""

    accept_natural_language: bool = True
    accept_keywords: bool = True
    prompt_guidance: str = ""
    size_mode: str = "choices"
    size_fixed: str = "1024x1024"
    size_default: str = "1024x1024"
    size_choices: tuple[str, ...] = ("1024x1024", "832x1216", "1216x832")
    width_min: int = 512
    width_max: int = 2048
    height_min: int = 512
    height_max: int = 2048
    size_step: int = 64
    max_pixels: int = 4194304
    count_mode: str = "fixed"
    count_fixed: int = 1
    count_default: int = 1
    count_min: int = 1
    count_max: int = 4
    allow_negative_prompt: bool = False

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ImagePolicy:
        """
        严格构造策略, 非法配置不能回退后继续计费

        参数:
        - config: 插件配置, 缺省字段沿用公开默认值

        返回:
        - 通过交叉校验的不可变策略
        """
        defaults = cls()
        values = {key: config.get(key, getattr(defaults, key)) for key in cls.__dataclass_fields__}
        choices = values["size_choices"]
        if isinstance(choices, str):
            choices = tuple(dict.fromkeys(line.strip() for line in choices.splitlines() if line.strip()))
        if not isinstance(choices, tuple) or any(not isinstance(item, str) or not item for item in cast(tuple[object, ...], choices)):
            raise ValueError("候选尺寸必须使用多行文本, 每行一个非空尺寸")
        choices = cast(tuple[str, ...], choices)
        values["size_choices"] = choices
        policy = cls(**values)
        for name in ("accept_natural_language", "accept_keywords", "allow_negative_prompt"):
            if type(getattr(policy, name)) is not bool:
                raise ValueError(f"{name} 必须是布尔值")
        if not policy.accept_natural_language and not policy.accept_keywords:
            raise ValueError("至少允许一种提示词写法")
        for name in ("prompt_guidance", "size_fixed", "size_default"):
            if not isinstance(getattr(policy, name), str):
                raise ValueError(f"{name} 必须是字符串")
        if len(policy.prompt_guidance) > 4000:
            raise ValueError("提示词指导不能超过 4000 字符")
        if policy.size_mode not in {"fixed", "choices", "range"} or policy.count_mode not in {"fixed", "range"}:
            raise ValueError("尺寸或数量模式无效")
        for name in ("width_min", "width_max", "height_min", "height_max", "size_step", "max_pixels",
                     "count_fixed", "count_default", "count_min", "count_max"):
            if type(getattr(policy, name)) is not int or getattr(policy, name) < 1:
                raise ValueError(f"{name} 必须是正整数")
        if policy.max_pixels > MAX_IMAGE_PIXELS:
            raise ValueError("最大像素数超过图片调用层上限")
        if policy.width_min > policy.width_max or policy.height_min > policy.height_max:
            raise ValueError("宽高范围的最小值不能超过最大值")
        if not 1 <= policy.count_min <= policy.count_default <= policy.count_max <= MAX_IMAGES or policy.count_fixed > MAX_IMAGES:
            raise ValueError("数量默认值必须在范围内, 数量上限为 32")
        if policy.size_mode == "choices" and (not choices or len(choices) > 100 or policy.size_default not in choices):
            raise ValueError("候选尺寸必须为 1 到 100 项, 默认尺寸必须位于候选列表")
        policy.resolve({})
        return policy

    @staticmethod
    def pixels(size: str) -> tuple[int, int] | None:
        """解析完整的正整数像素尺寸, 其它原生尺寸值由协议校验"""
        match = re.fullmatch(r"([1-9][0-9]{0,5})x([1-9][0-9]{0,5})", size)
        return (int(match[1]), int(match[2])) if match else None

    def _size(self, size: str) -> str | tuple[int, int]:
        """规范像素尺寸并限制总像素, 保留非空的厂商原生值"""
        if not isinstance(size, str) or not size.strip() or len(size) > 64:
            raise ValueError("尺寸必须是最多 64 字符的非空字符串")
        pixels = self.pixels(size)
        if pixels is not None and pixels[0] * pixels[1] > self.max_pixels:
            raise ValueError("尺寸超过最大像素数")
        return pixels or size

    def properties(self) -> dict[str, Any]:
        """按当前策略生成参数 schema, 固定参数不交给模型填写"""
        styles: list[str] = []
        if self.accept_natural_language:
            styles.append("自然语言描述")
        if self.accept_keywords:
            styles.append("关键词或标签")
        prompt = "支持" + "与".join(styles) + (", 可以混合使用" if len(styles) == 2 else "")
        if self.prompt_guidance:
            prompt += "; " + self.prompt_guidance
        properties: dict[str, Any] = {"prompt": {"type": "string", "minLength": 1, "maxLength": 16000, "description": prompt}}
        if self.size_mode == "choices":
            properties["size"] = {"type": "string", "enum": list(self.size_choices), "default": self.size_default,
                                  "description": "输出尺寸, 省略时使用默认尺寸"}
        elif self.size_mode == "range":
            default = self.pixels(self.size_default)
            assert default is not None
            for index, axis in enumerate(("width", "height")):
                properties[axis] = {"type": "integer", "minimum": getattr(self, axis + "_min"),
                                    "maximum": getattr(self, axis + "_max"), "multipleOf": self.size_step,
                                    "default": default[index], "description": f"像素{('宽', '高')[index]}, 宽高须同时填写或同时省略; 总像素不超过 {self.max_pixels}"}
        if self.count_mode == "range":
            properties["n"] = {"type": "integer", "minimum": self.count_min, "maximum": self.count_max,
                               "default": self.count_default, "description": "请求图片数量, 省略使用默认值"}
        if self.allow_negative_prompt:
            properties["negative_prompt"] = {"type": "string", "maxLength": 16000,
                                              "description": "负向提示词, 省略继承模型默认值, 空串表示本次清空"}
        return properties

    def resolve(self, arguments: dict[str, Any]) -> ImageGenParams:
        """
        把本次允许的参数解析为调用层参数

        参数:
        - arguments: 不含 prompt 的模型调用参数

        返回:
        - 固定值与可调值均已校验的公共绘图参数
        """
        allowed = self.allowed_fields()
        if set(arguments) - allowed:
            raise ValueError("包含当前策略未开放的参数: " + ", ".join(sorted(set(arguments) - allowed)))
        size = self.size_fixed if self.size_mode == "fixed" else self.size_default
        if self.size_mode == "choices":
            size = arguments.get("size", size)
            if not isinstance(size, str) or size not in self.size_choices:
                raise ValueError("size 不在候选尺寸列表中")
        if self.size_mode == "range":
            if ("width" in arguments) != ("height" in arguments):
                raise ValueError("width 和 height 必须同时填写或同时省略")
            default = self.pixels(self.size_default)
            if default is None:
                raise ValueError("范围模式的默认尺寸必须为 宽x高")
            width, height = arguments.get("width", default[0]), arguments.get("height", default[1])
            for axis, value in (("width", width), ("height", height)):
                if (type(value) is not int or not getattr(self, axis + "_min") <= value <= getattr(self, axis + "_max")
                        or value % self.size_step):
                    raise ValueError(f"{axis} 必须在配置范围内并为 {self.size_step} 的整数倍")
            size = f"{width}x{height}"
        count = self.count_fixed if self.count_mode == "fixed" else arguments.get("n", self.count_default)
        if type(count) is not int or not 1 <= count <= MAX_IMAGES:
            raise ValueError("n 必须是 1 到 32 的整数")
        if self.count_mode == "range" and not self.count_min <= count <= self.count_max:
            raise ValueError("n 不在允许的数量范围内")
        negative = arguments.get("negative_prompt")
        if "negative_prompt" in arguments and (not isinstance(negative, str) or len(negative) > 16000):
            raise ValueError("negative_prompt 必须是最多 16000 字符的字符串")
        return ImageGenParams(size=self._size(size), n=count, negative_prompt=negative)

    def allowed_fields(self) -> set[str]:
        """返回可调字段名, 配置校验期间不依赖尚未验证的默认尺寸"""
        names: set[str] = {"size"} if self.size_mode == "choices" else {"width", "height"} if self.size_mode == "range" else set()
        if self.count_mode == "range":
            names.add("n")
        if self.allow_negative_prompt:
            names.add("negative_prompt")
        return names

    def params(self, client: ImageGen | AsyncImageGen, arguments: dict[str, Any]) -> ImageGenParams:
        """合并模型默认参数并预构造请求, 在计费前检查协议与原生字段冲突"""
        override = self.resolve(arguments)
        if client.api_type == "seedream":
            override = ImageGenParams(size=override.size, n=override.n, negative_prompt=override.negative_prompt,
                                      native={"sequential_image_generation": "disabled"})
        merged = merge_params(client.default_params, override, client.api_type)
        model = merged.model or client.model
        validate_params(merged, client.api_type, model)
        build_request(client.api_type, model, "生图插件参数校验", merged, (), None, {})
        return override

    def validate_model(self, client: ImageGen | AsyncImageGen) -> None:
        """安装时检查开放的参数是否被所选协议接受, 不访问网络"""
        if self.allow_negative_prompt and client.api_type != "novelai":
            raise ValueError("所选协议不支持公共 negative_prompt, 请关闭负向提示词参数")
        if client.api_type == "seedream" and (self.count_fixed if self.count_mode == "fixed" else self.count_max) != 1:
            raise ValueError("当前 Seedream 调用层仅支持 n=1, 请将数量配置为固定 1")
        sizes = self.size_choices if self.size_mode == "choices" else (self.size_fixed if self.size_mode == "fixed" else self.size_default,)
        for size in sizes:
            self._size(size)
            arguments = {"size": size} if self.size_mode == "choices" else {}
            self.params(client, arguments)
