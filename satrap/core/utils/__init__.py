import json
import re

from satrap.core.log import logger

def safe_parse_arguments(arg_str: str) -> dict:
    """容错解析参数字符串, 返回 dict"""
    if not isinstance(arg_str, str):
        return arg_str if isinstance(arg_str, dict) else {}

    try:   # 尝试标准 JSON 解析
        return json.loads(arg_str)
    except json.JSONDecodeError:
        pass

    try:   # 尝试修复常见错误
        pattern = r'("code":\s*")(.*?)("(?=\s*[,}]))'
        def fix_code(match):
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
        import ast
        return ast.literal_eval(arg_str)
    except:
        pass

    # 全部失败, 记录并返回空字典
    logger.error(f"[安全解析] 无法解析参数: {arg_str[:200]}...")
    return {}