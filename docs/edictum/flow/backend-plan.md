# 流程会话 (issue #4) 后端改动计划

交给实现 agent 的任务清单。按阶段排列, 后一阶段依赖前一阶段。每项写明改哪里, 做什么, 怎样算完成。

## 0. 先读这些

| 文档 | 内容 |
| --- | --- |
| [node-contract.md](node-contract.md) | 插件节点 `meta.yaml` + `flow_nodes.py` 契约, 存档格式, 迁移, 安装时拒绝的写法 |
| [runtime-contract.md](runtime-contract.md) | 执行语义: 条件三值, 共享求值, 阈值位置, 预算, 状态槽 CAS, 失败处理 |
| `satrap-ui-preview/src/flow/` | 已确认的前端 demo: `registry.ts` (内置节点和端口), `validate.ts` (静态检查规则), `Simulator.tsx` (运行语义的参考实现), `model.ts` (存档格式, 模板) |
| [README.md](README.md) | 验收用例 (目标会话图), 已确认的语义, 和现有机制的对应 |

## 0.1 通用约束

- 不改变 `simple` / `async_simple` 会话的行为; 现有测试必须全部通过
- 流程会话只做 async 版本
- 测试只跑受影响的文件, 串行或 `-n 4` 以内, 不要 `-n auto`
- 新代码放在 `satrap/edictum/flow/` 包下, 公开给插件的名字从 `satrap.edictum.flow` 导出
- 注释, docstring, 日志, 错误信息用中文, 风格和周边代码一致 (现有 docstring 用"参数: / 返回:"格式)
- 不提交 `satrap-ui-preview/`

## 0.2 现有代码要点

| 事实 | 位置 |
| --- | --- |
| 类型注册, 只有 `simple` / `async_simple` | `satrap/edictum/registry.py:169-239` |
| 按类型名写死的参数规范化和默认插件 | `satrap/edictum/config.py:138-146`, `satrap/core/framework/providers/edictum.py:254-255` |
| 单 workflow 假设 `session._wf` | 21 个文件, 用 `grep -rln "\._wf\b\|\"_wf\"" --include=*.py satrap` 列出; 主要在 `edictum/simple_session/*`, `edictum/plugin/base.py:820,842,948`, `core/group_chat/reply.py:217`, `core/group_chat/reminder_service.py:60`, `expend/plugins/group_chat/tools.py:273,346`, `core/plugin_interop/*`, `core/plugin_authorization.py`, `core/approval/coordinator.py`, `core/memory/service.py`, `core/framework/SessionManager.py`, `core/backend/BackendManager.py`, `display/service.py` |
| 多 workflow 上下文已支持 | `base.py:113-133` (`workflow_id_assign`), `async_session.py:219-247` (`_track_workflow_context`) |
| 调度器只在唤醒后调用会话 | `satrap/core/pipeline/scheduler.py:560-610`, 兜底发送 `:832-833`, 出错反馈 `:1148` |
| 消息档案 | `satrap/core/config/platform_messages.py:124`; 表结构 `satrap/core/config/platform_schema.py:150-164`, 当前 `user_version = 12` |
| 插件元数据解析 (不认识的键静默忽略) | `satrap/edictum/plugin/base.py:178-195`, `plugin/catalog.py:207-257` |
| 配置字段 | `satrap/edictum/plugin/config.py:38` (类型), `:66-85` (`ConfigField`), `:169-223` (解析), `:547-574` (payload), `:577-591` (校验) |
| 插件全局配置读写 | `plugin/config.py:418-517`, 服务 `satrap/core/config/plugin_service.py:87-144` |
| 有界线程池 | `satrap/core/utils/async_worker.py:18`, 实例 `:78-82` |
| 诊断接口 | `satrap/core/log/debug/hooks.py` (`record_debug`) |
| 后台发送合约 | `satrap/core/platform/scheduled.py:15-120` |
| 路由 | `satrap/core/backend/control_server.py:3518-3560` (插件配置), `:3760-3789` (edictum 类型/配置), `satrap/core/backend/http_api.py:1361-1479` |

