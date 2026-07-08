# 核心 API

## 公开导出

`satrap.__init__` 公开了最常用的类:

```python
from satrap import (
    AsyncContextManager,
    AsyncLLM,
    AsyncModelWorkflowFramework,
    AsyncSession,
    AsyncTool,
    AsyncToolsManager,
    ContextManager,
    LLM,
    Logger,
    ModelWorkflowFramework,
    Session,
    Tool,
    ToolsManager,
)
```

## LLM / AsyncLLM

`LLM` 和 `AsyncLLM` 封装 OpenAI-compatible Chat Completions API。

常用初始化参数:

| 参数 | 说明 |
| --- | --- |
| `api_key` | API key |
| `base_url` | API base URL, 可传 `/v1` 或完整 `/v1/chat/completions` |
| `model` | 模型名称 |
| `temperature` | 采样温度, 默认 `0.7` |
| `top_p` | top-p, 默认 `0.95` |
| `max_tokens` | 最大输出 token, 默认 `1000` |
| `timeout` | 请求超时秒数, 默认 `60` |
| `suppress_error` | 是否捕获异常并返回空结果, 默认 `True` |
| `return_false` | 出错时是否返回 `False` |
| `reasoning_body` | 发送 thinking/reasoning 请求时追加到 `extra_body` 的内容 |
| `thinking_field_name` | 上下文中思考字段名称, 默认 `reasoning_content` |

常用方法:

```python
llm.chat(messages)
llm.stream_chat(messages)
llm.structured_output(messages, format={"name": "str", "score": "int"})
llm.call(messages, tools=tools, img_urls=["./a.png"])
```

推荐在 Agent 场景使用 `call()`, 因为它会返回 `LLMCallResponse`, 能区分普通消息和工具调用。

## ContextManager

`ContextManager` 使用 SQLite 保存对话上下文, 默认数据库为 `.satrap/chat_history.db`。

```python
from satrap import ContextManager

ctx = ContextManager(conversation_id="user-1")
ctx.reset_system_prompt("你是一个中文助手")
ctx.add_user_message("记住: 优先输出中文")
ctx.add_bot_message("已记住")

messages = ctx.get_context()
model_messages = ctx.get_model_context()
```

常用方法:

| 方法 | 说明 |
| --- | --- |
| `load_context()` | 从数据库加载当前对话 |
| `save_context()` | 写入数据库 |
| `get_context()` | 返回完整上下文 |
| `get_model_context()` | 返回应用截断策略后的上下文副本 |
| `add_user_message()` | 添加用户消息, 可带图片 |
| `add_bot_message()` | 添加助手消息 |
| `add_tool_message()` | 添加工具结果消息 |
| `add_tool_call_flow()` | 添加一次完整工具调用流 |
| `del_context()` | 清空非 system 消息 |
| `estimate_token()` | 估算当前 token 数 |
| `export_json()` | 导出上下文 |

`AsyncContextManager` 提供异步版本, 创建后先执行 `await ctx.initialize()`。

## 多模态消息

图片输入最终会转为 OpenAI-compatible content list:

```python
ctx.add_user_message(
    "分析这张 UI 截图",
    img_urls=["./screenshots/home.png"],
)
```

本地图片会自动转成 data URL。默认最长边为 1600px, 目标大小小于 4MB。

## Embedding / ReRank

底层模块提供 `Embedding`, `AsyncEmbedding`, `ReRank`, `AsyncReRank`, 用于 OpenAI-compatible embedding 和 rerank 服务。它们没有在顶层 `satrap` 直接导出, 可以从具体模块导入:

```python
from satrap.core.APICall.EmbedCall import Embedding
from satrap.core.APICall.ReRankCall import ReRank
```

## 消息组件

`satrap.core.components.message` 定义了跨平台消息组件, 包括:

- `Plain`
- `Image`
- `Record`
- `Video`
- `File`
- `At`
- `AtAll`
- `Reply`
- `Forward`
- `Node`
- `Nodes`
- `Json`
- `Unknown`

平台适配器会在原始平台消息和这些组件之间转换, 上层 Session 可以尽量处理统一结构。
