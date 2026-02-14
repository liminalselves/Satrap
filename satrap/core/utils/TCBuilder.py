from typing import Dict, Tuple, Any, Union, List
from satrap import logger

def create_tool_defined(
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
    - 一个字典, 符合 OpenAI function calling 的工具定义格式; 如果创建失败, 返回空字典
    """
    properties = {}
    required = []

    try:
        for param_name, (param_type, param_desc) in params_dict.items():
            properties[param_name] = {
                "type": param_type,
                "description": param_desc
            }
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
                "required": required
            }
        }
    }

class Tool:
    """工具基类; 所有工具都应当继承自该类"""
    def __init__(self, tool_name: str, description: str, params_dict: Dict[str, Tuple[str, str]]):
        """初始化工具; 工具的执行应当覆写 execute 方法并进行 `super().__init__(tool_name, description, params_dict)`

        参数:
        - tool_name: 工具名称
        - description: 工具描述
        - params_dict: 参数字典, 键为参数名, 值为一个元组 (类型, 描述), 结构如下:

        {
            "param1": ("string", "这是第一个参数"),
            "param2": ("number", "这是第二个参数"),
            ...
        }

        使用示例:
        ``` python
        class sum(Tool):
            def __init__(
            self,
            tool_name = 'sum',
            description = '计算两个数的和',
            params_dict = {
                "a": ("number", "第一个数"),
                "b": ("number", "第二个数")
            }):
            super().__init__(tool_name, description, params_dict)

            def execute(self, a: float, b: float) -> float:
                '''执行工具; 计算两个数的和'''
                return a + b
        ```
        """
        self.tool_name = tool_name
        self.description = description
        self.params_dict = params_dict

    def get_tool_defined(self) -> Dict[str, Any]:
        """获取工具定义"""
        return create_tool_defined(self.tool_name, self.description, self.params_dict)
    
    def get_tool_name(self) -> str:
        """获取工具名称"""
        return self.tool_name
    
    def execute(self, *input, **kwargs) -> Any:
        """执行工具"""
        return None
    
    def __call__(self, *input, **kwargs):
        result = self.execute(*input, **kwargs)
        return result
