# fix/issue1 分支代码质量 / 冗余 / 过度防御审计报告

日期: 2026-10-12。基线: `fix/issue1` vs `main`(702 文件, +112934/-4379)。
范围: 生产代码全量 (`satrap/`、`satrap-ui/src/`、`scripts/`), 不含 tests/docs/benchmark 结果; 九路并行审计汇总。
判定口径: 死代码 = 不向外暴露且内部零调用 (tests 引用单独注明); 动态入口 (注册表/插件工厂/路由表) 已排除误报。

## 复核结论 (2026-10-12, 主线对子代理发现全量逐条核验)

**P1/P2 与全部 P3 均已由主线直接读码 + grep 复验完毕**, 不再留有未复核项。

### P1/P2 复核

- **P1-1 属实**: launcher 只经 `_runtime_path` 写 `.satrap/runtime/release-instance.json` (launcher.py:73,199-201), `migrate_legacy_path` 在无旧文件时直接返回 runtime 新路径; smoke_release.py:130 监听旧路径且只检查 config.yaml 不存在 (不清空 .satrap), 全新发行目录下必然超时失败。
- **P2-1/P2-2 属实**: 5 个 snapshot 方法连测试都没有调用 (零调用, 非"仅测试"); 两个 `recover()` 生产无任何入口, BackendManager 只在降级时打 warning (BackendManager.py:3230-3231)。
- **P2-3 属实但需修正描述**: `_execute_request_decision` 的 `except Exception` 分支落的是 `"completed"` 且 **decision=None** (不是子代理说的 accepted/rejected); 但宿主 preflight 的 PermissionError 与 UnsupportedAdminAction 确实都会被记成 "completed" —— 动作从未发出却记为已完成, 结论成立。`_call` 对 PermissionError 原样重抛 (admin.py:303-304) 已核实。
- **P2-4 属实**: asr_references.py:8 docstring 明示"扫描与配置写入共用 REFERENCE_SCAN_LOCK", remove_llm_config/update_named_config 均遵守, 唯独 remove_asr_config 只持 `self._lock`。
- **P2-5 属实但后果降级**: 写回 → invalidate → epoch 失配 → 必交一次 SnapshotChangedError 重试; 归一化幂等时第二次成功, "三次耗尽构造失败"仅在归一化非幂等或并发写入时发生。与 6020336 修复的 model_config.json 读路径写回同族。
- **P2-6 属实**: `default_storage_layout = StorageLayout()` 解析 SATRAP_DATA_ROOT/默认 `.satrap/data`, 与 config.yaml 的 `data_root` 可不一致; cmd_session/cmd_edictum 均用 `StorageLayout(config.data_root)`。
- **P2-7/P2-8 属实**: administratorError/loggingError 是 errorText 的弱化版 (漏 detail 与 ApiError.code); ConfirmModal 全仓仅 Conversations/index.tsx 一处使用, window.confirm 仍有约 20 处 (含本分支新增的 UserView/AccountInvitations)。
- **P2-9 属实但一处存疑**: 两段路由核验确实雷同; 但"私聊版少一次群路由检查"大概率是私聊无群路由的合理差异, 不确认为漂移。

### P3 复核汇总 (全量)

**撤回 (误报, 共 4 条)**:
1. `RuntimeApplicationProgress.tsx:18` 禁用条件 —— 结果已知时正文已显示终态, 禁用查询按钮是合理设计, 非写反。
2. group_chat sticker 能力"不一致" —— tools.py:220 与 :380 两条路径都是"sticker 能力或 image 能力"双通道 (sticker 可退化为图片发送), 口径一致; 子代理漏看 or 前半支。
3. `GroupActions.tsx` 的 `ApiError` 未使用 import —— 该文件根本没有这个 import (现第 4 行是 Button), 且 action_type 标签已共享 `groupActionLabels.ts`。
4. 发送路径"enable 重复校验 3 次" (platform/__init__.py:400-402,436-437 + event.py:906) —— 是跨 await 的复核模式 (check-await-recheck), 项目刻意的并发再验证, 非冗余。

**复核不成立 (1 条)**: `platform/__init__.py:960-967` "terminate 与 dispatcher 双重 task_done 会超调抛 ValueError" —— 逐路径核对后每个 task_done 均与一次 get 配对, 正常退出顺序下两个排空循环不并发, 所述机制站不住 (仅存 check-then-get 的理论竞态, 非本报告所述后果)。

