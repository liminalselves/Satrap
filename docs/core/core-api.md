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
    LLMCallResponse,
    LLMCallStreamEvent,
    Logger,
    MCPClient,
    MCPToolAdapter,
    ModelWorkflowFramework,
    Session,
    Skill,
    SkillsManager,
    SkillTool,
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
| `thinking_field_name` | 上下文中思考字段名称, 默认 `reasoning_content` |
| `thinking_fields` | 该模型需要的思考请求字段列表, 如 `["reasoning_effort", "thinking.type"]` |
| `thinking_levels` | 前端允许选择的思考强度, 可选 `low` / `medium` / `high` / `xhigh` / `max` / `ultra` |
| `omit_none_thinking_fields` | 关闭思考时是否省略值为 `none` 的字段, 默认 `False` |

`thinking` 参数为字符串思考强度: `off` 表示不请求思考, 其余可用强度由模型配置的 `thinking_levels` 决定。请求时由 `thinking_fields` 构造 `extra_body` —— `reasoning_effort` / `thinking_level` 直接把强度值传给模型, `thinking.type` / `enable_thinking` 只区分开关。对于关闭时不接受 `reasoning_effort: none` 的供应商, 设置 `omit_none_thinking_fields=True` 即可省略值为 `none` 的字段, 同时保留 `disabled` 或 `false` 等显式关闭字段。

常用方法:

```python
llm.chat(messages)
llm.stream_chat(messages)
llm.structured_output(messages, format={"name": "str", "score": "int"})
llm.call(messages, tools=tools, img_urls=["./a.png"])
llm.stream_call(messages, thinking="medium", tools=tools)
```

推荐在 Agent 场景使用 `call()`, 因为它会返回 `LLMCallResponse`, 能区分普通消息和工具调用。

`stream_call()` 和 `AsyncLLM.stream_call()` 会把一次模型请求转换为统一事件流。同步版本使用 `for`, 异步版本使用 `async for`。`thinking` 请求模型返回思考增量, 最终是否有 `thinking_delta` 取决于模型和供应商是否支持该字段:

```python
messages = [{"role": "user", "content": "你好"}]
response = None
for event in llm.stream_call(messages, thinking="high"):
    if event.kind == "thinking_delta":
        print(event.delta, end="", flush=True)
    elif event.kind == "content_delta":
        print(event.delta, end="", flush=True)
    elif event.kind == "done":
        response = event.response
    elif event.kind == "error":
        print(f"\n流式调用失败: {event.error}")
```

`LLMCallStreamEvent.kind` 常见值如下:

| 类型 | 内容 |
| --- | --- |
| `content_delta` | 普通回答文本增量, 位于 `delta` |
| `thinking_delta` | 思考或 reasoning 文本增量, 位于 `delta` |
| `tool_call_delta` | 工具调用增量, 位于 `tool_call` |
| `done` | 请求结束, 完整结果位于 `response` |
| `error` | 请求错误, 详情位于 `error` |

流式方法会在内部拼接工具参数, `done` 事件中的 `response` 仍然是完整的 `LLMCallResponse`, 调用方不需要自行合并工具调用片段。

## ContextManager

`ContextManager` 使用 SQLite 保存对话上下文。平台后端会注入当前平台实例的 `platform.db`; 独立调用默认使用 `local` 平台数据库。

```python
from satrap import ContextManager

ctx = ContextManager(conversation_id="user-1")
ctx.reset_system_prompt("你是一个中文助手")
ctx.add_user_message("记住: 优先输出中文")
ctx.add_bot_message("已记住")

messages = ctx.get_context()
model_messages = ctx.get_model_context()
```

### 上下文预算与滞回截断

`ContextManager` 将总上下文窗口 `max_context` 按比例分离为历史预算与输出预算, 并采用滞回截断策略避免频繁重写前缀 (保持服务器 prefix cache 稳定):

