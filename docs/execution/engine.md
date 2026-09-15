# 执行引擎架构

执行引擎位于 `satrap/core/framework/Base/execution/`, 是所有内置 Agent 循环 (核心框架 workflow, edictum 会话, Chat 展示层) 共用的驱动层。设计上把"循环逻辑"与"I/O 和持久化"分开: 循环由一个生成器以固定指令描述, 同步和异步驱动器只是用各自的 I/O 原语执行同一组指令。

## 模块分层

| 模块 | 职责 |
| --- | --- |
| `flow.py` | 固定 Agent 状态转换: `agent_flow(user_input, max_iterations)` 生成器按序产出 `ModelStep` / `ToolStep`, 维护消息列表, 到达最大轮数时禁用工具并要求最终回答 |
| `engine.py` | 统一执行协议: `execute()` 生成器把 `agent_flow` 的步骤展开成执行指令, 并按 `recoverable` 决定持久化策略; `run_sync` / `run_async` 是执行指令的原生 I/O 驱动器 |
| `store.py` | `RunStore`: 任务 (runs) 与步骤 (agent_steps / agent_step_inputs) 的 SQLite 持久化, 与会话共用数据库; 负责执行互斥, 步骤恢复和整轮消息的原子提交 |
| `codec.py` | 任务内模型请求的增量编码: `pack` / `unpack` 压缩同一任务内相邻请求的公共消息前缀, 恢复时使用原始请求记录, 不从当前会话历史重建 |
| `errors.py` | 执行错误类型: `AgentExecutionError` / `ModelCallError` / `ModelProtocolError` |

## 指令驱动模型

`execute()` 不直接做 I/O, 它 yield 出下列指令对象, 由驱动器执行后把结果 send 回来:

| 指令 | 含义 |
| --- | --- |
| `FreezeInput` | 在执行记录创建前固定用户输入中的图像 / 视频媒体 |
| `Prepare` | 准备本轮模型上下文与工具定义 |
| `InvokeModel` | 调用模型 (流式或整包), 或消费兼容入口提供的首次响应 |
| `InvokeTool` | 用工作流工具管理器执行单个工具调用 |
| `RecordUsage` | 保存上下文准备统计与真实 token 用量 |
| `Reload` | 重新加载已提交历史, 成功后按需生成检查点 |
| `Commit` | 不建执行记录, 直接把完整一轮消息提交进上下文存储 |
| `ReplaceHistory` | 为兼容执行预先保存的用户消息补齐固定媒体 |

`run_sync` 与 `run_async` 逐条解释这些指令: 异步版用 `await` 与 `asyncio.to_thread`, 同步版直接调用, 循环语义完全一致。因此普通执行和可恢复执行, 同步会话和异步会话, 走的就是同一个 Agent 循环。

## recoverable 契约

`execute(..., recoverable=...)` 决定是否创建 `RunStore`:

- `recoverable=True` (默认): 任务创建前 `claim()` 获取执行互斥; 每个模型 / 工具步骤的输入与结果持久化; 成功后 `commit_messages` 把整轮消息和任务完成状态原子提交。
- `recoverable=False`: `store=None`, 同一循环零持久化, 成功时改用 `Commit` 指令提交消息。显式传 `run_id` 或 `initial_response` 与可恢复模式互斥, 组合非法时直接抛错。

workflow 入口 (`full_agent` / `stream_full_agent`) 把自身的 `recoverable` 配置透传给驱动器; Chat 展示层固定使用 `recoverable=True`。

## 恢复校验

恢复一个已有任务 (`run_id` 非空) 前会依次校验:

1. 已完成的任务直接重放: 逐条读取已完成模型步骤重建用量统计, 生成检查点后返回原结果, 不重新执行。
2. 已取消任务抛 `RunConflictError`。
3. 会话历史指纹 (`history_signature`) 不一致 → `RunConflictError` (历史已修改, 请新建任务或分支)。
4. 配置指纹 (`configuration()`: 模型参数 + 各工具源码与定义 + 插件恢复指纹) 不一致 → `RunConflictError`。
5. 执行期间每步前再次比对历史指纹, 中途被修改则停止后续步骤。

## 步骤重放与副作用

- 模型步骤幂等: 已有 `completed` 记录时直接使用保存的结果, 不重复调用模型。
- 工具步骤按声明的 `recovery_policy` 处理: `retry` 自动重放; 其他策略在结果未知时抛 `RunNeedsAttention`, 由调用方确认后通过 `authorize_retry` 授权重试。
- 异常出口按类型落状态: 业务失败 `failed`, 取消 `cancelled`, 进程级中断 `interrupted`, 待确认 `needs_attention`。

使用层的语义 (启用方式, Chat 的 retry / fork, 验收记录) 见 [任务执行记录与恢复](task-recovery.md)。
