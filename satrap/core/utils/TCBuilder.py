from satrap.core.utils.context import ContextManager
from typing import Dict, Tuple, Any, Union, List
from satrap.core.type import LLMCallResponse
import json

from satrap.core.log import logger

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
    """
    工具基类; 所有工具都应当继承自该类

    定义工具时, 应当在子类中通过类属性指定元数据:
        - tool_name: 工具名称
        - description: 工具描述
        - params_dict: 参数字典, 格式如 {"param1": ("类型", "描述"), ...}
    然后实现 execute 方法

    当然仍然支持使用`super().__init__(tool_name, description, params_dict)`初始化工具

    {
        "param1": ("string", "这是第一个参数"),
        "param2": ("number", "这是第二个参数"),
        ...
    }

    使用示例:
    ``` python
    # 写法 1. 直接在类属性中指定元数据
    class sum_numbers(Tool):
        tool_name = 'sum'
        description = '计算两个数的和'
        params_dict = {
            "a": ("number", "第一个数"),
            "b": ("number", "第二个数")
        }

        def execute(self, a: float, b: float) -> float:
            return a + b

    # 写法 2. 也可以在 __init__ 中指定元数据
    class sum_numbers(Tool):
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

    # 执行
    tool = sum_numbers()
    result = tool(1, 2)   # 返回 3
    ```
    """
    def __init__(self, tool_name: str, description: str, params_dict: Dict[str, Tuple[str, str]]):
        """初始化工具"""
        cls = self.__class__
        self.tool_name = tool_name if tool_name is not None else getattr(cls, 'tool_name', None)
        self.description = description if description is not None else getattr(cls, 'description', None)
        self.params_dict = params_dict if params_dict is not None else getattr(cls, 'params_dict', None)
        self.tool_available = True

        if self.tool_name is None or self.description is None or self.params_dict is None:
            logger.error(f"[创建工具] 工具 {tool_name} 定义不完整")
            self.tool_available = False

    def get_tool_defined(self) -> Dict[str, Any]:
        """获取工具定义"""
        if not self.assert_tool():
            return {}
        return create_tool_defined(self.tool_name, self.description, self.params_dict)   # type: ignore

    def get_tool_name(self) -> str:
        """获取工具名称"""
        return self.tool_name or "unknown_tool"

    def execute(self, *input, **kwargs) -> Any:
        """执行工具"""
        return None

    def assert_tool(self) -> bool:
        """断言工具是否可用"""
        return self.tool_available

    def __call__(self, *input, **kwargs):
        """
        使工具实例可被调用
        使用方式: result = tool_instance(...)
        """
        result = self.execute(*input, **kwargs)
        return result
    