**降级或修正 (6 条)**:
- `plugin_settings.py` 三层 FRIEND_SWITCHES 过滤 —— 三层确实存在, 但读层过滤可能是针对存量旧配置的刻意防御, 建议补注释而非删层。
- `reminder_management.py:51-53` "复制 group_chat_data" —— 实为"平台存在性校验惯用法"在 5 个模块共享 (platform_message_data/group_chat_data×2/reminder_management/memory management), scope 解析本身已委托 `reminder_scope()`, 重复面比原述小。
- onebot "有界响应校验散落 4 处" —— `group_chat._read` 已是参数化 max_bytes, admin 的 GROUP_DIRECTORY_* 已复用; 真内联字面量只剩 get_message 的 65536, 降级为一致性建议。
- `memory/runtime.py:70-79` raise-即吞 —— 实为借外层 except 统一格式化 error_type, 风格冗余非缺陷。
- `client.ts:24` `typeof data === 'object'` —— axios 泛型只是编译期承诺, 拦截器/代理可返回非对象, 属对不可信响应的合理防御, 撤回。
- `control.ts` 与 `client.ts` 拦截器"逐行复制" —— 模式相同但 client.ts 多了 `_satrapBoundService` 绑定校验, 非逐行; 仍建议抽公共工厂。

**其余 P3 全部属实**, 抽查改为全查后确认的关键细节:
- onebot `get_group_list`/`get_message` 方法仅测试调用属实 (能力登记表 `_CAPABILITY_ACTIONS` 用的是动作名字符串做被动学习探测, 不经这些方法; group_admin 插件是显式 if 链分派, 也不触及)。
- 提醒不变量三处重复**已发生漂移**: 存储层 (reminders.py:143-148) 比另两处多了单条 256 字与控制字符检查。
- `_identity` 同型 ID 校验实际 **6 处** (administrator_groups/platform_messages/friends service/stickers/group_chat service/message_archive), 比原报告多 2 处。
- `_DEFINITIONS` needs_group 恒 True 实测 Counter: 12×(True,True) + 2×(False,True)。
- `GroupChatLimits.max_reply_images` 默认 4 范围 0-8, service.py:479 硬编码的是范围上限 8 而非配置值, 配置不生效属实。
- pyflakes 子代理只报了 9 条; 主线对本分支全部改动 .py 文件 (排除 __init__ 再导出) 实测 **86 条**, 全量清单见附录 A。
- fetch_ptt_text:550-552 逐字重实现 `_normalize_message_id` 的 `lstrip("-").isdecimal()` 校验, 属实。
- `Plugin.permissions`/`effective_config` 只写不读属实 (administrator_settings.py:274/286 读的是 PluginCatalogEntry 的同名属性, 与 Plugin 实例无关)。

---

## 一、P1 级 (需优先处理)

### P1-1 发行冒烟脚本监听旧状态文件路径, 发行构建必然超时失败
`scripts/release/smoke_release.py:130` 监听 `ROOT / ".satrap/release-instance.json"`, 而启动器实际写 `.satrap/runtime/release-instance.json` (`scripts/release/launcher.py:22,73`, 经 `get_runtime_path`)。`smoke_release.check_lifecycle()` 的等待条件永不成立 → `raise AssertionError("启动超时")`; `scripts/build_release.py:171` 以 `check=True` 调用, 构建直接失败。且 `smoke_release.main()` 会清空全部 `SATRAP_*` 环境变量, 无路径覆盖的侥幸。单测 `tests/unit/test_release_packaging.py:40` 也证实正确路径在 runtime/ 下。

## 二、P2 级

### 死代码

- **P2-1 `satrap/core/config/group_store.py:334,419,436,452,461` — 5 个 snapshot 方法零调用 (约 140 行)**: `access_snapshot`/`policy_snapshot`/`event_snapshot`/`revision_snapshot`/`route_snapshot` 全库仅定义行; 数据已由 `runtime_snapshot` 一次性返回。注意相邻的 `membership_snapshot` 被 `onebot/adapter.py:484` 调用, 勿误删。
- **P2-2 `satrap/core/pipeline/manual_wake_store.py:658` — `ManualWakeStore.recover()` 生产零调用**: docstring 承诺"降级只能经 recover() 校验后解除", 但无 HTTP/CLI/后台入口可达; 重启 `_startup_locked` 读到降级清单仍继续降级 → 生产环境一旦降级没有任何解除路径。同样形态: `satrap/core/platform/onebot/request_registry.py:467` `RequestApprovalLedger.recover()` (`BackendManager._attach_request_ledger` 只读 degraded 状态从不恢复)。

### 过度防御 / 语义缺陷

- **P2-3 `satrap/core/platform/onebot/admin.py:853` — `_execute_request_decision` 的宽 `except Exception` 把宿主 preflight 拒绝 (`PermissionError`) 与 `UnsupportedAdminAction` 一并记成 `completed` (decision=None)**; 平台动作实际从未发出, 账本却记为已完成, docstring 自述"平台给出明确结果记 completed"与实现不符。注: 复核确认 `_call` 对 PermissionError 原样重抛, 真实落入该分支。
- **P2-4 `satrap/core/framework/BackGroundManager.py:642` — `remove_asr_config` 未持 `REFERENCE_SCAN_LOCK`, 破坏 TOCTOU 防护**: 对照 `remove_llm_config` (:373) 与 `update_named_config` (:678) 都在锁内完成"扫描+写回"; 此处扫描与 `_save_locked()` 之间有窗口, 并发插件配置写入可新增引用后仍删掉 ASR 配置。
- **P2-5 `satrap/edictum/config.py:155,200,205` — 只读快照 loader 在加载过程中写回被指纹化的文件, 违反 SnapshotCache 契约**: `load()` 归一化触发 `_save_locked()` → `invalidate()` 使 epoch 失配 → 必然多一次 `SnapshotChangedError` 重试; 若归一化非严格幂等, 三次重试耗尽后构造失败。
- **P2-6 `satrap/cli/cmd_model.py:21,37` — ASR 删除/重命名引用扫描用全局 `default_storage_layout`, 忽略用户配置的 `config.data_root`**; 与 cmd_session.py:191 / cmd_edictum.py:39 的 `StorageLayout(config.data_root)` 写法不一致, 自定义数据目录下会漏检引用, 允许删除仍被占用的配置。

