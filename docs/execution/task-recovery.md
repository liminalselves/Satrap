# 任务执行记录与恢复

内置 Agent 循环位于 `satrap/core/framework/Base/execution/`, 归属于工作流实现. 普通与可恢复执行, 同步与异步, 流式与非流式均共享同一状态转换, 分别调用原生模型和工具接口. 不增加执行过程中的插件 Hook, 不改变现有工具参数定义. 引擎的分层结构与指令驱动模型见 [执行引擎架构](engine.md)

## 启用与调用

Web Chat 默认启用. Python 会话默认不记录执行步骤, 可以显式启用:

```python
from satrap import AsyncSimpleSession

session = AsyncSimpleSession("demo", llm, db_path="platform.db", recoverable=True)
answer = await session.run("整理资料")
runs = await session.list_runs()
answer = await session.resume_run(runs[0]["id"])
```

同步 `SimpleSession` 提供相同方法, 不需要 `await`. `ModelWorkflowFramework` 和 `AsyncModelWorkflowFramework` 也接受 `recoverable=True`, 覆盖 `full_agent` 与 `stream_full_agent`. 自定义 `forward` 中的任意程序逻辑及工具内部子任务不自动获得步骤级恢复

- `list_runs()`: 返回任务状态和步骤概要, 不暴露模型请求与工具参数正文
- `resume_run(run_id)`: 从持久化步骤继续, 已完成步骤复用结果
- `retry_uncertain_step(run_id, step_id)`: 明确授权未知结果工具步骤再次执行, 授权后另行调用 `resume_run`
- `abort_run(run_id)`: 终止未完成任务; 不撤销已经发生的外部效果

恢复入口不重新执行 SessionHandler 的输入或输出处理器, 返回模型原始最终结果. 原调用的边界处理器仍按原流程执行. 恢复时的内容回调用于展示新产生的输出, 不重放已完成请求的 token

## 统一执行契约

- `max_iterations` 表示工具调用轮数, 一次模型响应中的多个工具属于同一轮; 非正数按 1 处理. 完成 N 轮工具后直接追加统一结束提示, 用 `tools=[]` 请求最终回答, 最多调用模型 N+1 次. 若最终响应仍包含工具调用, 抛出 `ModelProtocolError`
- `full_agent` 和 `stream_full_agent` 成功时返回最终回答, 失败时抛出异常. 无有效模型响应时抛出 `ModelCallError`, 不再将错误字符串作为成功回答. 这两个异常可从 `satrap` 导入; 其他执行异常和取消信号继续向外传播
- 两种模式都暂存本轮消息, 成功后一次提交到模型上下文. 普通模式失败不提交半轮历史; 可恢复模式另外保留输入与步骤结果, 成功时在同一事务提交消息与任务完成状态. 普通内存模式不强制写库
- 消息事务不撤销工具已经发生的外部副作用. Chat 的用户消息与流式展示由独立轮次记录保留, 不依赖半轮模型上下文
- `full_agent` 新增仅限关键字的 `thinking="off"`, 保留第四个位置参数 `img_urls`. 非流式 `SimpleSession.run` 和 `AsyncSimpleSession.run` 同样支持 `thinking`. `return_thinking` 只控制思考内容回传, 不控制请求强度. 恢复使用原任务保存的参数

```python
answer = workflow.full_agent("分析问题", thinking="high")
answer = workflow.stream_full_agent("分析问题", thinking="high")
```

`agent_executor` 保留 `(context, success)` 兼容接口, 内部复用同一循环, 失败返回 `False`. `full_agent` 不再通过这个可覆写方法执行; 依赖覆写 `agent_executor` 自定义流程的代码应改为在 `forward` 中显式调用它, 或自行提供新的公开入口. 自定义调用预先写入的用户消息由调用方管理, 不纳入新任务的提交边界

## 状态与副作用

任务记录保存在所属工作流数据库的 `agent_runs` / `agent_steps`, 与会话历史检查点分工:

| 能力 | 含义 |
|---|---|
| 会话检查点 | 消息状态的回滚, 重试和分支 |
| 任务恢复 | 复用模型及工具的完成结果, 继续未完成步骤 |

状态包括 `running`, `completed`, `failed`, `cancelled`, `interrupted`, `needs_attention`. 进程直接退出时数据库可能保留 `running`, 此时应通过恢复接口尝试接管; 操作系统锁用于判断执行者是否仍持有运行权, 不根据时间超时自动接管