---

## P0 基础: 配置字段, 节点契约, 流程定义, 静态检查

不涉及运行时, 可以先独立完成并测试。

### P0-1 `ConfigField` 新增 `time`, `group`, `visible_when`

- 改 `satrap/edictum/plugin/config.py`:
  - `_FIELD_TYPES` 加 `time`; `validate` / `validate_strict` 接受 `HH:MM` (`00:00`–`24:00`)
  - `ConfigField` 加 `group: str = ""` 和 `visible_when: dict[str, Any | list[Any]] | None`
  - `parse_config_schema` 解析并校验: `visible_when` 引用的字段必须存在 (在整个 schema 解析完后检查)
  - `schema_to_payload` 输出这两个字段 (空时省略)
- `visible_when` 只影响前端显示, 后端校验不看它 (运行时契约, 示例文档第 1.4 节)
- 前端插件配置表单 (`satrap-ui`) 支持这三项: 不在本计划内, 单独开前端任务
- 完成标准: 现有插件 `meta.yaml` 全部照常解析; 新增单测覆盖三项的解析, 校验, payload

### P0-2 端口类型和内置节点注册表

- 新建 `satrap/edictum/flow/types.py`: 7 种端口类型及其空值, 插件可用范围 (示例文档第 1.3 节)
- 新建 `satrap/edictum/flow/registry.py`: `FlowNodeType` 数据类 (完整类型名, 版本, label, description, 角色, 输入/输出端口, `passthrough`, `needs`, `config_schema`, `renamed`, `reading`, 来源插件), 注册表按完整类型名查找
- 内置节点照搬 demo `registry.ts` 的 `BUILTIN_TYPES`: `source`, `bucket`, `state`, `assemble`, `react_workflow`, `output`, `wake`, `threshold`, `tool`, `cond_message`, `cond_time`, `cond_state`, `and`, `or`, `not`, `note`; 端口名, 类型, 配置字段, 默认值都以 demo 为准
- 内置节点的 `react_workflow` 配置额外加 `max_calls_per_run` (运行时契约 5.3)
- 和 demo 不同的地方 (以本计划为准):
  - `output` 节点用 `send_mode` (`flow_reply` / `group_chat_reply` / `final_text`, 默认 `flow_reply`) 代替 demo 的 `fallback_send` (见 P1-8)
  - `state` 节点加 `on_new` (`keep` / `clear`), `threshold` 磁贴加 `on_new` (`drop` / `drain`) (见 P2-3b)
  - demo 模板里 RP 装 `group_chat` 并在提示词里要求调用 `group_chat_reply`; 后端模板改为不装 group_chat, 提示词改为 `flow_reply`
- 完成标准: 注册表能列出全部内置节点, 序列化成给前端的 payload

### P0-3 插件 `flow_nodes` 解析与安装校验

- 在 `plugin/catalog.py` 解析 `meta.yaml` 的 `flow_nodes` (列表, 每项 `name`) 和 `flow.workflow`; 结果挂到 `PluginCatalogEntry`, `to_payload` 输出
- 实现示例文档第 2 节的全部推断规则: 角色, 端口显示名和位置, 自动 `when` 口, `passthrough` 只按必填输入推断, `calls_model` 由模型类字段推断, 卡片字段
- 加载 `flow_nodes.py` (复用 `plugin/base.py:225-294` 的 `_load_module`): 同名函数, `@node("x")`, `FlowNode` 子类三种写法
- 安装校验: 示例文档第 18 节列出的全部 `flow_contract_*` 错误; 不通过时安装失败并回滚 (和 services 的严格规则一致, 不是 tools 的"只警告")
- `flow_nodes` **不加入** `CAPABILITY_KINDS`
- 完成标准: 示例文档里每个合法示例都能解析, 每个非法示例报对应错误码; 用 `tests/fixtures` 下的临时插件目录测试

