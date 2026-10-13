# 测试模块整合, 2026-10-13

基线: `216272757d5edcdc82e1e43834f7174154ea2088`, 分支 `fix/issue1`

## 范围与组织

生产代码没有修改, 本轮只调整自动测试, 共享夹具, 浏览器运行入口和本文档

后端按配置, 插件应用, 请求审批, 群聊, 提醒, 记忆, 会话, 媒体与存储等业务域组织, 单元测试模块从 232 个整合为 78 个. 保留参数化安全, 权限, 幂等, 并发与故障矩阵; 不把真实外部服务作为默认回归依赖

跨测试文件复用的构造器移到 `tests/support`, 合并后为原测试显式绑定其隔离夹具, 避免扩大原 autouse 作用范围. 跨进程工作器使用共享构造器, 不再导入已删除的小测试模块; 插件参数读取改为调用已注册的 /about 命令, Windows 状态文件读取允许 1 秒内重试原子替换造成的短暂 PermissionError

提醒创建贯通模型工作流, 工具, 策略及真实数据库, 同时验证同步/异步执行, 重复调用只落库一次, 正文规范化与原校验函数不修改输入, 提及顺序及独立副本, 创建时不提前发消息. 非法提及矩阵穿过工具, 管理入口和存储边界, 拒绝时不回源, 不落库

前端将服务契约, 配置契约, 浏览器运行环境, 展示与性能场景分别集中. 三个资源管理流程通过真实 Axios 客户端和 API facade, 只模拟传输适配器, 覆盖记忆提案/删除, 提醒创建/取消/恢复和平台/插件应用. 安全令牌, 配置来源版本, 异步应用和实例覆盖的集中契约保留

浏览器 39 个业务场景整合到 14 个模块. 总入口复用 Chromium 与普通 Vite 服务, 每个场景创建独立页面与请求夹具; 自定义中间件及生产预览使用独立服务. 原 npm 命令仍支持单独执行对应场景

## 夹具修复与覆盖变化

- 补齐原浏览器夹具遗漏的 `/config/versions`, 来源 token 随场景配置变化, 保留原版本失败/重试场景
- 修正全局插件响应字段 `config`, Edictum 定向应用接口及恢复面板 Router/ConfirmProvider 上下文
- 插件继承参数加载完成后才断言值; 外部来源变化后的轮询仍验证草稿保留
- 120 秒应用截止使用受控浏览器时钟, 截止前仍处于等待, 到期保留已保存事实与草稿
- 对外部请求进入加 30 秒测试截止, 失败附页面诊断, 避免测试无限挂起
- 引用保护夹具使用独立临时包名, 避免整合后的测试顺序受 `sessions` 模块缓存污染
- 提醒的 13 个分散用例替换为 11 个跨层非法输入场景, 正向校验并入同步/异步完整工作流, 后端展开数净减少 2
- 前端 9 个薄封装用例替换为 3 个贯通资源管理场景, 展开数净减少 6

## 执行入口

```powershell
python -m pytest -q tests
python -m pytest -q tests/unit/test_reminder_creation_flows.py
python -m pytest -q tests/integration/test_chat_recovery_process.py tests/integration/test_plugin_application_processes.py --run-integration
cd satrap-ui
npm test
npm run build
npm run test:e2e:all
node e2e/runtime-applications.mjs --case scoped-plugin-apply
```

PowerShell 执行前需设置 Console InputEncoding/OutputEncoding 与 OutputEncoding 为 UTF-8, 以及 PYTHONUTF8=1, PYTHONIOENCODING=utf-8

## 后端原模块对应表

