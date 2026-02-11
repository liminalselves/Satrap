from typing import Dict, Tuple, Any
from satrap import logger

def create_tool(
    tool_name: str,
    description: str,
    params_dict: Dict[str, Tuple[str, str]]   # 参数名 -> (类型, 描述)
) -> Dict[str, Any]:
    """创建一个符合 OpenAI function calling 规范的工具定义

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
    - 一个字典, 符合 OpenAI function calling 的工具定义格式
    """
    properties = {}
    required = []

    for param_name, (param_type, param_desc) in params_dict.items():
        properties[param_name] = {
            "type": param_type,
            "description": param_desc
        }
        required.append(param_name)

    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    }