### P0-4 流程定义 schema 与迁移

- 新建 `satrap/edictum/flow/definition.py`: 解析 `params.flow` (`version: 3`, `defaults`, `nodes`, `edges` 含 `label`, `layout`, `budget`); 存档格式见示例文档示例 L
- 配置合并: 类型默认 < 插件全局默认 < 流程默认 < 本节点 (运行时契约 7.1), 返回生效值和每个字段的来源层
- 迁移: 四处 (节点实例, 流程默认, 插件全局默认, 连线端口) 各按自己的版本逐版本执行 `renamed` 和 `migrate()`, 全部完成后校验一次 (运行时契约 7.3)
- 完成标准: 迁移单测覆盖示例 N 的场景, 包括"v2 改名 a→b, v3 新增 a"和插件降级

### P0-5 静态检查

- 新建 `satrap/edictum/flow/validate.py`: 移植 demo `validate.ts` 的 `analyze` + `validate` 全部规则, 问题格式 `{code, level, category, node?, edge?, field?, message, hint?}` 与 demo 的 `Issue` 一致
- 额外的后端规则 (demo 里没有): `flow_node_plugin_disabled`, `flow_node_config_stale`, `flow_node_version_ahead`, `flow_plugin_*` 用真实插件目录和 `flow.workflow` 声明判断 (不写死插件名)
- 完成标准: demo 三个模板 (`model.ts` 的 `TEMPLATES`) 转成 JSON 后校验结果与 demo 一致 (都是 0 错误; 提示数一致); 每条规则至少一个触发用例

### P0-6 注册 `flow` 类型和接口

- `registry.py` 注册 `flow` (`is_async=True`, 工厂指向 P1 的 `AsyncFlowSession`, P1 完成前可以先注册一个只做校验的占位)
- `config.py:138-146` 和 `providers/edictum.py:254-255` 的类型分支: `flow` 不调用 `normalize_session_settings`, 不注入默认插件; 保存时调用 P0-5 校验, 有错误则拒绝
- `GET /config/edictum/types` (两处路由) 的 `flow` 类型返回: 全部节点类型 (内置 + 插件), 端口类型表, 各层默认值, 模型能力 (上下文长度, 是否支持工具)
- 新增 `POST /config/edictum/flow/validate`: 只校验不保存, 返回问题列表; 两处路由都要加
- 完成标准: 能通过接口保存 demo 的 `agent_rp` 模板; 故意改坏后保存被拒绝并返回问题列表

### P0-7 插件节点全局默认

- 文件 `.satrap/config/plugins/<插件名>.flow_nodes.json`, 格式 `{format: 1, nodes: {节点名: {version, config}}}` (示例文档示例 K)
- 在 `PluginConfigManager` 加读写方法, 复用 `global_path` 的命名校验, `FileLock`, `atomic_write_json`; 不改动 `<插件名>.json` 的读写
- 新增 `GET/PUT /config/plugins/<插件名>/flow-nodes`, PUT 体 `{config, expected_revision}`, revision 用文件 SHA256 (和 `plugin_service.py:97-98` 一致)
- 插件升级后首次加载时迁移并写回, 写前备份
- 完成标准: 修改节点默认不改变插件配置的 revision, 不触发插件重装

---

## P1 运行时: 单群跑通验收用例

目标: 用 demo 的 `agent_rp` 模板, 在离线测试里跑通"阈值驱动后台 Agent 写快照, 唤醒驱动 RP 读快照回复"。

### P1-1 多 workflow 会话