| 业务模块 | 合并来源 |
|---|---|
| `test_account_request_flows` | `account_requests`, `friend_self_service`, `friend_manager` |
| `test_administrator_management_flows` | `administrator_settings_api`, `admin_control_and_config` |
| `test_administrator_policy_flows` | `administrator_groups`, `administrator_plugin_integration`, `administrator_read_split` |
| `test_chat_stream_flows` | `display_stream`, `display_recorder` |
| `test_checkpoint_flows` | `session_checkpoint`, `session_manager_checkpoint`, `checkpoint_regressions`, `checkpoint_http_api`, `cmd_checkpoint` |
| `test_cli_configuration_flows` | `cmd_edictum`, `cmd_plugin`, `cmd_session_instance` |
| `test_cli_flows` | `cli_client`, `cli_common`, `cli_control_and_api`, `cli_output` |
| `test_coding_configuration_flows` | `coding_configuration`, `satrap_coding_plugin`, `base_take_plugin` |
| `test_coding_flows` | `satrap_coding_core`, `satrap_coding_tools`, `coding_contracts`, `fetch_parity` |
| `test_coding_security_flows` | `sandbox_security`, `satrap_coding_security`, `proc_env` |
| `test_configuration_agents` | `edictum_config`, `edictum_config_snapshot`, `edictum_settings`, `session_class_config_service` |
| `test_configuration_documents` | `config_document`, `config_paths`, `config_versions` |
| `test_configuration_models` | `model_config_service`, `model_snapshot`, `llm_factory` |
| `test_configuration_references` | `asr_config_references`, `group_references`, `priority_function_fixes` |
| `test_context_policy_flows` | `context_policy`, `context_hysteresis_and_summarize`, `input_budget`, `window_projection` |
| `test_context_storage_flows` | `context_incremental_save`, `context_parity`, `context_usage_snapshot`, `legacy_context_cleanup`, `ephemeral_context` |
| `test_control_api_flows` | `http_api_dispatch`, `server_auth`, `user_http_api` |
| `test_control_ui_flows` | `control_static`, `static_ui`, `main_merge_contracts`, `expend_structure`, `sync_async_packages` |
| `test_conversation_storage_flows` | `conversation_catalog`, `conversation_data`, `conversation_long_term_cleanup`, `user_directory` |
| `test_group_administration_flows` | `group_admin_plugin`, `group_admin_roles`, `group_actions` |
| `test_group_configuration_flows` | `group_store`, `group_directory`, `group_events`, `group_session_overrides`, `group_sync_api` |
| `test_group_media_flows` | `group_chat_assets`, `group_chat_media_service`, `group_chat_media_management`, `group_chat_stickers` |
| `test_group_message_flows` | `group_chat_service`, `group_chat_reply`, `group_chat_plugin`, `group_chat_route_verification` |
| `test_group_summary_flows` | `group_chat_summaries`, `group_chat_summary_service`, `group_chat_summary_management` |
| `test_knowledge_flows` | `rag_service`, `rag_embedding_failures`, `document_upload`, `text_utils` |
| `test_llm_protocol_flows` | `llm_call_parity`, `llm_response_parsing`, `stream_call`, `thinking_request`, `tool_calling`, `safe_parse_arguments` |
| `test_logging_flows` | `logging_policy`, `log_retention`, `log_stream` |
| `test_media_projection_flows` | `media_resolve`, `media_source_security`, `quote_projection`, `attachments`, `audio_convert` |
| `test_memory_flows` | `memory_management`, `memory_platform`, `memory_plugin`, `memory_migration`, `scoped_memories` |
| `test_platform_application_flows` | `platform_plugin_apply_snapshot`, `full_agent_apply_snapshot` |
| `test_platform_forwarding_flows` | `onebot_forward`, `message_forward` |
| `test_platform_ingress_flows` | `onebot_ingress`, `platform_message_ingress`, `onebot_self_identity` |
| `test_platform_lifecycle_flows` | `platform_connection`, `platform_lifecycle`, `backend_startup_control` |
| `test_platform_message_flows` | `platform_messages`, `platform_message_data`, `platform_message_maintenance`, `platform_message_outgoing` |
| `test_platform_misskey_flows` | `misskey_adapter`, `misskey_client`, `misskey_runtime_session` |
| `test_platform_notice_flows` | `platform_notices`, `platform_logging`, `backend_health_and_log_state` |
| `test_platform_reply_flows` | `onebot_outbound`, `outbound_security`, `reply_decoration`, `send_receipt` |
| `test_plugin_application_flows` | `plugin_apply_scope`, `plugin_apply_snapshot`, `scoped_plugin_apply`, `control_plugin_application` |
| `test_plugin_catalog_contracts` | `plugin_catalog_snapshot`, `plugin_spec`, `plugin_compatibility`, `plugin_warning_regression` |
| `test_plugin_configuration_flows` | `plugin_config`, `plugin_management`, `plugin_management_permissions` |
| `test_plugin_installation_flows` | `plugin_archive`, `plugin_module_loading`, `plugin_resources`, `plugin_skill_activation` |
| `test_plugin_migration_flows` | `friend_switch_migration`, `group_tool_migration` |
| `test_plugin_runtime_flows` | `plugin_loop_bridge`, `loop_bridge`, `async_worker` |
| `test_recovery_flows` | `state_store`, `run_store`, `recoverable_engine`, `recoverable_flow`, `recovery_codec`, `builtin_recovery_policy` |
| `test_release_flows` | `release_packaging`, `runtime_paths` |
| `test_reminder_creation_flows` | `reminder_host`, `reminder_plugin`, `reminder_time`, `reminder_validation` |
| `test_reminder_delivery_flows` | `reminder_store`, `reminder_scheduler`, `reminder_management`, `scheduled_sends`, `onebot_reminders` |
| `test_request_approval_flows` | `request_inbox`, `request_diagnostics`, `request_decision_preflight` |
| `test_request_identity_flows` | `friend_request_occurrences`, `request_archive`, `suspicious_friend_requests` |
| `test_resource_protocol_flows` | `asr_call`, `embedding`, `rerank_security`, `mem0` |
| `test_routing_flows` | `agent_routing`, `agent_routing_config`, `multi_adapter_routing` |
| `test_session_command_flows` | `command_entry`, `session_commands`, `tools_enable_disable` |
| `test_session_execution_flows` | `session_manager_concurrency`, `call_origin`, `provider_call_origin`, `event_chain` |
| `test_session_management_flows` | `session_manager_config`, `session_providers`, `session_discovery`, `session_overrides`, `multisession` |
| `test_skills_flows` | `skills`, `skills_dirs`, `mcp` |
| `test_storage_flows` | `database`, `database_recovery`, `durability`, `storage_layout`, `storage_restore` |
| `test_visual_input_flows` | `visual_input`, `vision_context_and_llm`, `pdf_visual_input` |
| `test_wake_pipeline_flows` | `wake_window`, `wake_rejections`, `wake_all` |
| `test_wake_policy_flows` | `wake_overrides`, `wake_dry_run`, `policy_field_contract` |
| `test_wake_recovery_flows` | `manual_wake`, `manual_wake_store`, `platform_store_recovery` |

