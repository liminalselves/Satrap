"""Satrap 核心通用工具函数导出入口"""
from typing import Any, overload
from ipaddress import ip_address
import json
import ast
import re
from urllib.parse import urlsplit

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

    suffixes = ("/chat/completions", "/completions", "/responses")
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
