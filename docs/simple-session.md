# edictum: SimpleSession 简易 Agent 框架

`satrap/edictum` 是一个相对独立的简易 Agent 系统, 提供 `SimpleSession` (同步) 与 `AsyncSimpleSession` (异步): 基于 Session 的高可扩展单 workflow Agent 框架。

## 设计要点

- **单 workflow / 单主模型**: 一个会话只有一个主模型, 内部直接使用 `full_agent` (React 范式: 用户消息 -> 模型调用 -> 工具循环 -> 最终回复)
- **六类能力管理**: 命令 / 工具 / MCP / skill / 处理器 / 插件, 均提供注入、删除、启用/停用、查看接口
- **处理器直注流程 (非 hook)**: `SessionHandler` 函数直接嵌入调用流程, 4 个处理点:
  - `before_user_send`: 用户消息处理前, 返回 `str` 改写输入, 返回 `None` 透传 (可拦截/改写)
  - `after_user_send`: 用户消息处理后 (通知, 收到改写后的文本)
  - `before_model_reply`: 模型调用前 (通知)
  - `after_model_reply`: 模型回复后 (通知, 收到最终回复文本)
  - 多处理器按 `priority` 升序执行, 支持启停与运行时调整优先级
- **目录插件 (Plugin)**: 工具 + skill + MCP + 处理脚本的组合包 (meta.yaml + tools.py/skills.py/mcp.py/handlers.py), 双层启停 (插件级 × 能力级)
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

## 处理器示例 (优先级 + 改写)

```python
from satrap import SessionHandler

def 敏感词过滤(text: str) -> str | None:
    return text.replace("脏话", "**")

def 记录日志(text: str) -> None:
    print(f"[user] {text}")

session.add_handler(SessionHandler(
    name="filter", priority=10, before_user_send=敏感词过滤,
))
session.add_handler(SessionHandler(
    name="logger", priority=100, after_user_send=记录日志,
))

session.set_handler_priority("logger", 5)   # 调整优先级, 越小越先执行
session.disable_handler("filter")            # 停用处理器
session.remove_handler("logger")             # 删除处理器
```

## 目录插件 (Plugin)

插件是**可分发的能力组合包**: 一个目录即一个插件, 由 `meta.yaml` 声明身份, 可同时携带工具 / 技能 / MCP 接入 / 处理脚本。安装后其能力全部注册进会话, 并支持**双层启停** (插件级开关 × 能力独立状态, 能力生效 = 两者皆开)。

### 目录结构

```text
插件名/
├── meta.yaml     # name(必填) / version / author / repo / description
├── tools.py      # 可选: Tool 子类 (同步版) / AsyncTool 子类 (异步版)
├── skills.py     # 可选: 导出 skills: list[Skill]; 或 skills/ 子目录 (skill.md)
├── mcp.py        # 可选: 导出 clients: dict[str, MCPClient] 或 build_clients() (仅异步版)
├── handlers.py   # 可选: 导出 handlers: list[SessionHandler]; 或 4 个约定函数
└── ...           # 插件私有模块
```

### 安装 / 启停 / 卸载

```python
from satrap import SimpleSession

plugin = session.install_plugin("./plugins/my-plugin")   # 返回 Plugin 实例
plugin.name / plugin.version / plugin.author / plugin.repo   # meta.yaml 元信息

session.disable_plugin("my-plugin")    # 聚合停用: 压制名下全部能力
session.enable_plugin("my-plugin")     # 聚合启用: 按独立状态恢复
session.uninstall_plugin("my-plugin")  # 卸载: 全量回收, 不留孤儿
session.list_plugins()
```

异步版 `install_plugin` / `uninstall_plugin` / `enable_plugin` / `disable_plugin` 均为 async (`await session.install_plugin(path)`), 并支持 `mcp.py` 自动接入。同步版安装含 `mcp.py` 的插件会跳过该文件并警告。

### 双层启停与独立接口

插件内每项能力可**独立启停**, 聚合恢复不会误开独立停用的能力。推荐通过插件实例接口操作:

```python
plugin.disable_tool("calc")      # 独立停用插件内工具
plugin.enable_tool("calc")       # 独立启用
plugin.disable_skill("demo")     # 同步版返回 bool; 异步版需 await
plugin.disable_mcp("fs")         # 独立停用 MCP 连接的全部工具
plugin.disable_handler("log")    # 独立停用处理器
plugin.list_capabilities()        # 展示插件内每项能力的实效状态
```

> 注意: 插件内能力的独立启停建议走插件实例接口, 会话全局接口 (`session.disable_tool`) 不维护插件状态; 插件停用期间对名下能力的操作以恢复时的独立状态为准。

### 安全提示

安装插件 = 执行其代码 (tools.py / skills.py / mcp.py / handlers.py 均会被 import)。meta.yaml 的 `author` / `repo` 仅用于溯源, 不提供安全保证, 请仅安装可信来源的插件。

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
| 处理器回调 | 仅同步函数 | 同步 + 异步函数 |
| MCP 注入 | 不支持 (工具为 AsyncTool, 插件 mcp.py 跳过) | 支持 (`add_mcp` / `remove_mcp` / 插件 mcp.py 为 async) |
| skill 接口 | 同步 (`add_skill` 等) | async (`await add_skill` / `enable_skill` / `disable_skill` / `remove_skill`) |
| 插件接口 | 同步 (`install_plugin` 等) | async (`await install_plugin` / `uninstall_plugin` / `enable_plugin` / `disable_plugin`) |
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
