"""内置 Edictum 会话的模型参数和思考强度契约"""

from __future__ import annotations

import math
from typing import Any


THINKING_LEVELS = {"off", "low", "medium", "high", "xhigh", "max", "ultra"}
MODEL_PARAMETERS = {"temperature", "top_p", "max_tokens"}


def normalize_session_settings(params: dict[str, Any]) -> dict[str, Any]:
    """
    校验内置会话参数并迁移旧的顶层生成参数

    参数:
    - params: 会话参数, 不修改调用方对象

    返回:
    - dict[str, Any]: 生成参数归入 model_params 的新对象, 非法配置抛出 ValueError
    """
    normalized = dict(params)
    thinking = normalized.get("thinking", "off")
    if not isinstance(thinking, str) or thinking not in THINKING_LEVELS:
        raise ValueError("thinking 必须是 off/low/medium/high/xhigh/max/ultra")
    model_params = normalized.get("model_params", {})
    if not isinstance(model_params, dict):
        raise ValueError("model_params 必须是对象")
    model_params = dict(model_params)
    for key in MODEL_PARAMETERS:
        if key in normalized:
            value = normalized.pop(key)
            if key in model_params and model_params[key] != value:
                raise ValueError(f"{key} 与 model_params 中的值冲突")
            model_params[key] = value
    unknown = model_params.keys() - MODEL_PARAMETERS
    if unknown:
        raise ValueError(f"未知模型生成参数: {', '.join(sorted(unknown))}")
    for key, value in model_params.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} 必须是有限数字")
        if key == "temperature" and not 0 <= value <= 2:
            raise ValueError("temperature 必须在 0 到 2 之间")
        if key == "top_p" and not 0 < value <= 1:
            raise ValueError("top_p 必须大于 0 且不超过 1")
        if key == "max_tokens" and (not isinstance(value, int) or value <= 0):
            raise ValueError("max_tokens 必须是正整数")
    if model_params or "model_params" in normalized:
        normalized["model_params"] = model_params
    return normalized