- 新建 `satrap/edictum/flow/session.py`: `AsyncFlowSession`, 每个 WF 节点一个 `AsyncModelWorkflowFramework` (各自 `tools_manager`, `ctx`), 用 `workflow_id_assign(节点 id)` 分配 id, `_track_workflow_context` 登记
- 插件按 WF 节点安装: 每个 WF 只装自己 `plugins` 列表里的插件, 工具互相看不见
- `session._wf` 的写死点改成"当前 WF":
  - 先逐个审查第 0.2 节 grep 出的 21 个文件, 把每处 `_wf` 分成三类写进 PR 说明: (a) 指"正在运行的那个 WF" (例如回复绑定, 工具归属检查); (b) 指"插件要装到的 WF" (安装, 能力启停); (c) 只在 `simple` 会话里出现, 流程会话走不到
  - (a) 类: 引入 contextvar `current_flow_workflow`, 流程会话运行某个 WF 时设置; 这些位置先读 contextvar, 读不到再回退 `session._wf`
  - (b) 类: 改成显式传入目标 WF; 流程会话安装插件时按 WF 节点逐个调用
  - (c) 类: 不改
  - 保证 `simple` / `async_simple` 行为不变
- 完成标准: 两个 WF 各装不同插件, 工具列表互不包含; `simple` 会话全部现有测试通过

### P1-2 消息入口: 会话收到每条消息

- 调度器当前只在唤醒判定通过后调用会话。为 `flow` 类型加一条旁路: 每条入站消息和每条已确认的出站消息都交给流程会话的 `observe(message)`, 不经过唤醒判定, 不占 `_session_turn` 锁
- 流程会话内部决定唤醒谁: 群唤醒磁贴勾选"沿用群策略"时复用 `evaluate_wake` (`satrap/core/pipeline/wake_policy.py:113`)
- 被唤醒的前台 WF 运行时仍要能用现有回复链路 (`bind_reply_turn` / `ReplyTurn`): 唤醒来自哪条消息, 就绑定那条消息的事件
- 完成标准: 测试里连续投递 12 条消息, 流程会话每条都收到; 被 @ 的那条触发 RP 并通过 `group_chat_reply` 发出

### P1-3 桶与消息序号

- 桶建在 `PlatformMessageStore` 上。阈值位置需要单调递增的序号: `platform_messages` 加一列 `seq INTEGER` (每个 `scope_key` 内单调递增, 写入时分配), `platform_schema.py` 升到 `user_version = 13`, 旧数据按 `(message_time, message_id)` 回填
- `PlatformMessageStore` 新增按序号范围读取的方法: `after(scope, seq, limit)`, `latest(scope, n)`
- 不能直接用 SQLite 的 `rowid`: 删除恢复流程会删行再写回 (`platform_messages.py:719-723`), 写回后 `rowid` 变大, 顺序就错了。`seq` 必须是一列, 恢复时保留原值
- 分配方式: `platform_message_chats` 加一列 `next_seq`, 写入消息时在同一事务里取值并加一
- 迟到的消息 (平台补发, 档案回填): 照样分配新的 `seq`, 排在后面。阈值位置按 `seq` 走, 不会漏处理; 但它在 `assemble` 的"最新 n 条"里按 `message_time` 排序, 两者顺序可能不同, 这是预期行为
- 完成标准: 序号在同一群内单调递增, 撤回/删除不改变其他消息的序号

### P1-4 阈值磁贴, 每个 WF 的已提交位置

- 存储: `platform.db` 新表 `flow_threshold_positions(scope_key, flow_name, tile_id, wf_id, committed_seq, updated_at)`, 主键 `(scope_key, flow_name, tile_id, wf_id)`; 和 P1-6 状态槽同库
- 判断与触发按运行时契约 3.6: 每个 WF 分别看自己的位置; 只触发满足的 WF; 批次受 `max_batch` 限制
- 时间阈值 (`idle_seconds`) 需要定时检查: 流程会话持有一个后台任务, 会话释放时取消
- 完成标准: 两个 WF 由同一磁贴驱动, 一个失败一个成功, 下次只重试失败的那个; 成功的位置推进

