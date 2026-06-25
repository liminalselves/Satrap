# Satrap 使用指南

这份指南面向想直接上手 Satrap 的开发者, 覆盖安装, 模型配置, LLM 调用, 图片输入, 上下文管理, 工具调用, Agent workflow, CLI 和常见问题

## 1. 安装

在项目根目录执行:

```bash
pip install -e .
```

如果需要管理面板:

```bash
pip install -e .[admin]
```

如果需要向量数据库能力:

```bash
pip install -e .[vector]
```

如果希望一次安装全部可选依赖:

```bash
pip install -e .[all]
```

传统方式也可用:

```bash
pip install -r requirements.txt
```

## 2. 模型配置

Satrap 的 LLM 调用兼容 OpenAI Chat Completions 格式。只要服务提供 `base_url`, `api_key`, `model`, 就可以接入

```python
from satrap import LLM

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="your-model",
)
```

如果拿到的是完整 Chat Completions endpoint, 例如:

```text
https://api.example.com/v1/chat/completions
```

视觉处理模块会在多模态调用路径中归一化为 OpenAI client 需要的:

```text
https://api.example.com/v1
```

## 3. 文本调用

推荐使用 `ContextManager` 管理上下文:

```python
from satrap import ContextManager, LLM

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="your-model",
)

ctx = ContextManager(conversation_id="quickstart")
ctx.add_system_message("你是一个简洁可靠的助手")
ctx.add_user_message("用一句话介绍 Satrap")

response = llm.call(ctx.get_context())

if response:
    print(response.content)
else:
    print("模型调用失败")
```

异步调用:

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
    await ctx.add_user_message("你好")

    response = await llm.call(ctx.get_context())
    print(response.content if response else "模型调用失败")


asyncio.run(main())
```

## 4. 图片输入

图片可以通过 `img_urls` 传入。支持:

- `http://` 或 `https://` 图片 URL
- `data:image/...;base64,...`
- 本地图片路径

本地大图会在发送模型前压缩为 data URL, 原图文件不会被修改

```python
from satrap import ContextManager, LLM

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="vision-model",
)

ctx = ContextManager(conversation_id="vision-demo")
ctx.add_user_message(
    "请描述这张图片中的主体和背景",
    img_urls=["./images/example.jpg"],
)

response = llm.call(ctx.get_context())
print(response.content if response else "模型调用失败")
```

也可以直接在 `LLM.call()` 中追加图片:

```python
messages = [{"role": "user", "content": "这张图里有什么?"}]
response = llm.call(messages, img_urls=["https://example.com/image.png"])
```

## 5. 上下文管理

`ContextManager` 会保存对话历史, 并支持 SQLite 持久化。普通文本消息仍保存为字符串, 带图片的消息会保存为 OpenAI-compatible 多模态 content list

```python
from satrap import ContextManager

ctx = ContextManager(conversation_id="context-demo")

ctx.add_system_message("你是一个项目助手")
ctx.add_user_message("记住这个需求: 优先输出中文")
ctx.add_bot_message("已记住")

messages = ctx.get_context()
print(messages)
```

带图片的用户消息:

```python
ctx.add_user_message(
    "分析这张 UI 截图",
    img_urls=["./screenshots/home.png"],
)
```

## 6. 工具定义与执行

同步工具继承 `Tool`, 实现 `execute()`:

```python
from satrap import Tool, ToolsManager


class WeatherTool(Tool):
    tool_name = "weather"
    description = "查询城市天气"
    params_dict = {
        "city": ("string", "城市名"),
    }

    def execute(self, city: str):
        return {"city": city, "weather": "晴"}


manager = ToolsManager()
manager.register_tool(WeatherTool())

print(manager.get_tools_definitions())
print(manager.execute_tool("weather", {"city": "北京"}))
```

异步工具继承 `AsyncTool`:

```python
import asyncio

from satrap import AsyncTool, AsyncToolsManager


class AsyncWeatherTool(AsyncTool):
    tool_name = "weather"
    description = "异步查询城市天气"
    params_dict = {
        "city": ("string", "城市名"),
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

## 7. 工具启用, 禁用与错误结果

工具可以被禁用但不注销。禁用后仍保留在 `manager.tools` 中, 但不会暴露给模型, 也不能执行

```python
manager.disable_tool("weather")
print(manager.get_tools_definitions())

result = manager.execute_tool("weather", {"city": "北京"})
print(result)

