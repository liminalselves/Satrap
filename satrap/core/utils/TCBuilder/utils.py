"""
模型工具调用构建组件

定义同步和异步工具基类及工具管理器,
负责生成模型工具描述, 校验调用参数并统一包装执行结果
"""

from typing import Dict, Tuple, Any, cast
import json
from satrap.core.log import logger


_SENSITIVE_ARGUMENT_NAMES = {
    "access_token",
    "api_key",
    "api_secret",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
}

"""工具参数日志中必须脱敏的字段名"""


def _create_tool_error(
    tool_name: str, message: str, error_type: str
) -> Dict[str, object]:
    """
    创建工具错误结果

    参数:
    - tool_name: 工具名称
    - message: 消息内容
    - error_type: 错误类型

    返回:
    - Dict[str, object]: 创建工具错误结果
    """
    return {
        "error": message,
        "ok": False,
        "error_type": error_type,
        "tool_name": tool_name,
    }


def _safe_json_dumps(data: object) -> str:
    """
    安全序列化工具参数

    参数:
    - data: 输入数据

    返回:
    - str: 安全序列化工具参数
    """
    try:
        return json.dumps(data, ensure_ascii=False)
    except TypeError:
        return json.dumps(str(data), ensure_ascii=False)


def _redact_argument_value(value: object, depth: int = 0) -> object:
    """
    递归脱敏工具参数中的凭据字段并限制递归深度

    参数:
    - value: 原始参数值
    - depth: 当前递归深度

    返回:
    - object: 可安全写入日志的参数副本
    """
    if depth >= 4:
        return "<省略>"
    if isinstance(value, dict):
        output: dict[str, object] = {}
        for raw_key, item in cast(dict[object, object], value).items():
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_ARGUMENT_NAMES or normalized.endswith(
                ("_token", "_password", "_secret", "_api_key")
            ):
                output[key] = "********"
            else:
                output[key] = _redact_argument_value(item, depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        sequence = cast(list[object] | tuple[object, ...], value)
        return [_redact_argument_value(item, depth + 1) for item in sequence]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


def _summarize_arguments(arguments: object, max_length: int = 500) -> str:
    """
    生成日志用参数摘要

    参数:
    - arguments: 调用参数
    - max_length: 最大length

    返回:
    - str: 生成日志用参数摘要
    """
    summary = _safe_json_dumps(_redact_argument_value(arguments))
    if len(summary) <= max_length:
        return summary
    return summary[:max_length] + "..."


def _tool_execution_error(
    tool_name: str, error: Exception, arguments: object, *, async_: bool = False
) -> Dict[str, object]:
    """
    记录工具异常详情并构造不含异常文本的安全返回值

    参数:
    - tool_name: 工具名称
    - error: 捕获的异常
    - arguments: 工具调用参数
    - async_: 是否为异步工具

    返回:
    - Dict[str, object]: 安全的结构化工具错误
    """
    mode = "异步工具" if async_ else "工具"
    args_summary = _summarize_arguments(arguments)
    logger.error(
        f"[执行{mode}] 工具 {tool_name} 执行出错: {type(error).__name__}: {error}, 参数: {args_summary}"
    )
    return _create_tool_error(
        tool_name,
        f"工具执行失败: {type(error).__name__}",
        "execution_error",
    )


def create_tool_defined(
    tool_name: str,
    description: str,
    params_dict: Dict[str, Tuple[str, str]],  # 参数名 -> (类型, 描述)
) -> Dict[str, Any]:
    """
    创建一个符合 OpenAI function calling 规范的工具定义

    参数:
    - tool_name: 工具名称
    - description: 工具描述
    - params_dict: 参数字典, 键为参数名, 值为一个元组 (类型, 描述), 结构如下:

        {
            "param1": ("string", "这是第一个参数"),
            "param2": ("number", "这是第二个参数"),
            ...
        }

    其中参数类型可以是 "string", "number", "boolean", "array", "object"

    返回:
    - 一个字典, 符合 OpenAI function calling 的工具定义格式; 如果创建失败, 返回空字典
    """
    properties: dict[str, dict[str, str]] = {}
    required: list[str] = []

    try:
        for param_name, (param_type, param_desc) in params_dict.items():
            properties[param_name] = {"type": param_type, "description": param_desc}
            required.append(param_name)

    except Exception as e:
        logger.error(f"[创建工具] 工具定义 {tool_name} 生成失败: {e}")
        return {}

    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }
