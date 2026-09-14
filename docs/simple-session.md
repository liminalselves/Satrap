# edictum: SimpleSession 简易 Agent 框架

`satrap/edictum` 是一个相对独立的简易 Agent 系统, 提供 `SimpleSession` (同步) 与 `AsyncSimpleSession` (异步): 基于 Session 的高可扩展单 workflow Agent 框架。

## 设计要点

- **单 workflow / 单主模型**: 一个会话只有一个主模型, 内部直接使用 `full_agent` (React 范式: 用户消息 -> 模型调用 -> 工具循环 -> 最终回复)
- **六类能力管理**: 命令 / 工具 / MCP / skill / 处理器 / 插件, 均提供注入、删除、启用/停用、查看接口
- **处理器直注流程 (非 hook)**: `SessionHandler` 函数直接嵌入调用流程, 4 个处理点 (**回调全同步协议**, 统一携带 `HandlerContext`; 破坏性升级):
  - `before_user_send(text, ctx)`: 用户消息处理前, 返回 `str` / `HandlerResult.continue_with(text)` 改写输入, 返回 `None` 透传; `respond(text)` / `reject(reason)` 短路跳过模型 (ctx.outcome 置位); `abort(reason)` 抛 `HandlerAbortError`; 返回其他类型记 warning 忽略
  - `after_user_send(text, ctx)`: 用户消息处理后 (通知, 收到改写后的文本与上下文)
  - `before_model_reply(ctx)`: Agent/模型调用前 (通知; 命名沿用历史, 语义为"Agent 调用前")
  - `after_model_reply(result, ctx)`: 模型回复后 (通知, `result` 为最终文本); 返回 `str` 改写最终结果; 异常路径 `result=None` 且 `ctx.error` 有值, 返回值被忽略
  - 多处理器按 `priority` 升序执行 (同值按注册顺序, 稳定契约), 支持启停与运行时调整优先级
  - **命令入口 (`cmd_handler.process_message`) 不经过处理器, 仅覆盖 `run()`**; 处理器异常按 `error_policy` 处理 (默认隔离记日志继续, "abort" 抛 HandlerAbortError); 同一次 `run` 内处理器增删/启停/优先级变更为一致性快照 (下次生效)
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

非流式和流式调用均支持 `session.run("问题", thinking="high")`, `return_thinking` 仅决定是否回传思考内容. 内部 Agent 成功后提交整轮模型上下文, 失败抛出异常, 不再把模型调用失败字符串当作成功回答. 可恢复模式和兼容接口边界见 [任务执行记录与恢复](task-recovery.md)

## 处理器示例 (优先级 + 改写 + 短路)

```python
from satrap import SessionHandler, HandlerContext, HandlerResult

def 敏感词过滤(text: str, ctx: HandlerContext) -> str | None:
    return text.replace("脏话", "**")

def 权限拦截(text: str, ctx: HandlerContext) -> HandlerResult | None:
    if "危险操作" in text:
        return HandlerResult.reject("已拦截: 危险操作需要人工确认")
    return None

def 记录日志(text: str, ctx: HandlerContext) -> None:
    print(f"[user] {text}")

def 输出改写(result: str | None, ctx: HandlerContext) -> str:
    return f"{result} (由 handler 改写)"

session.add_handler(SessionHandler(
    name="filter", priority=10, before_user_send=敏感词过滤,
))
session.add_handler(SessionHandler(
    name="guard", priority=20, before_user_send=权限拦截,
))
session.add_handler(SessionHandler(
    name="logger", priority=100, after_user_send=记录日志,
))
session.add_handler(SessionHandler(
    name="tweak", priority=100, after_model_reply=输出改写,
))

session.set_handler_priority("logger", 5)   # 调整优先级, 越小越先执行
session.disable_handler("filter")            # 停用处理器
session.remove_handler("logger")             # 删除处理器 (触发 close)
```

### HandlerResult 短路指令

`before_user_send` 可返回 `HandlerResult` 实现第三态短路 (文本均为 `str`, 与 `run() -> str` 契约对齐):

| 指令 | 行为 |
|---|---|
| `continue_with(text)` | 等价 `str` 改写, 继续执行后续处理器 |
| `respond(text)` | 跳过模型直接返回 `text` (后续处理器短路; `after_model_reply` 仍执行) |
| `reject(reason)` | 业务拒绝, `reason` 作为 `run` 的返回文本 (正常返回) |
| `abort(reason)` | 故障终止, 抛 `HandlerAbortError(reason)` |

### HandlerContext / HandlerConfig

`HandlerContext` 由**冻结只读的 `HandlerConfig`** 与**运行期可变状态**组成:

- `ctx.config` (冻结, handler 不可改): `original_input` / `img_urls` / `thinking` / `max_iterations` / `call_id` (每轮唯一)
- `ctx.text`: 当前文本 (before_user_send 改写链更新; 可读, handler 改动不影响 run 主流程)
- `ctx.error`: 模型调用异常 (after_model_reply 可见; 非 None 时返回值被忽略)
- `ctx.outcome`: 短路语义 ("respond" / "reject" / "abort" 置位, 正常路径为 None)

`SessionHandler.error_policy`: "continue" (默认) 异常隔离记日志继续 (fail-open; 权限/安全检查 handler 请显式设置 "abort"); "abort" 抛 HandlerAbortError 终止 run。

### 处理器生命周期与同名冲突

- `add_handler` 同名默认抛 `ValueError`; 显式 `replace=True` 覆盖, 覆盖前调用旧对象 `close()`; 旧对象属插件时新对象继承其插件归属 (插件停用仍过滤)
- `remove_handler` / 插件卸载会调用处理器 `close()` (子类可覆盖释放资源; 契约: 同步函数、幂等、容忍超时态); **run 进行中移除的处理器延迟到 run 结束冲刷** (防 run 快照持有已 close 对象); `remove_handler` 同步清理所属插件的 handlers 名册, `list_capabilities` 不残留
- **回调全同步协议**: 所有回调必须为同步函数 (async 回调在 add_handler 与插件安装统一抛 `TypeError`); 异步会话经 `asyncio.to_thread` 执行, 网络 IO 用同步库 (requests/sqlite/文件) 即可, 不阻塞事件循环
- **异步超时 = "放弃等待"而非"终止执行"**: 超时后框架不再等结果, 底层工作线程仍跑完——handler 网络调用自设超时 (如 `requests.post(..., timeout=10)`); `timeout=0` 立即放弃等待, 负数拒绝

### 线程安全与线程池

- 注册表 (`_handlers` / `_plugins`) 由 `threading.RLock` 保护, 支持跨线程增删查
- handler 回调经 `to_thread` 可能运行在工作线程: handler 的共享可变状态需自行保证线程安全
- handler 阻塞会占用事件循环**全局默认 executor** 的 worker, 与项目其它 `to_thread` (checkpoint 持久化 / 工具文件 IO / 沙箱执行) 共享同一线程池——失控的无限阻塞会拖累同池操作, handler 必须避免无限阻塞
- 同一会话不支持并发 `run` (异步版经 `asyncio.Lock` 串行化, 第二个 run 排队; 同步版单线程天然串行)

## 目录插件 (Plugin)

插件是**可分发的能力组合包**: 一个目录即一个插件, 由 `meta.yaml` 声明身份, 可同时携带工具 / 技能 / MCP 接入 / 处理脚本 / 命令。安装后其能力全部注册进会话, 并支持**双层启停** (插件级开关 × 能力独立状态, 能力生效 = 两者皆开)。

```python
plugin = session.install_plugin("./plugins/my-plugin")   # 返回 Plugin 实例
session.disable_plugin("my-plugin")    # 聚合停用: 压制名下全部能力
session.enable_plugin("my-plugin")     # 聚合启用: 按独立状态恢复
session.uninstall_plugin("my-plugin")  # 卸载: 全量回收, 不留孤儿
plugin.list_capabilities()              # 每项能力的实效状态 (含 meta.yaml 声明的 description)
```

> 插件系统的完整说明 (目录结构 / meta.yaml 能力声明 / 能力收集约定 / 错误处理 / 同步异步差异) 见 [插件系统](plugin-system.md); 官方示例见 [satrap_coding 插件](satrap-coding-plugin.md)。

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
| 处理器回调 | 仅同步函数 (async 回调抛 `TypeError`) | 仅同步函数 (经 `to_thread` 执行, 不阻塞事件循环) |
| 处理器超时 | 无超时保护 (由插件作者负责) | 默认 30s, `SessionHandler.timeout` 可覆盖; 超时 = 放弃等待 (线程仍跑完) |
| 并发 run | 单线程天然串行 | 不支持并发 run (`asyncio.Lock` 串行化, 第二个 run 排队) |
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
