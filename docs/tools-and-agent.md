# 工具与 Agent

## 同步工具

工具继承 `Tool`, 通过类属性声明元数据, 实现 `execute()`。

```python
from satrap import Tool, ToolsManager


class WeatherTool(Tool):
    tool_name = "weather"
    description = "查询城市天气"
    params_dict = {
        "city": ("string", "城市名称"),
    }

    def execute(self, city: str):
        return {"city": city, "weather": "晴"}


manager = ToolsManager()
manager.register_tool(WeatherTool())

print(manager.get_tools_definitions())
print(manager.execute_tool("weather", {"city": "北京"}))
```

`params_dict` 的类型值会直接写入 OpenAI function calling schema, 常用值包括 `string`, `number`, `boolean`, `array`, `object`。

## 异步工具

```python
import asyncio

from satrap import AsyncTool, AsyncToolsManager


class AsyncWeatherTool(AsyncTool):
    tool_name = "weather"
    description = "异步查询城市天气"
    params_dict = {
        "city": ("string", "城市名称"),
    }

    async def execute(self, city: str):
        return {"city": city, "weather": "晴"}


async def main():
    manager = AsyncToolsManager()
    manager.register_tool(AsyncWeatherTool())
    result = await manager.execute_tool("weather", {"city": "北京"})
    print(result)


asyncio.run(main())
```

## 启用, 禁用和注销

工具可以临时禁用, 禁用后不会出现在 `get_tools_definitions()` 中, 也不能执行。

```python
manager.disable_tool("weather")
print(manager.is_tool_enabled("weather"))

result = manager.execute_tool("weather", {"city": "北京"})
print(result)

manager.enable_tool("weather")
manager.unregister_tool("weather")
```

结构化错误示例:

```python
{
    "error": "工具 weather 已禁用",
    "ok": False,
    "error_type": "disabled",
    "tool_name": "weather",
}
```

当前错误类型:

| `error_type` | 含义 |
| --- | --- |
| `not_found` | 工具不存在 |
| `disabled` | 工具已禁用 |
| `invalid_arguments` | 参数不是字典 |
| `execution_error` | 工具内部执行异常 |
| `invalid_tool_call` | 模型返回的 tool call 格式不合法 |

## 手动处理 Tool Call

```python
response = llm.call(
    ctx.get_context(),
    tools=manager.get_tools_definitions(),
)

if response and response.type == "tools_call":
    tool_messages = []
    tool_results = []

    for call_info in response.tool_calls:
        tool_message, tool_result = manager.execute_tool_call(call_info)
        tool_messages.append(tool_message)
        tool_results.append(tool_result)

    ctx.add_tool_call_flow(
        response.content,
        tool_messages,
        tool_results,
    )

    final_response = llm.call(ctx.get_context())
    print(final_response.content if final_response else "模型调用失败")
```

## ModelWorkflowFramework

`ModelWorkflowFramework.full_agent()` 封装完整工具调用循环。

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
    system_prompt="你是一个会按需调用工具的助手",
)

answer = agent.full_agent(
    "如果需要工具就调用工具, 然后给我最终答案",
    callback=True,
    max_iterations=10,
)
print(answer)
```

`callback=True` 时, workflow 会通过 `content_callback` 输出中间内容。`max_iterations` 用来限制连续工具调用轮数。

## 自定义 Workflow

```python
from satrap import ModelWorkflowFramework


class MyWorkflow(ModelWorkflowFramework):
    def forward(self, user_input: str) -> str:
        self.ctx.add_user_message(user_input)
        response = self.llm.call(
            self.ctx.get_context(),
            tools=self.tools_manager.get_tools_definitions(),
        )
        if not response:
            return "模型调用失败"

        context, success = self.agent_executor(response, callback=True)
        if not success:
            return "执行失败"

        return self.get_bot_message(context)
```

## Sub-Agent 扩展

`satrap.expend.agent` 提供 `SubAgent` 和 `AsyncSubAgent`, 可以把任务列表分派给子 Agent workflow。它适合需要拆分任务, 保持顺序汇总结果的场景。

```python
from satrap.expend.agent import SubAgent
```

使用前需要准备子 Agent 使用的 LLM 和 ToolsManager。具体行为可以参考 `tests/test_agent_sub_agent.py`。