class AsyncTool:
    """
    异步工具基类; 所有异步工具都应当继承自该类

    定义工具时, 应当在子类中通过类属性指定元数据:
        - tool_name: 工具名称
        - description: 工具描述
        - params_dict: 参数字典, 格式如 {"param1": ("类型", "描述"), ...}
    然后实现 async execute 方法

    使用示例:
    ``` python
    # 写法 1. 直接在类属性中指定元数据
    class AsyncSum(AsyncTool):
        tool_name = 'async_sum'
        description = '异步计算两个数的和'
        params_dict = {
            "a": ("number", "第一个数"),
            "b": ("number", "第二个数")
        }

        async def execute(self, a: float, b: float) -> float:
            return a + b

    # 写法 2. 也可以在 __init__ 中指定元数据
    class AsyncSearch(AsyncTool):
        def __init__(
            self,
            tool_name = 'search',
            description = '搜索网络信息',
            params_dict = {
                "query": ("string", "搜索关键词"),
            }):
            super().__init__(tool_name, description, params_dict)

        async def execute(self, query: str) -> str:
            return f"搜索结果：{query}"

    # 执行
    async def main():
        tool = AsyncSum()
        result = await tool(1, 2)   # 这里需要 await
        print(result)               # 返回 3

    asyncio.run(main())
    ```
    """
    def __init__(self, tool_name: str, description: str, params_dict: Dict[str, Tuple[str, str]]):
        """初始化工具"""
        cls = self.__class__
        self.tool_name = tool_name if tool_name is not None else getattr(cls, 'tool_name', None)
        self.description = description if description is not None else getattr(cls, 'description', None)
        self.params_dict = params_dict if params_dict is not None else getattr(cls, 'params_dict', None)
        self.tool_available = True

        if self.tool_name is None or self.description is None or self.params_dict is None:
            logger.error(f"[创建工具] 工具 {tool_name} 定义不完整")
            self.tool_available = False

    def get_tool_defined(self) -> Dict[str, Any]:
        """获取工具定义 (同步方法, 仅读取元数据)"""
        if not self.assert_tool():
            return {}
        return create_tool_defined(self.tool_name, self.description, self.params_dict)   # type: ignore

    def get_tool_name(self) -> str:
        """获取工具名称"""
        return self.tool_name or "unknown_tool"

    async def execute(self, *args, **kwargs) -> Any:
        """
        执行工具 (异步)
        子类必须重写此方法, 并使用 async def
        """
        return None

    def assert_tool(self) -> bool:
        """断言工具是否可用"""
        return self.tool_available

    async def __call__(self, *args, **kwargs):
        """
        使工具实例可被调用
        使用方式: result = await tool_instance(...)
        """
        if not self.assert_tool():
            raise RuntimeError(f"工具 {self.get_tool_name()} 不可用")
        
        result = await self.execute(*args, **kwargs)
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
        if not tool.assert_tool():
            logger.error(f"[注册工具] 工具 {tool.get_tool_name()} 定义不完整, 无法注册")

        else:
            self.tools[tool.get_tool_name()] = tool
            logger.info(f"[注册工具] 工具 {tool.get_tool_name()} 已注册")

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
        try:
            return tool(**arguments)
        except Exception as e:
            logger.error(f"[执行工具] 工具 {tool_name} 执行出错：{str(e)}")
            return {"error": f"工具执行异常：{str(e)}"}

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

class AsyncToolsManager:
    """异步工具管理器, 用于注册和执行异步工具"""
    def __init__(self):
        self.tools: Dict[str, AsyncTool] = {}

    def register_tool(self, tool: AsyncTool):
        """注册异步工具
        
        参数:
        - tool: 要注册的异步工具实例, 必须是 AsyncTool 类的子类
        """
        if not tool.assert_tool():
            logger.error(f"[注册异步工具] 工具 {tool.get_tool_name()} 定义不完整, 无法注册")
        else:
            self.tools[tool.get_tool_name()] = tool
            logger.info(f"[注册异步工具] 工具 {tool.get_tool_name()} 已注册")

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        执行指定工具 (异步方法)
        
        参数:
        - tool_name: 工具名称
        - arguments: 参数字典

        返回:
        - 工具执行结果，如果工具不存在返回错误字典
        """
        if tool_name not in self.tools:
            return {"error": f"工具 {tool_name} 不存在"}

        tool = self.tools[tool_name]
        try:   # 异步调用工具
            return await tool(**arguments)
        except Exception as e:
            logger.error(f"[执行工具] 工具 {tool_name} 执行出错：{str(e)}")
            return {"error": f"工具执行异常：{str(e)}"}

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
    
    async def execute_tool_call(self, call_info: Dict[str, Any]) -> Any:
        """执行工具调用流程 (异步方法)

        参数:
        - call_info: 工具调用信息, 格式为 {"name": "工具名", "arguments": {"param1": "值1", "param2": "值2", ...}}

        返回:
        - 元组 (工具调用消息, 工具调用的返回结果)
        """
        tool_name, arguments = self.get_call_info(call_info)
        tool_message = self.create_call_message(call_info)
        tool_result = await self.execute_tool(tool_name, arguments)
        return tool_message, tool_result
