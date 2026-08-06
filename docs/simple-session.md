# edictum: SimpleSession 简易 Agent 框架

`satrap/edictum` 是一个相对独立的简易 Agent 系统, 提供 `SimpleSession` (同步) 与 `AsyncSimpleSession` (异步): 基于 Session 的高可扩展单 workflow Agent 框架。

## 设计要点

- **单 workflow / 单主模型**: 一个会话只有一个主模型, 内部直接使用 `full_agent` (React 范式: 用户消息 -> 模型调用 -> 工具循环 -> 最终回复)
- **五类能力管理**: 命令 / 工具 / MCP / skill / 插件, 均提供注入、删除、启用/停用、查看接口
- **插件直注流程 (非 hook)**: 插件函数直接嵌入调用流程, 4 个处理点:
  - `before_user_send`: 用户消息处理前, 返回 `str` 改写输入, 返回 `None` 透传 (可拦截/改写)
  - `after_user_send`: 用户消息处理后 (通知, 收到改写后的文本)
  - `before_model_reply`: 模型调用前 (通知)
  - `after_model_reply`: 模型回复后 (通知, 收到最终回复文本)
  - 多插件按 `priority` 升序执行, 支持启停与运行时调整优先级
- **checkpoint 全套**: 继承 Session 聚合检查点 (会话上下文 + 工作流上下文)
- **多模态 + 流式**: `run(user_input, img_urls=[...])` 多模态透传; `set_stream_mode(True)` 切换流式输出

## 快速上手

```python
from satrap import SimpleSession, LLM

llm = LLM(...)  # 或从 ModelConfigManager 获取

session = SimpleSession(
    "conv-1", llm,
    system_prompt="你是助手",
    enable_checkpoint=True,
)

result = session("你好")
print(result)
```

## 插件示例 (优先级 + 改写)

```python
from satrap import SessionPlugin

def 敏感词过滤(text: str) -> str | None:
    return text.replace("脏话", "**")

def 记录日志(text: str) -> None:
    print(f"[user] {text}")

session.add_plugin(SessionPlugin(
    name="filter", priority=10, before_user_send=敏感词过滤,
))
session.add_plugin(SessionPlugin(
    name="logger", priority=100, after_user_send=记录日志,
))

session.set_plugin_priority("logger", 5)   # 调整优先级, 越小越先执行
session.disable_plugin("filter")           # 停用插件
session.remove_plugin("logger")            # 删除插件
```

## 工具 / 命令 / skill

```python
# 工具: 任意时刻注入, 立即生效
session.add_tool(my_tool)
session.remove_tool("my_tool")
session.enable_tool("my_tool") / session.disable_tool("my_tool")
session.list_tools()

# 命令
session.add_command("ping", lambda args: f"pong:{args}", intro="测试")
session.remove_command("ping")
session.enable_command("ping") / session.disable_command("ping")
session.list_commands()

# skill
session.add_skill("coding-agent")
session.remove_skill("coding-agent")
session.disable_skill("coding-agent") / session.enable_skill("coding-agent")
session.list_skills()
```

## MCP 接入 (异步版)

```python
from satrap import AsyncSimpleSession, MCPClient

session = AsyncSimpleSession("conv-1", async_llm)

# stdio 或 streamable HTTP 的 MCP Server
mcp = MCPClient(url="https://example.com/mcp")
await session.add_mcp("fs", mcp)          # 工具自动注册进主工作流

await session.run("列出文件")
await session.remove_mcp("fs")            # 注销工具并断开连接
session.list_mcp()
```

## 模型与流式

```python
session.set_llm(new_llm)                   # 替换主模型
session.set_model_parameters(temperature=0.5)  # 调整调用参数
session.set_stream_mode(True)              # 流式 / 非流式切换
```

## 同步 / 异步边界

| 能力 | SimpleSession (同步) | AsyncSimpleSession (异步) |
|---|---|---|
| 插件回调 | 仅同步函数 | 同步 + 异步函数 |
| MCP 注入 | 不支持 (工具为 AsyncTool) | 支持 (`add_mcp` / `remove_mcp` 为 async) |
| skill 接口 | 同步 (`add_skill` 等) | async (`await add_skill` / `enable_skill` / `disable_skill` / `remove_skill`) |
| 调用方式 | `session("你好")` | `await session("你好")` |
| 初始化 | 构造即就绪 | `await session.initialize()` 或直接 run (自动初始化) |

## checkpoint

```python
session.create_checkpoint(name="存档")
session.list_checkpoints()
session.rollback(checkpoint_id)
session.fork(branch_name="新线")
session.list_branches()
session.list_mutations()
```

未启用检查点 (`enable_checkpoint=False`) 时调用上述接口会抛 `ValueError`。
