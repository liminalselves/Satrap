from typing import Dict, Tuple, Any, Union, List
import json

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

class ToolsManager:
    """工具管理器, 用于注册和执行工具"""
    def __init__(self):
        self.tools: Dict[str, Tool] = {}

    def register_tool(self, tool: Tool):
        """注册工具
        
        参数:
        - tool: 要注册的工具实例, 必须是 Tool 类的子类
        """
        self.tools[tool.get_tool_name()] = tool

    def get_tools_definitions(self) -> list:
        """获取所有工具的 OpenAI 格式定义
        
        返回:
        - 一个列表, 每个元素为所有已注册工具的 OpenAI 格式定义
        """
        return [tool.get_tool_defined() for tool in self.tools.values()]

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """执行指定工具"""
        if tool_name not in self.tools:
            return {"error": f"工具 {tool_name} 不存在"}

        tool = self.tools[tool_name]
        return tool(**arguments)

    @staticmethod
    def get_call_info(call_info: Dict[str, Any]):
        """获取工具调用信息
        
        参数:
        - call_info: 工具调用信息, 格式为 {"name": "工具名", "arguments": {"param1": "值1", "param2": "值2", ...}}
        
        返回:
        - 一个元组 (工具名, 参数字典)
        """
        tool_name = call_info.get("name", "")
        arguments = call_info.get("arguments", {})
        return tool_name, arguments

    @staticmethod
    def create_call_message(call_info: Dict[str, Any]) -> Dict[str, Any]:
        """创建工具调用消息
        
        参数:
        - call_info: 工具调用信息, 格式为 {"name": "工具名", "arguments": {"param1": "值1", "param2": "值2", ...}}
        
        返回:
        - 一个字典, 符合 OpenAI function calling 的工具调用消息格式

        例如:
        ```
        {
            "id": "call_abc123",
            "type": "function",
            "function": {
                "name": "sum",
                "arguments": "{"a": 1, "b": 2}"
            }
        }
        ```
        """
        tool_name = call_info.get("name", "")
        tool_call_id = call_info.get("id", "")
        arguments = call_info.get("arguments", {})
        # 创建工具调用消息

        return {
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(arguments, ensure_ascii=False)
            }
        }

    def execute_tool_call(self, call_info: Dict[str, Any]) -> Any:
        """执行工具调用流程
        
        参数:
        - call_info: 工具调用信息, 格式为 {"name": "工具名", "arguments": {"param1": "值1", "param2": "值2", ...}}
        
        返回:
        - 元组 (工具调用消息, 工具调用的返回结果)
        """
        tool_name, arguments = self.get_call_info(call_info)
        tool_message = self.create_call_message(call_info)
        tool_result = self.execute_tool(tool_name, arguments)
        return tool_message, tool_result