manager.enable_tool("weather")
```

失败时返回结构化错误:

```python
{
    "error": "工具 weather 已禁用",
    "ok": False,
    "error_type": "disabled",
    "tool_name": "weather",
}
```

当前错误类型:

| error_type | 含义 |
| --- | --- |
| `not_found` | 工具不存在 |
| `disabled` | 工具已禁用 |
| `invalid_arguments` | 参数不是字典 |
| `execution_error` | 工具内部执行抛异常 |
| `invalid_tool_call` | 模型返回的 tool call 格式不合法 |

成功结果不包裹, 会保持工具自己的原始返回值

## 8. Tool call 与 LLM 联动

模型返回工具调用后, 可以通过 `execute_tool_call()` 执行, 并把结果写回上下文:

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
        response.thinking,
    )

    final_response = llm.call(ctx.get_context())
    print(final_response.content if final_response else "模型调用失败")
```

如果工具执行失败, 错误结果会作为普通 tool result 写入上下文, 由模型继续处理

## 9. Agent workflow

`ModelWorkflowFramework.full_agent()` 封装了“用户输入 -> 模型请求工具 -> 执行工具 -> 模型最终回复”的完整流程

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

answer = agent.full_agent(
    "如果需要工具就调用工具, 然后给我最终答案",
    callback=True,
    max_iterations=10,
)
print(answer)
```

异步 workflow:

```python
import asyncio

from satrap import AsyncLLM, AsyncModelWorkflowFramework, AsyncToolsManager


async def main():
    llm = AsyncLLM(
        api_key="your-api-key",
        base_url="https://api.example.com/v1",
        model="your-model",
    )
    tools = AsyncToolsManager()
    agent = await AsyncModelWorkflowFramework.create(
        llm=llm,
        context_id="async-agent-demo",
        tools_manager=tools,
    )

    answer = await agent.full_agent("你好", callback=True, max_iterations=10)
    print(answer)


asyncio.run(main())
```

## 10. Session 基础用法

`Session` 适合把 LLM, 上下文和工具管理器组合成可复用会话类

```python
from satrap import Session


class DemoSession(Session):
    def forward(self, user_input: str) -> str:
        return self.full_agent(user_input)
```

具体项目中可以通过 Session 注册和 CLI 管理会话类

## 11. CLI

安装后可使用:

```bash
satrap --help
```

常用命令:

```bash
satrap run
satrap status
satrap stop
satrap restart
satrap config show
satrap config init
satrap model list
satrap session list
satrap platform list
```

如果命令入口还没有安装, 也可以使用:

```bash
python -m satrap.main --help
```

## 12. 管理面板

安装管理面板依赖:

```bash
pip install -e .[admin]
```

启动:

```bash
streamlit run satrap/admin.py
```

管理面板当前包含后端状态, 模型配置, 会话管理, 平台状态, 日志监控和系统设置等页面

## 13. 常见问题

### 缺少 colorlog

如果运行时报:

```text
ModuleNotFoundError: No module named 'colorlog'
```

请确认当前 Python 环境已安装项目依赖:

```bash
python -m pip install -e .
python -c "import colorlog; print(colorlog.__file__)"
```

### API key 如何配置

直接创建 `LLM` / `AsyncLLM` 时可以传入:

```python
LLM(api_key="your-api-key", base_url="https://api.example.com/v1", model="your-model")
```

如果使用 CLI 和后端管理能力, 推荐通过配置文件和 `satrap config` / `satrap model` 管理

### 工具调用失败会怎样

工具调用失败不会默认中断 agent 流程。失败信息会作为 tool result 写入上下文, 模型可以看到错误并继续回复

### 图片太大会怎样

本地图片会在发送模型前自动压缩为 JPEG data URL。默认最长边为 1600px, 目标大小小于 4MB。原始图片文件不会被修改

### 可选依赖怎么选

- 只用核心 LLM, 上下文和工具能力: `pip install -e .`
- 需要 Streamlit 管理面板: `pip install -e .[admin]`
- 需要 faiss 向量数据库: `pip install -e .[vector]`
- 想一次装齐: `pip install -e .[all]`

## 14. 下一步

建议先完成三个最小闭环:

1. 使用 `LLM` 完成一次文本调用
2. 注册一个自定义 `Tool`, 并让模型调用它
3. 使用 `full_agent()` 跑通完整工具调用流程
