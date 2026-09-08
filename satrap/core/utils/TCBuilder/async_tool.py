from typing import Any
from .tool_base import _ToolBase


class AsyncTool(_ToolBase):
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
            return f"搜索结果: {query}"

    # 执行
    async def main():
        tool = AsyncSum()
        result = await tool(1, 2)   # 这里需要 await
        print(result)               # 返回 3

    asyncio.run(main())
    ```
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        """
        执行工具 (异步)
        子类必须重写此方法, 并使用 async def

        参数:
        - args: 额外位置参数
        - kwargs: 额外关键字参数

        返回:
        - Any: 执行工具 (异步)
        """
        return None

    async def __call__(self, *args: Any, **kwargs: Any):
        """
        使工具实例可被调用
        使用方式: result = await tool_instance(...)

        参数:
        - args: 额外位置参数
        - kwargs: 额外关键字参数

        返回:
        - 使工具实例可被调用
        """
        if not self.assert_tool():
            raise RuntimeError(f"工具 {self.get_tool_name()} 不可用")

        result = await self.execute(*args, **kwargs)
        return result