## 前端合并来源

| 场景模块 | 原模块 |
|---|---|
| `configurationContracts.test.tsx` | `utils/adminMigration.test.ts`, `utils/wakeOverrides.test.ts`, `utils/wakePolicyContract.test.ts`, `utils/edictumParams.test.ts`, `components/common/PluginConfigFields.test.tsx` |
| `serviceContracts.test.ts` | `api/client.test.ts`, `api/control.test.ts`, `api/errorMessages.test.ts`, `api/rag.test.ts`, `utils/errorText.test.ts` |
| `browserRuntimeContracts.test.ts` | `utils/clipboard.test.ts`, `utils/constants.test.ts`, `utils/runtimeConfig.test.ts`, `utils/format.test.ts` |
| `performanceFlows.test.ts` | `utils/logBuffer.test.ts`, `utils/frameLimiter.test.ts`, `utils/visiblePoll.test.ts`, `hooks/glassReflectGeometry.test.ts`, `hooks/useGlassReflect.test.ts` |
| `chatPresentation.test.tsx` | `pages/Chat/AskUserPanel.test.tsx`, `pages/Chat/conversations.test.ts`, `pages/Chat/streaming.test.ts`, `components/diagnostics/RequestDiagnosticsPanel.test.tsx` |
| `resourceManagementFlows.test.ts` | `api/backend.test.ts`, `api/memory.test.ts`, `api/reminders.test.ts` |

## 浏览器合并来源

| 业务模块 | 场景 |
|---|---|
| `access-routing` | `administrators`, `agent-routing`, `backend-platform-status` |
| `model-configuration` | `asr-models`, `model-dimensions` |
| `platform-configuration` | `platform-policy`, `manual-wake`, `group-polling` |
| `plugin-configuration` | `plugins`, `plugin-loading`, `edictum-settings`, `friend-tool-settings`, `main-merge` |
| `runtime-applications` | `control-plugin-application`, `runtime-application-progress`, `scoped-agent-apply`, `scoped-plugin-apply` |
| `account-relations` | `friends`, `account-requests` |
| `group-configuration` | `groups`, `group-chat-plugin` |
| `group-data` | `group-chat-media`, `group-chat-summaries`, `group-chat-memory-reminders` |
| `conversation-management` | `conversations`, `conversation-users`, `platform-archives` |
| `chat-media-flows` | `chat-history-media`, `chat-media`, `chat-fork-projects` |
| `chat-recovery-flows` | `chat-reconnect`, `checkpoint-branches`, `recovery-pagination` |
| `logging-flows` | `log-retention`, `log-stream` |
| `startup-forms` | `auth-startup`, `form-required` |
| `knowledge-flows` | `rag-settings`, `rag-upload` |