### 可复用未复用

- **P2-7 `satrap-ui/src/api/administrators.ts:134` `administratorError` 与 `api/logging.ts:30` `loggingError` 重复实现 `utils/errorText.ts:8` `errorText`**, 且功能更弱 (漏 `{detail}` 信封); 本分支已统一抽出 errorText (123 处调用), 这两处应收敛。同类: `api/rag.ts:30` `ragErrorMessage` 是第 4 份实现。
- **P2-8 前端 `ConfirmModal` (components/common/ConfirmModal.tsx) 几乎未被复用**: 全仓仅 `Conversations/index.tsx:347` 一处使用; 同域新增页面仍用原生 `window.confirm` (UserView.tsx:113,115、AccountInvitations.tsx:79,118、Friends/Sessions/Platforms/Checkpoints 等多处), 且 GroupMemories/GroupReminders/GroupSummaries/StickerLibrary 各自手写删除确认弹窗骨架, 与 ConfirmModal 雷同。
- **P2-9 `satrap/core/platform/__init__.py:412-435` 与 `:493-506` 路由核验两段雷同 (上轮审查已知) 仍在, 且 `group_chat_private_scope` 少一次群路由检查, 属平行实现漂移风险。**

## 三、P3 级 (按类别归并)

### 死代码 (零调用 / 仅测试调用)

| 位置 | 符号 | 备注 |
|---|---|---|
| satrap/core/pipeline/manual_wake_store.py:1199 | `pending_counts()` | 仅测试 |
| satrap/core/group_chat/reminders.py:342 | `ReminderStore.due()` | 调度器改用 `active`, 到期筛选成无消费者的平行实现 |
| satrap/core/group_chat/reply.py:56,140,148 | `ReplyTurn._output` | 只写不读, 可退化为计数器 |
| satrap/core/pipeline/request_diagnostics.py:108 | `capacity` property | 与 `_per_adapter` 重复, 零调用 |
| satrap/core/platform/request_inbox.py:245 | `RequestInbox.discard()` | 零调用 |
| satrap/core/platform/onebot/request_registry.py:758,1069 | `pending_counts()` / `cached_state()` | 仅测试 |
| satrap/core/platform/__init__.py:695 | `group_chat_native_stickers()` (复数) | 零引用; 被用的是单数 `..._component` |
| satrap/core/platform/onebot/adapter.py:489 | `group_active_revision()` | 零引用 |
| satrap/core/platform/onebot/onebot_utils.py:214 | `forward_ids_in_message()` | 零引用 |
| satrap/core/platform/onebot/admin.py:343,599 | `get_group_list()` / `get_message()` | 仅测试; 生产走 fetch_group_directory / group_chat reader |
| satrap/core/platform/event.py:370 | `MessageEvent.agent_binding_source` | 只写不读, 唯一读者是测试 |
| satrap/core/memory/store.py:380 | `MemoryStore.count()` | 零调用, 且不在 MemoryService 操作白名单内 |
| satrap/core/APICall/ASRCall/base.py:53 等 | `get_model()/get_api_key()/get_base_url()/set_parameters()` | 沿袭 APICall 各子模块既有风格 (LLM/Embed/ReRank 同样零调用), 沿袭性冗余 |
| satrap/edictum/plugin.py:676-677 | `Plugin.permissions` / `effective_config` | 只写不读 (绑定用的是局部变量) |
| satrap/edictum/plugin_migrations.py:131-135 | `explicit` 恒真后的不可达分支 | 冗余防御+死分支 |
| satrap/core/config/administrator_groups.py:193,204 | `normalize_administrator_overrides` 的 `strict_scope` 参数 | 从不被读取, 纯签名装饰 |
| satrap/expend/plugins/group_admin/tools.py:29-87 | `_DEFINITIONS` 第 5 字段恒为 True | `needs_group` else 分支不可达 |
| satrap-ui/src/api/edictum.ts:65 | `retryFullRuntimeChanges` | 仅测试引用; 页面走 createEdictumApplication |
| satrap-ui/src/utils/wakePolicyContract.ts | `PolicyFieldKind`/`PolicyFieldScope`/`PolicyIssue`/`POLICY_FIELDS`/`POLICY_FIELD_LABELS`/`policyFieldLabel` | 无跨模块消费者 |
| satrap-ui/src/api/configVersions.ts | `ConfigVersion`/`ConfigVersions`/`sameConfigVersions` | 仅模块内部使用, 多余导出 |
| satrap-ui/src/utils/wakeOverrides.ts:156,183 | `RowsConversion`/`parseOverrideJson` | 多余导出 |
| satrap-ui/src/components/diagnostics/RequestDiagnosticsPanel.tsx:9 | `STAGE_LABELS` 导出 | 无外部引用 |
| satrap-ui/src/pages/Conversations/GroupReminders.tsx:12 | `reminderStateLabels` 导出 | 无外部引用 |
| satrap-ui/src/components/common/ConfirmModal.tsx / FormModal.tsx | `confirmText` / `cancelText` props | 声明了默认值但无任何调用方传入 |
| ~~satrap-ui/src/pages/Groups/GroupActions.tsx:4~~ | ~~`ApiError` 未使用 import~~ | **复核撤回 (误报)**: 该文件无此 import |

未使用导入/赋值 (pyflakes 实测): 子代理原报 9 条 (`group_chat/stickers.py:7` Path、`manual_wake_store.py:23` Iterator 与 :555 局部 `bad`、`request_diagnostics.py:11` deque、`wake_dry_run.py:12` SOURCE_PLATFORM、`wake_window.py:5` Any、`group_chat/reply.py:26-28` MessageChain 双导入、`group_chat/service.py:379` 未使用的 `exc`、`platform_messages.py:769` field 遮蔽) 全部属实; **主线对本分支全部改动 .py 文件复跑 pyflakes 共 86 条 (排除 __init__ 再导出)**, 详见附录 A。

### 可复用未复用 (重复实现)

- `satrap/core/config/session_class_service.py:43-59` 与 `edictum_service.py:55-70` — `_guard_reference` 15 行逐字复制, 仅字面量不同。
- `satrap/core/config/asr_references.py:83-102` 与 `group_references.py:22-38` — 插件字段枚举平行实现 (后者已复用前者的 `_plugin_dirs`, 合并可行)。
- `satrap/core/config/platform_messages.py:36-48` `_identity` 与 `group_chat/service.py:64-73` `_id` 等 — 同型 ID 校验至少 4 份 (`grep "ord(c) < 32"`)。
- 覆盖表名 `"session_config_overrides"` 硬编码 3 处 (asr_references.py:24、platform_schema.py:14、session_overrides.py 内嵌 SQL), 靠注释人工同步。
- 文件身份指纹 (mtime/ctime/size/ino) 平行实现 3 遍: `edictum/plugin_apply.py:23-66`、`plugin_catalog.py:100-122`、`config.py:173-181`; "3 次重试后抛持续变化" 循环也重复。
- `GLOBAL_RECEIPT` 常量 (`friend_migration.py:21`) 未复用, `"__capability_migration"` 在 plugin_config.py:257,315,316 硬编码。
- 原子 JSON 写入模式复制 3+ 份 (friend_migration.py:64-71、memory_migration.py:56-59、plugin_config.py:311-320、display/plugins.py:151、config/document.py:413), 而本分支新增的共享原语 `storage/persist.py:16 atomic_write_json` 仅 2 处使用; 且迁移模块跨模块访问私有 `manager._global_path(...)`。
- `group_chat/reminders.py:245-265` `_offset` 与 `memory/scoped.py:230-250` 逐字重复 (同为本分支新增); "签名+offset" 分页整套重复。
- ~~`group_chat/reminder_management.py:51-53` 复制 `config/group_chat_data.py:43-47`~~ **复核降级**: 实为"平台存在性校验惯用法"在 5 个模块共享 (platform_message_data/group_chat_data×2/reminder_management/memory management), scope 解析已委托 `reminder_scope()`, 重复面很小。
- 提醒文本/mentions 不变量三处重复校验: reminders.py:143-148、reminder_host.py:285-289、reminder_service.py:102-121 (**已发生漂移**: 存储层比另两处多了单条 256 字与控制字符检查)。
- `framework/BackGroundManager.py:404` `_check_llm_in_use` 与 `:666` `_check_asr_in_use` 逐行雷同; 外层加锁+内层删除模式也未复用。
- `framework/SessionManager.py:1767` 与 `:1978` 两个 `reconcile_one` 平行实现 (复核样板高度重叠)。
- `platform/event.py:374-401` 与 `platform/__init__.py:232-246` — 群会话路由解码两处平行实现。
- `platform/event.py:906-913` 与 `:942-949` — send/send_streaming 守卫块整段复制。
- `onebot/admin.py:486-491` 与 `:518-523` — get_image/get_record 媒体标识校验重复; `:550-552` fetch_ptt_text 重新实现消息 ID 校验 (已有 `_normalize_message_id`)。
- onebot 数字身份校验三套: `group_chat.py:22-41 _numeric_id` 与 `admin.py:159-195 _normalize_decimal/_normalize_message_id` (friends.py 依赖前者)。
- `onebot/friends.py:259-291` verify_record 与 `admin.py:832-861` verify_request — 同一 preflight 复核模式复制两份。
- ~~onebot 有界响应校验散落 4 处各写常量~~ **复核降级**: `group_chat._read` 已参数化 max_bytes, `GROUP_DIRECTORY_*` 已在 admin 内复用; 真内联字面量只剩 `get_message` 的 65536, 属一致性建议。
- `onebot/group_action_types.py:39-94` 与 `admin.py` 各动作方法 — 同一参数不变量两层校验。
- `satrap/expend/plugins/satrap_coding/tools/__init__.py:97-111` 与 `base_take/tools/__init__.py:69-71` — `allowed_env_vars` 逗号拆分一字不差两份。
- `satrap/expend/plugins/friend_manager/tools.py:199,201,225,227` — 同一异常被 `failure()` 解析两次 (打日志一次 + `_failure_result` 内部一次)。
- CLI 三处手写 `list_agent_references(...)` lambda 组装 (cmd_model:34-38、cmd_session:187-193、cmd_edictum:36-40)。
- 前端: `api/control.ts:104` 与 `api/client.ts:31` 复制同一套鉴权拦截器 (401 重放/`_satrapAuthRetry`); Groups/ 四个页面复制 `actionStateLabels` 映射; `Logs/index.tsx:15-19 LogLine` 与 `Logs/VirtualLogList.tsx:3 LogEntry` 字段完全相同 (还需强转)。

