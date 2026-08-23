"""Satrap 核心通用工具函数导出入口"""
from typing import Any, overload
import json
import ast
import re

from satrap.core.log import logger


def _repair_json_value(value: str) -> str:
    """
    正则一次性修复字符串值内的 JSON 瑕疵: 裸换行/制表符/引号与未转义反斜杠 (Windows 路径等)

    参数:
    - value: 输入值

    修复模式只在 json.loads 失败后进入, 此时值内的反斜杠几乎都是模型未转义的路径
    分隔符, 因此除已成对的 \" 与 \\ 外, 其余反斜杠一律转义 (避免 C:\new 被
    误解码为换行, C:\bin 被误解码为退格)

    返回:
    - str: 正则一次性修复字符串值内的 JSON 瑕疵: 裸换行/制表符/引号与未转义反斜杠 (Windows 路径等)
    """
    value = value.replace("\\\\", "\uFFFF")   # 保护已成对 \\\\
    value = value.replace('\\"', "\uFFFE")   # 保护已成对 \\"

    value = re.sub(r"\\", lambda m: "\\\\", value)   # 剩余裸反斜杠 (Windows 路径) -> 转义
    # 用 lambda 做替换, 避免 re.sub 替换模板对 \\ 的二次解释 (repl 模板中 \\ -> 单个反斜杠)

    value = value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    value = value.replace("\uFFFF", "\\\\")
    value = value.replace("\uFFFE", '\\"')
    return re.sub(r'"', lambda m: '\\"', value)   # 剩余裸引号 -> 转义


def safe_parse_arguments(arg_str: str | dict[str, Any]) -> dict[str, Any]:
    """
    容错解析参数字符串, 返回 dict

    参数:
    - arg_str: 参数字符串

    返回:
    - dict[str, Any]:  dict
    """
    if not isinstance(arg_str, str):
        return arg_str

    try:   # 尝试标准 JSON 解析
        return json.loads(arg_str)
    except json.JSONDecodeError:
        pass

    try:   # 尝试修复常见错误: 任意字符串字段内的裸换行/引号/反斜杠
        pattern = r'("[^\"\s]+":\s*")(.*?)("(?=\s*[,}]))'
        def fix_value(match: re.Match[str]) -> str:
            return match.group(1) + _repair_json_value(match.group(2)) + match.group(3)

        repaired = re.sub(pattern, fix_value, arg_str, flags=re.DOTALL)
        return json.loads(repaired)
    except Exception:
        pass

    try:   # 尝试 ast.literal_eval

        return ast.literal_eval(arg_str)
    except:
        pass

    logger.error(f"[安全解析] 无法解析参数: {arg_str[:200]}...")
    # 全部失败, 记录并返回空字典
    return {}


@overload
def normalize_openai_base_url(base_url: None) -> None: ...

@overload
def normalize_openai_base_url(base_url: str) -> str: ...

def normalize_openai_base_url(base_url: str | None) -> str | None:
    """
    归一化 OpenAI 兼容客户端 base_url

    参数:
    - base_url: API 服务地址

    返回:
    - str | None: 归一化 OpenAI 兼容客户端 base_url
    """
    if not base_url:
        return base_url

    cleaned = base_url.strip().rstrip("/")
    suffixes = ("/chat/completions", "/completions", "/responses")
    for suffix in suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break

    return cleaned or base_url