```python
ctx = ContextManager(
    conversation_id="user-1",
    max_context=128000,       # 总上下文窗口
    history_ratio=0.7,        # 历史预算比例: 历史预算 = max_context × 0.7
    context_threshold=0.8,    # 触发线: 历史预算 × 0.8, 超过才触发截断
    truncation_floor=0.4,     # 截断底线: 历史预算 × 0.4, 截断后降至此线以下
)
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `max_context` | `128000` | 总上下文窗口 token 数 |
| `history_ratio` | `0.7` | 历史上下文比例, 输出预算 = `max_context × (1 - history_ratio)` |
| `context_threshold` | `0.8` | 触发线 (占历史预算比例), 超过才截断 |
| `truncation_floor` | `0.4` | 截断底线 (占历史预算比例), 截断目标 |
| `exceed_process` | `"sliding"` | 超限处理: `sliding` 滑动窗口 / `mid_truncate` 中间截断 |

滞回区间 (`truncation_floor` ~ `context_threshold`) 内前缀保持稳定, 只有超过触发线才截断到底线, 减少前缀变动频率。

### 总结压缩

`summarize_and_compress()` 将较早的对话轮次调用 LLM 总结为一段摘要, 删除原文并注入 system prompt, 用于长对话的上下文压缩:

```python
summary = ctx.summarize_and_compress(llm, keep_recent_turns=4)
```

- `llm`: 用于总结的 `LLM` 实例 (多模态图片会先投影为 `[图片]` 占位符, 无需多模态能力)
- `keep_recent_turns`: 保留最近的对话轮数 (不总结不删除)
- 返回总结文本; 已有摘要区块时会调用 LLM 合并为一段

`AsyncContextManager` 提供对应的 `await ctx.summarize_and_compress(llm, keep_recent_turns)` (传入 `AsyncLLM`)。

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
| `summarize_and_compress()` | 总结压缩较早对话轮次, 摘要注入 system prompt |
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

## MCP 客户端

`MCPClient` / `MCPToolAdapter` (位于 `satrap.core.utils.mcp`) 是 MCP 协议接入基础设施, 与 `ToolsManager` 同层。`MCPClient` 连接 MCP Server (stdio 子进程或 streamable HTTP), 把远端工具包装为 `AsyncTool` 注册进 `AsyncToolsManager`, 模型即可通过 function calling 直接调用:

```python
import asyncio

from satrap import AsyncToolsManager, MCPClient


async def main():
    # stdio 传输 (本地子进程)
    mcp = MCPClient(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "."],
        name="fs",   # 工具名自动加前缀, 避免多来源冲突
    )
    # 或 streamable HTTP 传输
    # mcp = MCPClient(url="https://example.com/mcp", headers={"Authorization": "Bearer xxx"})

    tools = AsyncToolsManager()
    await mcp.register_tools(tools)   # 注册所有远端工具

    _, result = await tools.execute_tool_call({"name": "fs_read_file", "arguments": {"path": "/tmp/a.txt"}})

    await mcp.close()   # 断开连接并自动注销工具


asyncio.run(main())
```

`MCPClient` 参数:

| 参数 | 说明 |
| --- | --- |
| `command` / `args` / `env` | stdio 传输的启动命令, 参数与环境变量 |
| `url` / `headers` | streamable HTTP 传输的地址与请求头 |
| `name` | 客户端名称, 默认工具名前缀 |
| `tool_prefix` | 自定义工具名前缀, 传空字符串禁用前缀 |

MCP 远端工具的 JSON Schema 会原样透传为 OpenAI 参数定义, 保留可选参数, 枚举和嵌套结构。`MCPToolAdapter` 也可直接手动构造并注册:

```python
from satrap.core.utils.mcp import MCPToolAdapter

tools.register_tool(MCPToolAdapter(session, mcp_tool, name_prefix="fs"))
```

反向导出本地工具为 MCP Server (供其他 MCP 客户端调用) 使用 `MCPServerExporter`:

```python
from satrap import ToolsManager
from satrap.core.utils.mcp import MCPServerExporter

exporter = MCPServerExporter(tools, name="satrap")
exporter.run(transport="stdio")   # 阻塞运行, 支持 stdio / sse / streamable-http
```

## 技能 (Skill)

`Skill` / `SkillsManager` / `SkillTool` (位于 `satrap.core.utils.skills`) 是技能基础设施: 技能 = 指令文本 + 关联工具列表 (+ 可选自带工具与 MCP 客户端)。激活技能时指令注入系统提示词, 关联工具注册并启用; 反激活时自动剥离指令并禁用工具。

技能以文件夹为单位组织, 每个技能一个目录:

``` text
skills/
└── coding_agent/          # 技能文件夹 (front matter 未声明 name 时, 文件夹名即技能名)
    ├── skill.md           # 技能指令 (Markdown, 支持 YAML front matter)
    ├── tools.py           # 可选: 自带工具与 MCP 客户端
    └── meta.yaml          # 可选: 作者, 版本等信息 (加载进 skill.meta)
```

`skill.md` 的 front matter 声明 `name`, `description` 与关联的 `tools` (激活时启用的外部工具名):

```markdown
---
name: coding_agent
description: 代码助手
tools:
  - code_sandbox
  - search
---
<技能指令正文...>
```

`tools.py` (可选) 约定:
- `get_tools()`: 返回工具实例列表 (构造函数需要参数的场景)
- `get_mcp_clients()`: 返回 MCPClient 实例列表 (`activate_async` 时自动连接注册)
- 未定义 `get_tools` 时, 模块内定义的 `Tool` / `AsyncTool` 子类会被自动实例化收集
- 自带工具名自动并入技能工具列表, 无需在 front matter 重复声明

也兼容旧式单文件技能 (直接放置 .md 文件)。

```python
from satrap import SkillsManager