### 过度防御性代码

- `asr_references.py:153-154` — `except AsrReferenceScanError: raise` 不可达 (RuntimeError 不被 `except sqlite3.Error` 捕获)。
- `agent_references.py:59-61` 与 `agent_routing.py:140-142,166-168,212-214` — catch-记日志-原样重抛的噪音式防御。
- `plugin_settings.py:52-56,72-73,186-187` — 好友旧开关字段过滤同一不变量三层重复 (validator/get/resolve) (**部分属实**: 读层过滤可能是针对存量旧配置的刻意防御, 建议补注释而非删层)。
- `friend_migration.py:80-82,169-171`、`memory_migration.py:61-63,108-110` — except-记堆栈-重抛四处。
- `group_chat/assets.py:201` — `_LEASES.get(self.path, 1)` 默认 1 掩盖引用计数不一致。
- `group_chat/stickers.py:192` — `blob_key is not None` 与 `payload is not None` 等价的冗余校验。
- `manual_wake_store.py:594-596` — `manifest is None` 抛错分支不可达。
- `input_projection.py:371` — `_MediaBudget.merge(origin="context")` 默认值从未生效。
- `framework/session_discovery.py:56` — `existing is None and module_name in sys.modules` 近乎不可达的冗余再校验。
- `onebot/admin.py:45,311-313` — retcode 范围校验写两遍 (+is_missing_action_error 第三次读同字段); `:305` `_call` 宽 except 把编程错误也归一为 AdminActionUnconfirmed。
- ~~`platform/__init__.py:400-402,436-437` + event.py:906 + adapter `_send_chunk` — 一次发送路径 enable/群范围重复校验 3 次~~ **复核撤回**: 是跨 await 的 check-await-recheck 复核模式, 项目刻意的并发再验证。
- `memory/runtime.py:70-79` — 内层 raise 的 MemoryError 被同函数外层 except 立即吞掉, 等价于直接 return (复核降级: 借外层统一格式化 error_type, 风格冗余非缺陷)。
- 前端: ~~`api/client.ts:24` 对已声明对象类型再做 `typeof === 'object'`~~ **复核撤回** (axios 泛型仅编译期承诺, 不可信响应的合理防御); `stores/useBackendStore.ts:80` `catch {}` 静默吞控制服务错误 (与 refreshHealth 不一致, 属实); `pages/Groups/GroupEvents.tsx:45-52` `Promise.allSettled` 当 `Promise.all` 用还吞掉另一路 rejection (属实)。

### 一般代码质量 / bug 隐患