工具开始执行前保存意图, 完成后保存完整结果. 两次保存之间进程退出时, 工具的外部效果可能已经发生. 旧工具默认按 `manual` 处理, 不自动重新执行. 确认可安全重复的工具可声明:

```python
class SearchTool(Tool):
    recovery_policy = "retry"
```

这只是重复执行策略, 不保证幂等或外部效果恰好发生一次. 不支持通过标签自动提供幂等键

内置工具已按实际行为划分恢复策略, 同步与异步版本保持一致:

| 策略 | 工具 |
|---|---|
| `retry` | `search`, `read_file`, `list_dir`, `glob_files`, `grep_files`, `read_document`, `list_memories`, `load_skill`, `rag_search`, `rag_list` |
| `manual` | 文件写入与修改, 记忆增删改, `todo_write`, `rag_ingest`, Shell, 沙箱, 子代理, 用户交互, MCP 工具, 未声明策略的自定义工具 |
| `manual` | `fetch_page`: 目标是任意 URL, 无法仅根据 GET 方法确认接口没有副作用 |

`load_skill` 只返回技能文本, 不执行技能指令. RAG 查询重试可能再次调用 Embedding / Rerank 服务并产生费用. 可重试读取返回的是重试时的数据, 不承诺与中断前内容相同. `todo_write` 同时提供查询与修改操作, 整个工具保留 `manual`

模型请求会保存实际准备后的消息和参数. 请求未完成时可再次请求, 可能产生额外费用及不同回答. 完整流式结果一次落盘, 不逐 token 写入执行记录

新记录采用 v2 请求编码: `agent_steps.input` 仅保存版本标记与统计元数据, 大正文存入同库 `agent_step_inputs`. 同一任务内的后续请求可引用前一请求的共同消息前缀, 引用链最多 7 层, 超限或共享不足时保存完整快照. 引用和完整性验证失败时停止恢复. 旧内联 JSON 请求仍可读取, 不会批量改写已有任务

## 一致性

- 同一数据库与工作流作用域使用非重入进程内锁和操作系统文件锁, 进程退出自动释放
- 会话历史行及 ID 的指纹用于识别追加, 编辑, 回滚等变更, 恢复前及每个后续步骤执行前检查
- 模型参数, 工具定义与实现, 已加载插件代码/配置摘要及能力状态变化会阻止旧任务继续
- 最终消息与任务完成标记在同一 SQLite 事务提交, 重复读取完成任务不会重复追加历史
- 恢复后重建 usage 统计, 任务完成后接入现有自动检查点
- 用户主动取消的任务不会自动恢复
- 归档/恢复包含执行记录; 删除会话同时删除其执行步骤, 并验证归档步骤不能引用其他会话的任务

输入和中间工具消息在执行完成时统一追加到会话历史, 未完成内容保留在任务记录中. 因此失败任务不需要先删除一条孤立的用户消息才能继续

## Chat 的 retry / fork

Chat 的“执行记录与恢复”提供继续执行, 终止和未知工具步骤重试确认. 对应 API:

- `GET /api/chat/runs?conversation=...`
- `POST /api/chat/runs/action`, JSON 字段为 `conversation`, `run_id`, `action`, 可选 `step_id`
- `action`: `resume`, `abort`, `authorize_retry`

执行列表默认每页 20 条, 支持 `limit` (1 到 100), `cursor` 和 `unfinished=true`; 响应通过 `next_cursor` 指示后续页面. Chat 面板提供加载更多和未完成任务筛选. Python 会话的无参 `list_runs()` 仍返回全部摘要, 内部改为批量查询

续跑沿用原轮次和回复版本, 不创建新的回复版本. 原轮次已被删除或切换版本时拒绝回填. 若任务已完成但展示尚未回填, 可选择“回填已完成结果”, 不再次调用模型或工具; 完成之后历史已改变时拒绝回填旧轮次

原有 retry 保留“重新生成”语义: 回退相应上下文, 创建新的执行记录和回复版本, 可能再次执行工具. fork 复制选中的历史和会话设置, 新会话不继承旧任务执行权. 两者都不被偷偷转换为续跑

## 验证

相关测试位于 `test_run_store.py`, `test_recoverable_flow.py`, `test_recoverable_engine.py`, `test_display_service.py`. 覆盖工具未知副作用确认, 保存请求重用, 原子消息提交, 进程退出锁释放, 服务重启后续跑, retry 版本和 fork 作用域隔离

真实后端进程终止验收与离线性能基准见 [恢复机制验收记录](../archive/recovery-validation.md)