## 最终验证与统计

### 文件与行数口径

统计自动测试源文件及共享夹具. 后端包含 test_*.py, conftest, fixtures 和新增 support; 前端包含 *.test.*; 浏览器包含 e2e 的 mjs, HTML 与 Python 夹具. 固有手工/性能脚本未改变, 不计入本表. 本文另新增 1 个文件, 不计入测试源码行数

| 范围 | 文件数: 前 → 后 | 总行数: 前 → 后 |
|---|---:|---:|
| 后端自动测试与共享夹具 | 242 → 104 | 69,071 → 60,047 |
| 前端测试 | 38 → 18 | 2,785 → 2,774 |
| 浏览器场景, 运行器与辅助夹具 | 42 → 19 | 6,709 → 7,218 |
| 合计 | 322 → 141 | 78,565 → 70,039 |

自动测试与夹具减少 181 个文件 (56.2%), 总行数减少 8,526 行 (10.9%), 加上本文新增 1 个文件, 本轮文件净减少 180 个. 总行数包含空行, 注释及文档字符串; 非空行合计 66,623 → 61,741

后端按 Python token 行计数并排除 docstring, 有效代码行 50,389 → 49,898, 减少 491 行. 前端 TS/TSX token 行 2,518 → 2,516; 浏览器 mjs token 行 6,579 → 6,974, 增加 395 行, 主要来自共享运行器, 来源版本夹具与旧失败场景修复. 这些主要源码有效行合计仅净减少 98 行, 不把总行数减少误称为测试逻辑减少. HTML/Python 浏览器辅助夹具计入上表总行数, 未计入 mjs token 行

保留多数有效边界矩阵, 不为了行数删掉鉴权, 幂等, 并发, 来源归属和恢复断言. 文件整合, 夹具复用及运行入口复用是主要结果

### 验证

- 前端: 18 个模块, 299 项通过; TypeScript 与生产构建通过
- 浏览器: 39/39 场景通过, 原基线为 29 通过/10 失败
- 本机跨进程补验: 聊天恢复 3 个场景与插件应用 1 个场景通过
- 平台应用共享夹具与引用保护针对性回归: 111 项通过
- git diff --check 通过, 生产目录 satrap/ 无变更
- 后端完整回归: 4,016 通过, 22 跳过, 0 失败; 原基线 4,018 通过, 22 跳过, 0 失败. 跳过原因保持: 外部服务集成测试开关, reportlab 缺失和 Windows 无符号链接权限

### 时间

| 测试 | 原基线 | 最终结果 |
|---|---:|---:|
| 后端 pytest tests | 684.20 秒 | 606.78 秒 |
| 前端 Vitest | 4.49 秒 | 2.41 秒 |
| 浏览器全部场景 | 1,121.39 秒 | 629.52 秒 |

时间为同一机器本轮实测, 后端与浏览器部分并行执行, 前后负载未严格匹配; 不能解释为稳定性能基准. 浏览器原基线还有 10 个失败场景, 最终增加了这些场景的有效业务步骤, 同时消除了失效夹具导致的等待. 120 秒超时改为受控时钟, Chromium/Vite 复用减少了重复启动. 表中三个耗时的和只表示累计测试耗时, 不是并行流水线墙钟时间


三套运行器的累计实测耗时: 1810.08 → 1238.71 秒, 本轮下降 31.6%. 不将其解释为稳定性能收益或并行流水线墙钟耗时

后端 pytest 内部计时为 601.16 秒, 时间表采用含 Python/pytest 启动的外层进程墙钟计时, 与原基线口径一致

## 后续非必要测试清理

此阶段基于 `a384463`, 与上面的整合基线分开记录

