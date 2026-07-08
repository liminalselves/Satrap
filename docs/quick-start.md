# 快速开始

## 1. 安装

在项目根目录执行:

```bash
pip install -e .
```

需要管理面板时安装:

```bash
pip install -e .[admin]
```

需要向量检索能力时安装:

```bash
pip install -e .[vector]
```

## 2. 文本调用

```python
from satrap import ContextManager, LLM

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="your-model",
)

ctx = ContextManager(conversation_id="quickstart")
ctx.reset_system_prompt("你是一个简洁可靠的助手")
ctx.add_user_message("用一句话介绍 Satrap")

response = llm.call(ctx.get_context())
print(response.content if response else "模型调用失败")
```

`LLM.call()` 返回 `LLMCallResponse`。常见字段:

| 字段 | 说明 |
| --- | --- |
| `type` | `message` 或 `tools_call` |
| `content` | 模型文本内容 |
| `tool_calls` | 模型请求调用的工具列表 |
| `thinking` | 兼容部分供应商的思考字段 |

## 3. 异步调用

```python
import asyncio

from satrap import AsyncContextManager, AsyncLLM


async def main():
    llm = AsyncLLM(
        api_key="your-api-key",
        base_url="https://api.example.com/v1",
        model="your-model",
    )
    ctx = AsyncContextManager(conversation_id="async-quickstart")
    await ctx.initialize()
    await ctx.add_user_message("你好")

    response = await llm.call(ctx.get_context())
    print(response.content if response else "模型调用失败")


asyncio.run(main())
```

## 4. 图片输入

`img_urls` 支持远程 URL, data URL 和本地图片路径。本地图片会在发送前转为 data URL, 原文件不会被修改。

```python
from satrap import ContextManager, LLM

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="vision-model",
)

ctx = ContextManager(conversation_id="vision-demo")
ctx.add_user_message(
    "描述这张图片中的主体和背景",
    img_urls=["./images/example.jpg"],
)

response = llm.call(ctx.get_context())
print(response.content if response else "模型调用失败")
```

也可以直接在调用时追加图片:

```python
messages = [{"role": "user", "content": "这张图里有什么?"}]
response = llm.call(messages, img_urls=["https://example.com/image.png"])
```

## 5. 最小工具

```python
from satrap import Tool, ToolsManager


class AddTool(Tool):
    tool_name = "add"
    description = "计算两个数字的和"
    params_dict = {
        "a": ("number", "第一个数字"),
        "b": ("number", "第二个数字"),
    }

    def execute(self, a: float, b: float) -> float:
        return a + b


tools = ToolsManager()
tools.register_tool(AddTool())

print(tools.get_tools_definitions())
print(tools.execute_tool("add", {"a": 1, "b": 2}))
```

## 6. 最小 Agent

```python
from satrap import LLM, ModelWorkflowFramework, ToolsManager

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="your-model",
)
tools = ToolsManager()

agent = ModelWorkflowFramework(
    llm=llm,
    context_id="agent-demo",
    tools_manager=tools,
)

answer = agent.full_agent("你好, 请简单介绍你自己", callback=True, max_iterations=10)
print(answer)
```