skills = SkillsManager()   # 默认扫官方预设 + 用户目录 .satrap/skills
skills.scan()   # 扫描并加载全部技能 (文件夹式 + 单文件式)

skills.activate("coding-agent", workflow)          # 同步 workflow: 注入指令 + 注册/启用工具
await skills.activate_async("coding-agent", workflow)   # 异步 workflow (额外自动连接自带 MCP 客户端)

skills.deactivate("coding-agent", workflow)        # 取消激活
await skills.deactivate_async("coding-agent", workflow)   # 异步取消 (额外关闭自带 MCP 连接)
```

扫描目录分两层: 官方预设 (`satrap/expend/skills`, 只读基线) + 用户技能目录 (默认 `.satrap/skills`, 构造参数 `skills_dir` 可覆盖, `include_preset=False` 可关闭官方合并)。

同名技能冲突规则:

- 技能身份识别符 `satrap-skill-id` (meta.yaml 中声明): 同名技能携带不同 id 时共存不冲突, 可用 id 或名称激活
- 无 id 的旧式技能与官方同名时官方优先 (用户想定制官方技能应使用不同 id)

常用方法:

| 方法 | 说明 |
| --- | --- |
| `scan()` | 扫描技能 (默认官方预设 + 用户目录; 传参时仅扫指定目录) |
| `load_skill(name, file_path)` | 加载单个技能文件 |
| `get_skill()` / `has_skill()` / `list_skills()` | 查询已加载技能 |
| `activate()` / `deactivate()` | 同步装配 / 卸载技能 |
| `activate_async()` / `deactivate_async()` | 异步装配 / 卸载技能 (含自带 MCP 连接/关闭) |

`SKILLS_PRESET_DIR` 指向内置示例技能目录 (`satrap/expend/skills`, 内含 `coding-agent` 与 `web-research` 两个技能文件夹), 可复制到自己的技能目录使用。

技能支持动态加载路线: 注册 `SkillTool` 后模型可按需调用 `load_skill` 获取技能指令:

```python
from satrap import SkillTool

tools.register_tool(SkillTool(skills))
```

技能引用的工具可以是 MCP 注册的远端工具, 二者共享同一个 `ToolsManager` 注册表。典型组合: 先 `await mcp.register_tools(tools)`, 再 `skills.activate("coding_agent", workflow)` 把远端工具纳入技能工作流。

## Embedding / ReRank

底层模块提供 `Embedding`, `AsyncEmbedding`, `ReRank`, `AsyncReRank`, 用于 OpenAI-compatible embedding 和 rerank 服务。它们没有在顶层 `satrap` 直接导出, 可以从具体模块导入:

```python
from satrap.core.APICall.EmbedCall import Embedding
from satrap.core.APICall.ReRankCall import ReRank
```

`embed()` 的行为契约 (同步 / 异步一致):

- 空列表输入返回 `[]` (单文本输入返回该文本的向量);
- 输入按 `max_batch_size` (默认 100, `<= 0` 构造时抛 `ValueError`) 分批请求; 单批 API 失败不中止整个调用, 失败批的项以 `[]` 按输入位置占位, 继续后续批次 (`suppress_error=False` 时改为抛出);
- 跨批次向量维度不一致的项置为 `[]` 占位 (`_align_embedding_dimensions`);
- `return_false=True` 时, 任意批次含空结果则整体返回 `False` 而非占位列表。

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

## 类型安全工具 (safe_getattr)

`satrap.core.type` 提供一组 `safe_getattr` 系列函数, 用于替代裸 `getattr(obj, "attr", None)`。裸 `getattr` 返回 `Any`, pyright 无法检查返回值; `safe_getattr` 通过类型重载让返回值携带类型信息, 适合在处理外部 API 响应, 动态模块, 多态对象时使用:

```python
from satrap.core.type import (
    safe_getattr,           # 通用, 返回 Any | None
    safe_getattr_str,       # 保证返回 str (默认 "")
    safe_getattr_int,       # 保证返回 int (默认 0)
    safe_getattr_float,     # 保证返回 float
    safe_getattr_bool,      # 保证返回 bool
    safe_getattr_list,      # 保证返回 list
    safe_getattr_dict,      # 保证返回 dict
    safe_getattr_callable,  # 仅当属性可调用才返回, 否则 None
)

text = safe_getattr_str(message, "text")            # 替代 getattr(message, "text", "")
handler = safe_getattr_callable(mod, "build")       # 替代 getattr + callable 检查
if handler is not None:
    handler()
```

这些函数仅提供类型标注与默认值归一化, 不改变 `getattr` 的运行时语义。插件 / 适配器作者处理不确定结构时推荐使用, 以获得 pyright 静态检查。