- 删除已审计的 11 个用例: 根目录常量相等, MCP 占位文档, CLI 自动配置类型, 两个 forward 类型冒烟, 已被共享矩阵覆盖的通知合法值, 两个 JSON 格式检查及三个真实 DeepSeek 冒烟
- 删除 DeepSeek 专用配置读取/客户端构建, 路径常量和失去用途的导入
- 插件依赖扫描集中为一个用例, 保留旧 group_admin 工具排除/新昵称工具存在断言, 以及 Import/ImportFrom 的插件互相依赖检查
- RAG 重复超时/网络提示检查移除, 非对象输入 123 的独有回退断言合入保留用例
- 后端全量收集 4,038 → 4,026, 核对仅减少上述 11 项及合并掉的 1 项扫描, 其余用例 ID 与参数化场景一致
- 后端受影响模块回归 434 通过, 1 跳过; 前端 298 项通过, TypeScript 检查通过; 未重复运行未受影响的浏览器场景
- 测试源码新增 12 行, 删除 169 行, 净减少 157 行; 文件数不变, 生产代码无变更

## 冗余删除与夹具复用

此阶段基于上一节, 覆盖全库测试审计中确认可精简的项. 生产代码无变更

### 删除的用例与断言

| 位置 | 理由 |
|---|---|
| `test_control_ui_flows.py` 三处导入/别名元测试 | 零断言纯导入与符号 callable 复述, 已由参数化打包检查与真实调用覆盖 |
| `test_simple_session.py` 非 str 返回值同步用例 | 与 `test_handler_invalid_return_value_warns` 构造相同且后者多断言 warning |
| `test_llm_protocol_flows.py` 两处 `safe_parse_arguments` 用例 | 与保留用例断言等价, 其中一处弱于 `is arg` 身份断言 |
| `test_checkpoint_flows.py` 未知操作退出码用例 | 与 `test_cli_flows.py` 同名用例逐条断言相同, 该封装路径已被同文件其它用例走过 |
| `test_routing_flows.py` 来源适配器两用例 | 合并为参数化用例, 保留两个不同类级配置值 |

保留但有意不动的项: `test_wake_policy_flows.py` 的产物字段校验与导入顺序用例 (byte-for-byte 断言不覆盖契约字段语义), `test_onebot_adapter.py` 的 `Image(file=裸路径)` 拒绝用例 (媒体白名单矩阵无对等覆盖), 平台通知的 `_adapter` 与转发 `_adapter` (分别依赖"起点无 self_id"与无 `dict()` 转换), 各文件 `restore_manager` 导入 (`usefixtures` 字符串引用, pyflakes 误报)

### 提取到共享夹具

| 共享模块 | 来源 | 消除的副本 |
|---|---|---|
| `tests/support/providers.py` | `RunnableRegistry` | 8 个文件中的 11 份逐字相同替身 |
| `tests/support/adapters.py` | `RecorderAdapter` | 3 个文件中两两逐字相同的副本 |
| `tests/support/casts.py` | LLM/工具管理器 cast 工厂 | 2 个文件各 4 个函数 |
| `satrap-ui/src/test/helpers.ts` | `deferred` 与 Axios 响应工厂 | 3 处 deferred 与 2 处 response |

`RecorderAdapter` 统一为记录三元组 (会话, 消息链, 用途), 原二元组解包点相应更新; 子类改为调用父类记录后阻塞, 不再自行 append

### 未使用导入清理

按 `pyflakes` 报告逐条核验后删除测试文件与共享夹具中的死导入, 并保留全部 fixture 字符串引用; 修复 RAG 验证命令与群验证命令中的已删脚本路径, 重写 `tests/README.md` 的后端回归模块表

### 验证

- 后端全量: 3,984 通过, 33 跳过, 2 失败; 两条失败为 `test_redirected_management_directory_cannot_touch_outside_files` 的 `.runtime`/`.locks` 参数, 系本机 PATH 缺 PowerShell 的既有环境问题, 已用 stash 验证与本次改动无关
- 前端: Vitest 298 项通过, `tsc --noEmit` 0 错误, 改动文件 ESLint 0 错误
- 受影响模块分批复验: 会话/上下文组 381 通过, 平台组 609 通过, 调度/记录器组 169 通过
- `pyright -p .pyrightcfg` 无新增错误: 被提取符号与新增共享模块均 0 error
- 测试源码 48 个文件变更, 新增 182 行, 删除 633 行, 净减少 451 行; 新增 4 个共享夹具文件共 81 行