- `satrap/expend/plugins/group_admin/tools.py:11,136` — 权限边界 `allowed_groups` 用宽松 `config_ids` 解析, 与同批插件一律用 `permission_id_list` 相反 (`core/plugin_authorization.py:73-88` docstring 明确要求权限名单走后者)。
- `satrap/expend/plugins/memory/tools/base.py:136` — `"invalid_argument"` 单数, 同批插件一致用 `"invalid_arguments"`, 下游按类型匹配会失效。
- `satrap/expend/plugins/group_chat/tools.py:218-220` vs `:380` — ~~sticker 组件 schema 与 list_stickers 能力口径不一致~~ **复核撤回 (误报)**: 两条路径都是"sticker 能力或 image 能力"双通道 (sticker 可退化为图片发送), 口径一致。
- `satrap/core/memory/service.py:154` — 群记忆写操作 `repository.mutate` 在事件循环上同步执行 (读路径都走 `asyncio.to_thread`), 阻塞循环。
- `satrap/core/group_chat/reminders.py:88` — `delivery_json` 为 NULL 时不弹出, 内部字段泄漏进对外记录 (对照其他 *_json 字段无条件 pop)。
- `satrap/core/group_chat/service.py:479` — 单条消息图片上限硬编码 8, 未用 `GroupChatLimits.max_reply_images`。
- `satrap/core/pipeline/input_projection.py:54-56` — `ProjectedInput` 相邻裸字符串, 说明归属混乱 (无意义表达式语句)。
- `satrap/core/config/platform_messages.py:769` — 循环变量 `field` 遮蔽 `dataclasses.field` 导入 (pyflakes 确认)。
- `satrap/core/platform/onebot/admin.py:61` — 悬空字符串字面量; `ADMIN_TIMEOUT` 的 docstring 错放位置。
- `satrap/core/platform/onebot/request_registry.py:968` — `len(request_id) != 35` 硬编码收件箱 ID 形状, 依赖 uuid4 hex 长度, 一侧改动即静默失效。
- ~~`satrap/core/platform/__init__.py:960-967` 与 dispatcher finally 双重 `task_done()` 超调抛 ValueError~~ **复核不成立**: 每个 task_done 均与一次 get 配对, 正常退出顺序下两排空循环不并发, 所述机制站不住。
- `satrap/edictum/simple_session/async_.py:103-104` (sync.py:91-92 同) — 归一化结果被丢弃, `default_thinking` 用未归一化原值。
- `satrap/edictum/plugin_settings.py:204` — snapshot 分支下 `PluginConfigManager()` 被无条件构造但零使用。
- `satrap/display/service.py:240` vs `:213` — `_shared_runtime_creation` 前缀匹配只单向, 与独占操作的双向规则不对称 (当前唯一调用方不触发, 潜在缺口)。
- `scripts/build_release.py:117` — `next(wheel_dir.glob(...))` 空结果抛裸 `StopIteration`, 无明确构建失败提示。
- 前端: ~~`components/common/RuntimeApplicationProgress.tsx:18` 禁用条件疑似写反~~ **复核撤回 (误报)**: 结果已知时正文已显示终态, 禁用查询按钮是合理设计; `pages/Plugins/index.tsx:21,128,131` — `?tab=stickers` 在非 group_chat 插件下静默落到 PluginUsages 且无高亮; `pages/Sessions/EdictumSessionsPanel.tsx:113-118` — `Promise.all` 传 4 个解构 3 个, 第 4 个结果静默丢弃 (复核属实, 第 4 项 fetchEdictumConfigs 自带 setState, 属可读性问题)。

## 四、审计过但未发现问题的领域 (摘要)

- wake window 核心 / wake_policy / 命令入口 / 调度器新增阶段 / 手动唤醒 / 媒体附件 / 摘要存储 — 调用链闭合, 无死分支。
- edictum 的 plugin_archive / plugin_permissions / settings / plugin_catalog / plugin_apply / plugin_runtime / plugin_resources — 均有生产消费方, 校验无重复。
- config 的 platform_policy 契约驱动校验、wake_overrides、document 原子保存、versions 探测 — 无问题。
- platform 的 notices/receipt/scheduled/message_archive/outbound/self_identity/forwarding/misskey — 未见缺陷。
- storage/durability、persist、application_progress、snapshot_cache、memory scoped/lifecycle/management、context_catalog — 符号均有调用方。
- expend 插件的沙箱路径保护、异常映射类型序、请求处理契约 — 无问题; 无静默 `except: pass`。
- 前端大部分 api/hooks/stores 与页面 — API 契约与后端逐条比对一致; Modal/Tabs/Toggle 等基础组件无问题; `/users` 路由残留已确认清除。
- 上轮已知债务 (语音 canonical key、SessionManager 宽 except、sync/async 双写、execution/workflow 平行实现、platform 路由两段雷同已计入 P2-9) 未重复上报。

---

## 附录 A: pyflakes 全量清单 (本分支改动 .py 文件, 排除 __init__ 再导出, 共 86 条)

复现命令: `python -m pyflakes $(git diff main...HEAD --name-only -- satrap/ | grep '\.py$' | grep -v '__init__.py')`

注意: 缩进列 (:5:) 的多为 `if TYPE_CHECKING:` 块内的死导入; 本清单含存量问题, 非全部为本分支引入。