### P1-5 触发引擎: 事件, 条件三值, 逻辑门

- 新建 `satrap/edictum/flow/engine.py`, 语义以 demo `Simulator.tsx` 和运行时契约第 2 节为准:
  - 事件从磁贴出发, 经 AND/OR 门传递到 WF 的 `trigger` 口; AND 两个事件输入用时间窗
  - 条件三值: 真/假/错误; 门的运算表; 错误不触发并写诊断
  - 插件条件函数走新建的 `FLOW_CONDITION_WORKERS = BoundedAsyncWorker("satrap-flow-cond", workers=4, capacity=32)`, 150 毫秒超时, 线程池满判错误; 内置条件在事件循环里直接求值
  - `on_busy`: queue / merge / drop; drop 阈值批次时不推进位置
  - WF 完成后发 `done` 事件 (沉默时输出为空)
  - 交接工具: 运行时把工具挂到调用方 WF 的 `tools_manager`; 调用时立即返回"已交给 X", 再触发目标 WF
- 完成标准: demo 三个模板的 `Simulator` 脚本 (17 条消息) 在后端离线测试里得到相同的 LLM 调用次数 (5, 6, 6) 和相同的回复决策; 用 mock LLM

### P1-6 状态槽与 `flow_snapshot` 插件

- 存储: `platform.db` 新表 `flow_states(scope_key, flow_name, state_id, version, content, covers_to, committed_at, run_id)`
- 提交按运行时契约第 6 节: CAS (`base_version`), 多槽一起提交, 与阈值位置推进在同一个事务
- `writer_reads_previous` 开关: 写入方 WF 开始时自动拿到上一版
- 新内置插件 `satrap/expend/plugins/flow_snapshot/`: 工具 `snapshot_write` / `snapshot_read`, 只绑定这个 WF 连着的状态槽; `meta.yaml` 写 `flow.workflow.provides: [state_write]`
- 完成标准: 验收用例里 Agent 写 v1, v2, RP 读到最新版本; 并发提交冲突时报 `flow_state_conflict`

### P1-7 transform 求值与共享

- 按运行时契约第 3 节: 拉取时拓扑求值; 同一触发事件内共享结果; 等待者计数, 全部取消才中断; 超时取等待者最长; 输入输出按第 4 节 (None 和空值区分, frozen, 输出校验)
- 内置 transform: `bucket`, `assemble` (快照覆盖后 + 最新 n 条 + 去重, 逻辑见 demo `Simulator.tsx` 的 `pullParts`)
- 插件 transform: 同步函数走 `SESSION_WORKERS` 或新建池, 异步函数直接 await
- 完成标准: 两个 WF 同一事件拉取同一插件节点, 节点函数只被调用一次

### P1-8 发送: 平台回复节点自带 `flow_reply`

流程的发送不依赖 `group_chat` 插件。`group_chat_reply` 只能在有来源回复轮次时使用 (`satrap/core/group_chat/service.py:364-366` 要求 `current_reply_turn()` 且来源一致), 被交接/完成事件触发的 WF 没有来源轮次, 私聊也受 `reply.py:220-223` 限制, 所以流程必须有自己的发送工具。

- 平台回复节点新增配置 `send_mode`: `flow_reply` (默认) / `group_chat_reply` / `final_text`
  - `flow_reply`: 宿主给连到该节点的每个 WF 注入工具 `flow_reply`, 参数 `{text: str, quote_trigger?: bool}`; 不调用即沉默; 工具定义放在 `satrap/edictum/flow/reply_tool.py`, 不放进任何插件
  - `group_chat_reply`: 沿用 group_chat 插件的富消息回复 (@, 图片, 表情), 要求 WF 装了 group_chat; 静态检查: WF 可能被后台触发 (`roots` 含阈值或经交接/完成到达) 时报错 `flow_reply_mode_background`, 在私聊会话中报错 `flow_reply_mode_private`
  - `final_text`: 等价于旧的 `fallback_send=true`, 把 WF 最终文本发出去
- 发送路径由宿主按有无来源消息选择:
  - 有来源消息 (群唤醒触发的前台运行): 走现有 `ReplyTurn` (`satrap/core/group_chat/reply.py`), 群和私聊一样
  - 无来源消息 (阈值, 交接, 完成触发): 走 `ScheduledTarget` + `execute_scheduled_segments` (`satrap/core/platform/scheduled.py:15-120`), 用流程会话的 `MessageScope` 作目标; 需要一个流程专用的 `ScheduledRecorder` 实现 (现有唯一实现在提醒功能里, `satrap/core/group_chat/reminder_host.py:261`)
- 适配器能力: 后台发送目前只有 `supports_scheduled_group_send` (`satrap/core/platform/__init__.py:119`, OneBot 为 True), 没有私聊对应项。需要新增 `supports_scheduled_private_send` (OneBot 实现私聊主动发送); 不支持的适配器上, 后台触发的发送失败并按 P1-9 处理, 静态检查给提示 `flow_background_send_unsupported`
- 调度器兜底发送 (`scheduler.py:832-833`): 流程会话的运行不经过这段 (由 P1-2 的旁路处理), 宿主只按 `send_mode` 发送
- 平台回复的发送条件 `when` 在发送前求值
- 静态检查去掉 `flow_reply_plugin_missing`; `flow.workflow.provides` 不再有 `reply` 取值
- 完成标准: (1) 模板里的 RP 不装 group_chat 也能回复; (2) logic 模板里 Agent 调用 `remind_group` 后, RP 的提醒通过后台发送成功发出并记录发送证据; (3) 私聊会话里, 唤醒触发的 RP 回复成功

### P1-9 失败处理与诊断

- 按运行时契约 10.1: `record_debug` 新类别 `flow_trigger` / `flow_run` / `flow_node` / `flow_state` / `flow_budget`; 每次失败 `logger.error`; 向群发一条 `error_feedback` 告知, 同一 WF 连续失败只告知一次, 跟随调度器 `error_feedback` 开关
- 不做退避, 不暂停磁贴
- 完成标准: mock LLM 连续失败 3 次, 群里只收到 1 条告知, 日志 3 条; 成功一次后再失败, 再收到 1 条

### P1-10 私聊

流程配置可以绑到私聊路由 (`session_bindings` 的 `private`, `satrap/core/config/agent_routing.py:82`)。

- 档案: 私聊已按 `MessageScope(adapter, self_id, "private", 对方 user_id)` 入档 (`satrap/core/platform/__init__.py:325-341`), 桶直接可用; 每个私聊对象一份桶, 一份状态槽, 一组阈值位置
- 唤醒: 现有 `evaluate_wake` 对私聊直接返回唤醒 (`satrap/core/pipeline/wake_policy.py:123`)。群唤醒磁贴在私聊里:
  - `use_platform_policy: true` 时沿用这个行为, 每条消息都唤醒
  - 自定义规则时照常按 @ (私聊里视为总是被 @), 唤醒词, 新消息数, 冷却判断
- 静态检查: 流程绑到私聊路由时, 用会话类型重新检查插件 `applicability` (`group_chat`, `group_admin`, `friend_manager` 等只声明 `platform` 的插件在私聊里能装, 但功能是否可用由插件自己判断); `send_mode: group_chat_reply` 报错
- 静态检查需要知道绑定的会话类型: `POST flow/validate` 增加可选参数 `conversation_kinds: ["group", "private"]`, 保存流程时取它当前被绑定的所有类型
- 完成标准: 同一份流程配置同时绑群和私聊, 两边各自有独立的快照和阈值位置, 私聊里 RP 用 `flow_reply` 回复成功

---

## P2 预算, 生命周期, 运行状态

### P2-1 模型调用计量与预算

