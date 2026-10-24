"""Satrap 核心通用工具函数导出入口"""
from urllib.parse import urlsplit
from ipaddress import ip_address
from typing import Any, cast, overload
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


def parse_tool_arguments(arg_str: object) -> tuple[dict[str, Any], str | None]:
    """解析模型工具参数, 同时保留可供下一轮修正的错误原因"""
    if isinstance(arg_str, dict):
        return arg_str, None
    if not isinstance(arg_str, str):
        return {}, "工具参数必须是 JSON 对象"

    def parsed_result(value: object) -> tuple[dict[str, Any], str | None]:
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            return cast(dict[str, Any], value), None
        return {}, "工具参数必须是键名为字符串的 JSON 对象"

    try:
        return parsed_result(json.loads(arg_str))
    except json.JSONDecodeError:
        pass

    try:
        pattern = r'("[^\"\s]+":\s*")(.*?)("(?=\s*[,}]))'
        def fix_value(match: re.Match[str]) -> str:
            return match.group(1) + _repair_json_value(match.group(2)) + match.group(3)

        repaired = re.sub(pattern, fix_value, arg_str, flags=re.DOTALL)
        return parsed_result(json.loads(repaired))
    except json.JSONDecodeError:
        pass

    try:
        return parsed_result(ast.literal_eval(arg_str))
    except (SyntaxError, ValueError):
        pass

    logger.error(f"[安全解析] 无法解析参数: {arg_str[:200]}...")
    return {}, "工具参数不是有效的 JSON 对象, 请重新生成完整参数"


def safe_parse_arguments(arg_str: str | dict[str, Any]) -> dict[str, Any]:
    """解析工具参数; 只需兼容字典结果的调用方可使用此入口"""
    result, _ = parse_tool_arguments(arg_str)
    return result


@overload
def normalize_openai_base_url(base_url: None, *, allow_insecure: bool = False) -> None: ...

@overload
def normalize_openai_base_url(base_url: str, *, allow_insecure: bool = False) -> str: ...

def normalize_openai_base_url(
    base_url: str | None,
    *,
    allow_insecure: bool = False,
) -> str | None:
    """
    归一化 OpenAI 兼容客户端 base_url

    参数:
    - base_url: API 服务地址
    - allow_insecure: 是否显式允许非回环 HTTP 地址

    返回:
    - str | None: 归一化 OpenAI 兼容客户端 base_url
    """
    if not base_url:
        return base_url

    cleaned = base_url.strip().rstrip("/")
    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("API base_url 仅支持 http 或 https 协议")
    if not parsed.hostname:
        raise ValueError("API base_url 缺少有效主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("API base_url 不允许包含用户名或密码")
    if parsed.fragment:
        raise ValueError("API base_url 不允许包含 URL 片段")
    if scheme == "http" and not allow_insecure and not _is_loopback_api_host(parsed.hostname):
        raise ValueError("非回环 API base_url 必须使用 https, 或显式启用 allow_insecure")

    suffixes = ("/chat/completions", "/audio/transcriptions", "/completions", "/responses")
    for suffix in suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break

    return cleaned or base_url


def _is_loopback_api_host(host: str) -> bool:
    """
    判断 API 主机是否为本机回环地址

    参数:
    - host: 主机名或 IP 地址

    返回:
    - bool: 是否为回环地址
    """
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