```
    satrap/cli/cmd_control.py:15:1: 'satrap.cli.output.info' imported but unused
    satrap/core/backend/BackendManager.py:13:1: 'signal' imported but unused
    satrap/core/backend/BackendManager.py:16:1: 'typing.Optional' imported but unused
    satrap/core/backend/BackendManager.py:64:1: 'satrap.core.platform.PlatformAdapterRegistry' imported but unused
    satrap/core/backend/BackendManager.py:3404:13: 'satrap.core.platform.onebot.adapter' imported but unused
    satrap/core/backend/http_api.py:37:1: 'satrap.core.type.safe_getattr' imported but unused
    satrap/core/backend/http_api.py:37:1: 'satrap.core.type.safe_getattr_str' imported but unused
    satrap/core/components/message.py:13:1: 'typing.cast' imported but unused
    satrap/core/config/platform_messages.py:769:13: import 'field' from line 6 shadowed by loop variable
    satrap/core/framework/Base/async_session.py:28:5: 'satrap.core.framework.SessionManager.SessionManager' imported but unused
    satrap/core/framework/Base/async_workflow.py:11:1: 'typing.TYPE_CHECKING' imported but unused
    satrap/core/framework/Base/async_workflow.py:14:1: 'satrap.core.utils.context.add_bot_message' imported but unused
    satrap/core/framework/Base/async_workflow.py:14:1: 'satrap.core.utils.context.add_tools_call_flow' imported but unused
    satrap/core/framework/Base/async_workflow.py:14:1: 'satrap.core.utils.context.clear_reasoning_content' imported but unused
    satrap/core/framework/Base/execution/engine.py:22:1: '.flow.ToolStep' imported but unused
    satrap/core/framework/Base/sync_session.py:26:5: 'satrap.core.framework.SessionManager.SessionManager' imported but unused
    satrap/core/framework/Base/sync_workflow.py:14:1: 'satrap.core.utils.context.add_bot_message' imported but unused
    satrap/core/framework/Base/sync_workflow.py:14:1: 'satrap.core.utils.context.add_tools_call_flow' imported but unused
    satrap/core/framework/Base/sync_workflow.py:14:1: 'satrap.core.utils.context.clear_reasoning_content' imported but unused
    satrap/core/framework/Base/sync_workflow.py:24:5: 'satrap.core.config.session_overrides.SessionOverrideStore' imported but unused
    satrap/core/framework/Base/sync_workflow.py:25:5: 'satrap.core.framework.BackGroundManager.ModelConfigManager' imported but unused
    satrap/core/framework/Base/sync_workflow.py:26:5: 'satrap.core.storage.layout.StorageLayout' imported but unused
    satrap/core/framework/Base/sync_workflow.py:27:5: 'satrap.core.framework.SessionManager.SessionManager' imported but unused
    satrap/core/framework/Base/sync_workflow.py:28:5: 'satrap.core.framework.UserManager.UserManager' imported but unused
    satrap/core/framework/Base/utils.py:20:5: 'satrap.core.config.session_overrides.SessionOverrideStore' imported but unused
    satrap/core/framework/Base/utils.py:21:5: 'satrap.core.framework.BackGroundManager.ModelConfigManager' imported but unused
    satrap/core/framework/Base/utils.py:22:5: 'satrap.core.storage.layout.StorageLayout' imported but unused
    satrap/core/framework/Base/utils.py:23:5: 'satrap.core.framework.SessionManager.SessionManager' imported but unused
    satrap/core/framework/Base/utils.py:24:5: 'satrap.core.framework.UserManager.UserManager' imported but unused
    satrap/core/framework/SessionManager.py:1450:28: f-string is missing placeholders
    satrap/core/group_chat/reply.py:28:1: redefinition of unused 'MessageChain' from line 26
    satrap/core/group_chat/service.py:379:9: local variable 'exc' is assigned to but never used
    satrap/core/group_chat/stickers.py:7:1: 'pathlib.Path' imported but unused
    satrap/core/pipeline/manual_wake_store.py:23:1: 'collections.abc.Iterator' imported but unused
    satrap/core/pipeline/manual_wake_store.py:555:9: local variable 'bad' is assigned to but never used
    satrap/core/pipeline/request_diagnostics.py:11:1: 'collections.deque' imported but unused
    satrap/core/pipeline/wake_dry_run.py:12:1: 'satrap.core.config.wake_overrides.SOURCE_PLATFORM' imported but unused
    satrap/core/pipeline/wake_window.py:5:1: 'typing.Any' imported but unused
    satrap/core/platform/event.py:15:1: 'copy.deepcopy' imported but unused
    satrap/core/platform/misskey/adapter.py:8:1: 'os' imported but unused
    satrap/core/platform/onebot/group_action_types.py:4:1: 'typing.Any' imported but unused
    satrap/core/type.py:16:1: 'satrap.core.components.PlatformComponentType' imported but unused
    satrap/core/utils/context/async_.py:24:5: 'satrap.core.APICall.LLMCall.LLM' imported but unused
    satrap/core/utils/context/async_.py:24:5: 'satrap.core.APICall.LLMCall.AsyncLLM' imported but unused
    satrap/core/utils/context/async_.py:25:5: '.sync.ContextManager' imported but unused
    satrap/core/utils/context/async_.py:29:1: redefinition of unused 'AsyncContextManager' from line 26
    satrap/core/utils/context/async_storage.py:32:5: 'satrap.core.APICall.LLMCall.LLM' imported but unused
    satrap/core/utils/context/async_storage.py:32:5: 'satrap.core.APICall.LLMCall.AsyncLLM' imported but unused
    satrap/core/utils/context/async_storage.py:33:5: '.sync.ContextManager' imported but unused
    satrap/core/utils/context/async_storage.py:34:5: '.async_.AsyncContextManager' imported but unused
    satrap/core/utils/context/async_summary.py:20:5: 'satrap.core.APICall.LLMCall.LLM' imported but unused
    satrap/core/utils/context/async_summary.py:21:5: '.sync.ContextManager' imported but unused
    satrap/core/utils/context/async_summary.py:22:5: '.async_.AsyncContextManager' imported but unused
    satrap/core/utils/context/base.py:19:5: 'satrap.core.APICall.LLMCall.LLM' imported but unused
    satrap/core/utils/context/base.py:19:5: 'satrap.core.APICall.LLMCall.AsyncLLM' imported but unused
    satrap/core/utils/context/base.py:393:13: local variable 'system_turn' is assigned to but never used
    satrap/core/utils/context/sync.py:25:5: 'satrap.core.APICall.LLMCall.LLM' imported but unused
    satrap/core/utils/context/sync.py:25:5: 'satrap.core.APICall.LLMCall.AsyncLLM' imported but unused
    satrap/core/utils/context/sync_storage.py:32:5: 'satrap.core.APICall.LLMCall.LLM' imported but unused
    satrap/core/utils/context/sync_storage.py:32:5: 'satrap.core.APICall.LLMCall.AsyncLLM' imported but unused
    satrap/core/utils/context/sync_summary.py:20:5: 'satrap.core.APICall.LLMCall.AsyncLLM' imported but unused
    satrap/core/utils/context/utils.py:16:1: 'satrap.core.utils.vision.estimate_content_image_count' imported but unused
    satrap/core/utils/context/utils.py:28:5: 'satrap.core.APICall.LLMCall.AsyncLLM' imported but unused
    satrap/core/utils/context/utils.py:28:5: 'satrap.core.APICall.LLMCall.LLM' imported but unused
    satrap/core/utils/text_utils.py:71:9: local variable 'final_chunks' is assigned to but never used
    satrap/expend/plugins/group_chat/tools.py:29:1: 'satrap.edictum.plugin_resources.PluginResources' imported but unused
    satrap/expend/plugins/satrap_coding/tools/async_interaction.py:21:1: '.shell._run_shell' imported but unused
    satrap/expend/plugins/satrap_coding/tools/async_interaction.py:134:9: redefinition of unused '_run_shell' from line 21
    satrap/expend/plugins/satrap_coding/tools/paths.py:17:1: '.constants.WORKSPACE_ROOT' imported but unused
    satrap/expend/plugins/satrap_coding/tools/paths.py:17:1: '.constants._PROTECTED_DIRS' imported but unused
    satrap/expend/plugins/satrap_coding/tools/paths.py:17:1: '.constants._PROTECTED_FILES' imported but unused
    satrap/expend/plugins/satrap_coding/tools/paths.py:17:1: '.constants._SYSTEM_PROTECTED_ROOT' imported but unused
    satrap/expend/plugins/satrap_coding/tools/paths.py:17:1: '.constants._FILE_LINE_BREAK' imported but unused
    satrap/expend/plugins/satrap_coding/tools/paths.py:17:1: '.constants.DEFAULT_SANDBOX_ROOT' imported but unused
    satrap/expend/plugins/satrap_coding/tools/paths.py:39:5: redefinition of unused 'WORKSPACE_ROOT' from line 17
    satrap/expend/plugins/satrap_coding/tools/paths.py:75:5: redefinition of unused 'WORKSPACE_ROOT' from line 17
    satrap/expend/plugins/satrap_coding/tools/paths.py:144:5: redefinition of unused 'WORKSPACE_ROOT' from line 17
    satrap/expend/plugins/satrap_coding/tools/paths.py:144:5: redefinition of unused '_PROTECTED_DIRS' from line 17
    satrap/expend/plugins/satrap_coding/tools/paths.py:144:5: redefinition of unused '_PROTECTED_FILES' from line 17
    satrap/expend/plugins/satrap_coding/tools/paths.py:144:5: redefinition of unused '_SYSTEM_PROTECTED_ROOT' from line 17
    satrap/expend/plugins/satrap_coding/tools/paths.py:188:5: redefinition of unused 'DEFAULT_SANDBOX_ROOT' from line 17
    satrap/expend/plugins/satrap_coding/tools/paths.py:271:5: redefinition of unused '_FILE_LINE_BREAK' from line 17
    satrap/expend/plugins/satrap_coding/tools/sync_interaction.py:19:1: '.shell._run_shell' imported but unused
    satrap/expend/plugins/satrap_coding/tools/sync_interaction.py:152:9: redefinition of unused '_run_shell' from line 19
    satrap/expend/tools/agent/sync.py:1:1: 'multiprocessing' imported but unused
    satrap/expend/tools/agent/sync.py:105:9: redefinition of unused 'multiprocessing' from line 1
```
