from typing import Any, overload
import json
import ast
import re

from satrap.core.log import logger

def safe_parse_arguments(arg_str: str | dict[str, Any]) -> dict[str, Any]:
    """容错解析参数字符串, 返回 dict"""
    if not isinstance(arg_str, str):
        return arg_str

    try:   # 尝试标准 JSON 解析
        return json.loads(arg_str)
    except json.JSONDecodeError:
        pass

    try:   # 尝试修复常见错误
        pattern = r'("code":\s*")(.*?)("(?=\s*[,}]))'
        def fix_code(match: re.Match[str]):
            prefix = match.group(1)
            code_body = match.group(2)
            suffix = match.group(3)
            code_body = code_body.replace('\\"', '\uFFFF')   # 临时占位符
            code_body = code_body.replace('"', '\\"')
            code_body = code_body.replace('\uFFFF', '\\"')
            code_body = code_body.replace('\n', '\\n').replace('\r', '\\r')
            return prefix + code_body + suffix
        
        repaired = re.sub(pattern, fix_code, arg_str, flags=re.DOTALL)
        return json.loads(repaired)
    except Exception:
        pass


    try:   # 尝试 ast.literal_eval

        return ast.literal_eval(arg_str)
    except:
        pass

    # 全部失败, 记录并返回空字典
    logger.error(f"[安全解析] 无法解析参数: {arg_str[:200]}...")
    return {}


@overload
def normalize_openai_base_url(base_url: None) -> None: ...

@overload
def normalize_openai_base_url(base_url: str) -> str: ...

def normalize_openai_base_url(base_url: str | None) -> str | None:
    """归一化 OpenAI 兼容客户端 base_url"""
    if not base_url:
        return base_url

    cleaned = base_url.strip().rstrip("/")
    suffixes = ("/chat/completions", "/completions", "/responses")
    for suffix in suffixes:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break

    return cleaned or base_url