- 包装 WF 和 `ctx.resources` 拿到的模型客户端, 每次调用前原子检查并扣减 (运行时契约第 5 节)
- `params.flow.budget` (流程硬限制) 和 WF 的 `max_calls_per_run`
- 共享插件节点的调用只计一次, 记录不带 `run_id`
- 给条件节点 `ctx.calls(kind, scope, window_seconds)` 读计数
- 首版只存内存
- 完成标准: 超限时调用不发出, 抛 `BudgetExceeded`, WF 失败并走 P1-9

### P2-2 节点实例生命周期

- 按运行时契约第 8 节: 类写法 `setup` / `close`; 生效配置变化时重建实例, 进行中的求值用旧实例完成
- 配置变化来源: 节点配置保存, 流程默认保存, P0-7 插件节点默认保存, 插件升级
- 完成标准: 修改节点配置后, 下一次求值用新实例, 旧实例 `close` 被调用

### P2-3 重启恢复

- 按运行时契约第 10 节表格: 阈值位置和状态槽保留; 排队, 合并, AND 窗口, 唤醒计数, 调用计数丢弃
- 完成标准: 测试里重建会话后, 阈值位置和快照版本不变, 未完成的批次重新触发

### P2-3b `/new`

- 每个 WF 的对话历史清空 (和 `simple` 一致)
- 档案起点按现有机制更新 (`SessionManager.py:1665`), 之后桶读不到起点之前的消息
- 状态槽新增配置 `on_new`: `keep` (默认) / `clear`; `clear` 时版本回到 0, 内容为空
- 阈值磁贴新增配置 `on_new`: `drop` (默认) / `drain`
  - `drop`: 每个 WF 的已提交位置直接跳到 `/new` 时刻的最新序号
  - `drain`: `/new` 生效前, 先对每个落后的 WF 跑一次批次 (受 `max_batch` 限制, 积压多时可能跑多批), 全部结束后再切换档案起点; 等待上限 120 秒, 超时的 WF 按 `drop` 处理并写诊断
  - 只有它驱动的 WF 所写状态槽为 `keep` 时 `drain` 才有意义; 否则静态检查提示 `flow_new_drain_cleared`
- 完成标准: 两个开关的四种组合都有测试

### P2-4 运行状态接口

- `GET /config/edictum/flow/runtime?platform_id&session_id`: 每个阈值磁贴每个 WF 的已提交位置和积压数, 每个状态槽的版本和覆盖位置, 正在运行的 WF, 最近 50 条触发事件 (来自诊断记录)
- 完成标准: 前端可以用它做"运行事件"面板 (前端不在本计划内)

---

## P3 示例插件与收尾

- 按示例文档实现 `quiet_hours.in_hours` 和 `llm_budget.within` (放 `satrap/expend/plugins/`), 作为插件节点契约的真实样例
- `group_chat`, `memory`, `image_generation` 的 `meta.yaml` 加 `flow.workflow` 声明 (示例文档示例 J); `image_generation` 当前 `interop.requires: group_chat`, 在流程里它要能通过 `flow_reply` 发图, 需要评估是否放宽这个依赖 (单独任务, 先问用户)
- 文档: 实现完成后, 把 `docs/edictum/flow/` 里与实现不一致的地方改成实际行为

---

## 已确认的决定

| 问题 | 决定 |
| --- | --- |
| `/new` 对快照和阈值 | 做成节点配置: 状态槽 `on_new` 默认 `keep`, 阈值磁贴 `on_new` 默认 `drop` (P2-3b) |
| 私聊 | 支持, 见 P1-10 |
| 条件线程池 | 4 线程, 容量 32 |
| 单个群覆盖流程节点字段 | 首版不支持; 需要不同值时复制流程配置 |
| 失败告知文案 | "<WF 标题> 本次运行失败: <简短原因>" |
| 发送 | 平台回复节点自带 `flow_reply`, 不依赖 group_chat (P1-8) |
