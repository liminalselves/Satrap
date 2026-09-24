# Issue #10 目标审计 B1-B10 对照表与关闭证据

日期: 2026-09-24
范围: [修复后独立复审](issue-10-goal-audit-recheck-2026-09-24.md) 提出的 B1-B10
实施: [整改方案](issue-10-goal-audit-remediation-plan-2026-09-24.md) 批次 5-8, 分支 `fix/issue10`

本表按复审列出的十项逐项给出关闭条件、实现位置、反例测试与实测结果, 并列出仍未关闭的边界。表中"反例"一律指复审给出的具体失败行为; 每项的反例测试都在实现前先能复现失败, 关键项做过变异验证 (把修复改回旧行为即变红)。

## 1. 提交对照

| 批次 | 提交 | 内容 |
| --- | --- | --- |
| 5 | `b81a981` | B1 转发来源证明、B2 降级持久化与清单事务、B4 审批账本防重放 |
| 6 | `5ae0200` | B3 发送段证据与请求裁决、B7 文件未确认语义、B5 ASR 扫描完整性、B10 诊断后端 |
| 7 | `62c061a` | B6 有效零值先于到期补偿、B10 平台配置字段与策略来源 |
| 8 | `97cf232` | B8 草稿行由表单持有、B9 数据路由离开拦截、B10 诊断前端 |

实施基线: `4d8d314` (批次 1-4 完成后的修复前状态)。逐批细节见 [实施记录](issue-10-progress.md) 的目标审计批次 5-8 各节。

## 2. 逐项对照

### B1 / P1: 转发读取必须证明来源消息与 forward_id 的关联

| 项 | 内容 |
| --- | --- |
| 复审反例 | 允许群号 + 异群 forward_id 可以通过新增读取工具取到异群转发内容 |
| 关闭条件 | 异群来源、来源消息不含该 ID、错误账号、回源失败/不支持、等待中改配置均拒绝且 `get_forward_msg` 调用数为 0; 正确来源通过; 同步与异步工具入口都覆盖 |
| 实现 | `satrap/core/platform/onebot/admin.py` (`get_forward_message` 先回源核验群/类型/账号/消息 ID, 再要求 `forward_id ∈ forward_ids_in_message(顶层组件)`, 回源前后各复查群范围与连接代次); `onebot_utils.forward_ids_in_message` 只取顶层不递归; `expend/plugins/group_admin/tools.py` 新增必填 `source_message_id`, 缺失即参数错误 |
| 反例测试 | `tests/unit/test_onebot_admin.py` 新增 8 项: `test_get_forward_message_requires_source_proof`、`test_source_message_from_other_group_is_rejected`、`test_forward_id_absent_from_source_message_is_rejected`、`test_nested_forward_does_not_authorize`、`test_account_and_message_id_mismatch_are_rejected`、`test_lookup_failure_and_stale_generation_block_read`、`test_response_with_contradictory_group_is_rejected`、`test_missing_source_message_id_is_a_parameter_error`; 工具层 `tests/unit/test_group_admin_plugin.py` |
| 实测 | 上述用例全部通过; 断言同时检查 `get_forward_msg` 调用次数为 0 (不只是返回值)。变异验证: 去掉转发 ID 成员检查后相关用例变红 |
| 剩余限制 | 证明范围限于"当前连接的当前账号 + 目标群", 连接代次变化后需要重新走一次核验; 未对平台侧转发内容本身做真伪校验 (上游返回什么就是什么) |

### B2 / P1: 降级状态持久化, 主文件与归档使用同一恢复规则

| 项 | 内容 |
| --- | --- |
| 复审反例 | 坏文件隔离后再次启动丢失降级状态, 去重账本被重置为可写空库 |
| 关闭条件 | 主文件损坏→隔离→连续重启仍降级; 归档损坏同样拒绝; 已初始化文件丢失拒绝; 原 request_id 不重新 accepted; 合法旧数据迁移不丢记录; 文件锁与归档轮转各写入点故障后不出现记录复活或消失 |
| 实现 | `satrap/core/storage/persist.py` (`atomic_write_json`/`quarantine_file` 共用原语) + `manual_wake_store.py` 引入 `MANIFEST_VERSION=1` 清单 (`expected_files` 三态 + `degraded{reason,at}`), 清单与主/归档同事务; 启动分流"清单可读性 × 数据文件存在性", 清单损坏但有数据文件→校验后迁移而非初始化; 固定顺序"先持久降级再隔离"; 记录校验加严 (身份、空 ID、段状态枚举、`sent` 不得含未确认段、重叠检测) |
| 反例测试 | `tests/unit/test_manual_wake_store.py` 的 `TestPersistentDegradation` 10 项 (`test_corrupt_main_stays_degraded_across_restarts`、`test_archive_corruption_degrades_without_empty_fallback`、`test_missing_expected_files_are_corruption_not_empty_store`、`test_manifest_corrupt_with_data_migrates_instead_of_resetting`、`test_manifest_corrupt_and_main_unreadable_degrades_without_reset`、`test_degrade_marker_write_failure_keeps_original_file`、`test_legacy_files_migrate_without_record_loss`、`test_rotation_crash_keeps_records_and_adopts_archive`、`test_failed_persist_does_not_show_unconfirmed_terminal_state` 等) + `TestRecoveryEntry` 4 项 |
| 实测 | 全部通过。变异验证: 把"缺清单按首次初始化"改回旧行为后迁移用例变红; 去掉降级标记先落盘的顺序后隔离用例变红 |
| 剩余限制 | 存储仍是 JSON 文件方案 (按方案不迁移数据库); 降级后只允许"恢复原文件"或"读取修复后的原文件"两条恢复通道, 不提供清空历史继续的路径 |

### B3 / P2: 根据已提交副作用裁决请求结果

| 项 | 内容 |
| --- | --- |
| 复审反例 | 发送开始后取消, 手动请求状态被记为明确失败 (`failed`), 已提交副作用被抹掉 |
| 关闭条件 | I/O 前取消、首段 I/O 后取消、确认一段后取消、重复取消、停止平台、模型超时但工具已写出、收尾超时、尝试落盘失败均得到规定状态; 不重复发全文; 查询与段记录一致 |
| 实现 | `onebot/adapter.py` 段级 `planned→submitted→sent/partial/failed/unknown/skipped`, 每段 I/O 前推进并立即落该段结果; `manual_wake_store.derive_attempt_status` 按段证据归并; `scheduler._adjudicate_manual_request` 以同一 `request_id` 的业务尝试为准 (确认前缀+未尝试→`partial`, 有未确认段→`unknown`, 全确认→`sent`, 未提交→`failed/cancelled_before_send`); `receipt.py` 有界收尾 `asyncio.wait_for(asyncio.shield(worker), 2.0)`; `purpose` 区分业务/错误反馈, 旧记录缺 `purpose` 只提高保守程度 |
| 反例测试 | `tests/unit/test_manual_wake_store.py`: `test_cancel_before_any_io_is_failed_cancelled_before_send`、`test_cancel_after_first_segment_is_unknown_with_confirmed_prefix`、`test_llm_timeout_does_not_erase_tool_delivery`、`test_error_feedback_receipt_does_not_mark_business_sent`、`test_tracking_failure_refuses_business_send`、`test_legacy_attempt_without_purpose_is_not_business_evidence`; `tests/unit/test_onebot_adapter.py` 段状态与收尾超时用例 |
| 实测 | 全部通过, 断言段状态与持久化两侧一致; 收尾超时用例确认"段已确认、尝试仍未终结、不重发、后续可信确认可精化"。变异验证: 去掉有界等待即变红 |
| 剩余限制 | 收尾等待上限 2 秒, 超过后结果停留在 `unknown` 直到新的可信确认; 平台不返回回执时只能靠段证据与保守归并 |

### B4 / P2: 审批账本与可用请求缓存分离

| 项 | 内容 |
| --- | --- |
| 复审反例 | flag 从 512 条缓存淘汰后, 重复入站让审批资格恢复, 可二次执行管理动作 |
| 关闭条件 | 容量缩小反例、TTL 过期后重投、重复入站、并发审批、占用后崩溃重启、账号/请求类别冲突、账本损坏/满载均不能产生第二次写动作; 断言动作次数而不只检查内存状态 |
| 实现 | `onebot/request_registry.py` 新增 `RequestApprovalLedger` (JSON + 清单 + 文件锁): 键 `adapter_id\nself_id\nkind\nflag 摘要` (只存 sha256 前缀), 锁覆盖"重读→判定→占用→落盘"全程; 过期转 `expired` 墓碑不删身份; 容量达限拒绝新登记; 重启把 `executing` 记 `unknown`; `RequestFlagRegistry` 降级为 512 条近期缓存门面 |
| 反例测试 | `tests/unit/test_onebot_admin.py` 的审批账本与门面/适配器层用例 (16 项新增): `test_cache_eviction_keeps_ledger_identity`、`test_duplicate_inbound_does_not_reset_occupied_state`、`test_expired_flag_cannot_be_occupied`、`test_concurrent_occupy_has_single_winner`、`test_dual_instance_concurrent_occupy_single_winner`、`test_cancel_during_occupy_keeps_executing_and_sends_nothing`、`test_restart_after_cancel_keeps_unknown_not_available`、`test_capacity_refuses_new_and_old_identity_stays_consumed`、`test_duplicate_after_expiry_does_not_refresh_ttl`、`test_restart_sweeps_executing_to_unknown`、`test_same_key_conflict_rejected_and_kinds_isolated`、`test_corrupt_ledger_degrades_and_survives_restart`、`test_corrupt_ledger_keeps_degraded_after_repair`、`test_missing_manifest_with_existing_file_is_migrated`、`test_occupied_flag_not_replayable_after_restart`、`test_degraded_ledger_refuses_approval_without_action`、`test_unknown_account_not_registered` |
| 实测 | 全部通过; 双实例并发与"占用落盘期间取消"用例断言网络动作次数为 0。变异验证: 去掉占用前的锁内重读后并发用例变红 |
| 剩余限制 | 账本没有协议级失效依据时不做回收, 满载按显式降级处理 (拒绝新登记并提示容量), 这是有意的安全取舍 |

### B5 / P2: 扫描不完整必须阻止 ASR 引用删除/重命名

| 项 | 内容 |
| --- | --- |
| 复审反例 | 扫描内部吞掉错误, 外层 fail-closed 实际失效, 配置在存在引用时被删除 |
| 关闭条件 | 已存在文件读不出/解析失败、插件 schema 坏、SQLite 锁定/损坏、覆盖 JSON 非法都不得返回"无引用"; 仅契约上允许不存在的旧库缺表可作空集合; 通过时仍返回 `config_in_use`, 不完整返回独立错误, HTTP 503 与 CLI 非零 |
| 实现 | `config/asr_references.py` 新增 `AsrReferenceScanError(reason, origin)`, 覆盖读取/解析/数据库/覆盖 JSON/声明版本五类来源; 扫描与配置变更共用 `REFERENCE_SCAN_LOCK`; `session_overrides.ensure_override_tables` 只向上写 `user_version`; control/CLI 映射 503 `asr_reference_scan_failed` 与非零退出 |
| 反例测试 | `tests/unit/test_asr_config_references.py` 10 项 (插件声明 asr 字段时旧库分支 [NameError 修复]、插件元数据损坏、全局覆盖 JSON 非法、数据库锁定、声明版本与库不符、旧库缺表仍是空集合) + 503 路由 + CLI 退出码 |
| 实测 | 全部通过。变异验证: 去掉 `logger` import 后旧库分支用例变红 (该分支原本必然 NameError) |
| 剩余限制 | 扫描在配置变更锁内进行, 大库上的删除操作会有一次额外扫描成本; 未对 ASR 配置内容做语义校验 |

### B6 / P2: talk_value=0 仍可被最长等待触发

| 项 | 内容 |
| --- | --- |
| 复审反例 | `wake_talk_value=0` (关闭自动参与) 时 `wake_max_wait` 到期补偿仍然触发 |
| 关闭条件 | 零值 + max_wait 不触发; 正值到期仍触发; 显式阈值 + 零值遵守优先级且原因准确; @/手动/necessity 不回归; 真实管线与 dry_run 一致 |
| 实现 | `pipeline/wake_policy.py` 新增 `resolve_message_threshold` 与 `MessageThreshold` (`explicit` > `talk_value` > 默认 3, `closed` 仅在有效来源是 `talk_value=0` 时成立); `wake_window.decide` 在冷却与到期判断之前先解析, 关闭时立即返回不触发且不消费窗口; `wake_dry_run` 共用同一解析并输出 `automatic.threshold` |
| 反例测试 | `tests/unit/test_wake_window.py` 7 项: `test_zero_talk_value_is_not_bypassed_by_max_wait`、`test_positive_talk_value_still_triggers_on_deadline`、`test_explicit_threshold_with_zero_talk_value_keeps_deadline`、`test_zero_talk_value_max_wait_does_not_wake_real_pipeline`、`test_zero_talk_value_keeps_mention_and_manual_wake`、`test_zero_talk_value_does_not_block_necessity_deadline` 等; `tests/unit/test_wake_dry_run.py::TestPolicySources` 4 项 |
| 实测 | 全部通过。变异验证: 把关闭判断移回到期判断之后, 三个零值用例同时变红 |
| 剩余限制 | 关闭状态只影响自动参与 (`frequency`), 不影响 @/唤醒词/手动唤醒与 `necessity`; 这是既定语义而不是缺口 |

### B7 / P2: 未验证的文件兼容回落仍上报成功

| 项 | 内容 |
| --- | --- |
| 复审反例 | 缺上传动作时的兼容回落只要普通消息动作返回 `message_id` 就标成功, 文件实际未必送达 |
| 关闭条件 | 缺上传动作 + 普通消息成功仍为 `unknown`; 管线、持久化、工具返回与 UI 均不显示已送达; 混合链顺序、未发后缀、确认前缀与同代次能力缓存可验证 |
| 实现 | `onebot/adapter.py` 的 `_send_file_fallback` 统一返回 `unknown` + `file_delivery_unconfirmed`, 保留传输层确认信息; 首段确认保留、后续段不发送; 平台明确拒绝该消息动作时才 `failed` |
| 反例测试 | `tests/unit/test_onebot_forward.py` / `test_onebot_adapter.py` 中的混合链用例: Plain/File/Plain 的确认前缀与未尝试后缀在回执与持久化两侧都不显示已送达; 探针 `scripts/probe_snowluma.py` 同步更新 |
| 实测 | 全部通过。对外行为变化: 文件兼容回落由 `success`/`fallback_unverified` 统一改判 `unknown`, `fallback_unverified` 不再出现 |
| 剩余限制 | 该兼容路径只有在具体实现版本验证过之后才可能升级确认语义; 当前任何版本都判未确认 |

### B8 / P2: 覆盖编辑器会在编辑中删除已有规则

| 项 | 内容 |
| --- | --- |
| 复审反例 | 清空已有群号准备重新输入时整行消失 (`AUDIT_ROWS_BEFORE 1` → `AUDIT_ROWS_AFTER_CLEAR 0`), 时段编辑成暂时无效值同理 |
| 关闭条件 | 清空再输入、重复群号、已有时段暂时无效、最后字段改继承、删除中间行、新增未完成行均不误删/串行; 服务端错误与修订冲突保留全部草稿; 只有无错误时才提交规范化值 |
| 实现 | `satrap-ui/src/utils/wakeOverrides.ts` 行带稳定 `rowId`, 转换函数返回 `{value, issues}`; `pages/Platforms/WakeOverrideEditor.tsx` 改为受控组件 (删除"父 value 变即 effect 覆盖 rows"); `pages/Platforms/index.tsx` 由表单持有草稿行并与初始快照一起参与脏比较, 保存与试算共用同一校验结果 |
| 反例测试 | `satrap-ui/src/utils/wakeOverrides.test.ts` 22 项 (空号/非法号/重复号保留并报错、裁剪、rowId 唯一且稳定、全继承行为合法非错误、起止相同报错); Playwright `e2e/platform-policy.mjs` 的"草稿行保留与校验"段复跑复审探针 |
| 实测 | e2e 断言: 清空已有群号后行数与同行其它显式值不变 (复跑 `AUDIT_ROWS_BEFORE 1` 场景, 行数仍为 1); 重复群号保留 2 行且保存被阻止 (写入数不增); 删除中间行后剩余行群号仍为 `20`/`32`、冷却值不串行; 时段开始时间置空后行与"显式关闭"值保留且保存被阻止; 修订冲突后含未完成行的草稿全部保留 |
| 剩余限制 | 校验规则与后端一致但不替代后端校验 (后端仍独立校验); 重名群号只保留首行, 需用户显式删除多余行 |

### B9 / P2: 浏览器前进/后退仍绕过脏保护

| 项 | 内容 |
| --- | --- |
| 复审反例 | 修改唤醒词后真实后退, 路径变为 `/sessions`, `confirmations=0`, `modalVisible=false`, 草稿无确认丢失 |
| 关闭条件 | 真实浏览器后退/前进、站内导航、Escape、遮罩、刷新/关闭都要验证; 取消离开保留草稿, 确认放弃只导航一次; 未完成行也触发保护; 保存成功后不误拦截; `/chat` 与管理页直达正常 |
| 实现 | 新增 `satrap-ui/src/router.tsx` 用 `createBrowserRouter` 装配等价路由 (URL、`/chat` 独立布局、AppLayout、懒加载、启动认证顺序不变); `main.tsx`/`App.tsx` 改由 `RouterProvider` 渲染; `hooks/useDirtyGuard.ts` 接入 `useBlocker` 并保留 `beforeunload` |
| 反例测试 | Playwright `e2e/platform-policy.mjs` 的"数据路由离开拦截"段: 后退取消两次仍拦截且草稿完整 (2 行未完成行也在)、URL 保持 `/platforms`; 确认放弃后 URL 变为 `/sessions` 且确认次数恰好 1 次; 非脏状态前进/后退不弹确认 (`unexpectedConfirm === 0`); `/chat` 直达渲染聊天页且无管理侧栏; `/platforms` 直达渲染管理布局 |
| 实测 | 通过 (复跑复审探针: 后退时出现确认, 取消后仍停留在编辑页且草稿保留) |
| 剩余限制 | 浏览器关闭/刷新仍由 `beforeunload` 原生确认承载 (浏览器不允许自定义文案); 未匹配路径保持空渲染 |

### B10 / P2: 配置与逐次诊断交付仍有缺口

| 项 | 内容 |
| --- | --- |
| 复审反例 | ① 平台表单没有 `input_text_limit`/`input_media_limit`/平台级 `wake_talk_value`; ② 新记录只采集拒绝阶段, 附件/转录失败不可查; ③ 普通自动回复的 partial/unknown 缺诊断闭环 |
| 关闭条件 | ① 新建/编辑/清空/0 值/别名平台/未知扩展字段往返正确, 热更新后下一事件用新值且已冻结事件保持快照; ② 未执行投影的拒绝、语音转录失败、附件部分失败、普通 partial/unknown、手动请求都能查到阶段; 查询无正文/凭据, 容量有界, 适配器过滤准确; ③ 普通事件与手动请求共用同一展示, 空/失败/慢/窄屏/键盘都有可用状态 |
| 实现 | 后端: `pipeline/wake_rejections.py` 扩为按请求关联的有界诊断 (每实例 256 请求 × 16 条), 在决策/限流/补全/模型/发送五个位置就地采集, 脱敏 (`reason_code`/附件类型/截断说明), 新增 `/api/platforms/wake/diagnostics` 列表与详情端点并保留旧拒绝接口; 配置: 三个平台级字段进入表单与 `hot_keys` 热更新, `resolve_wake_policy_sources` 与运行时共用合并实现, 试算返回 `sources`/`defaults`/`automatic.threshold`; 前端: `components/diagnostics/RequestDiagnosticsPanel.tsx` 在平台页与手动唤醒弹窗共用列表/详情, 中文阶段与原因、附件失败逐项、已确认/未确认/失败分段、平台筛选、详情展开、手动刷新与有界轮询, `unknown` 显示"不确定, 不自动重发" |
| 反例测试 | 后端 `tests/unit/test_request_diagnostics.py` 15 项 (未投影拒绝、限流、附件失败与发送分开、partial 与 unknown 分离、模型超时、容量与去重、适配器过滤、无正文、列表与详情路由、不可用标记、采集异常不影响管线、普通与手动共用阶段); `tests/unit/test_platform_config_runtime.py::test_input_budget_and_talk_value_are_hot_applied`; 配置往返 `tests/unit/test_platform_config*` 与 `satrap-ui/src/utils/adminMigration.test.ts`; 前端 e2e `platform-policy.mjs` 的"请求阶段诊断"段与 `manual-wake.mjs` 的阶段诊断/unknown/降级断言 |
| 实测 | 后端 15 项通过; 前端 e2e 覆盖: 普通事件 (决策/限流) 与手动请求同列表、阶段中文与 `unknown` 标语、附件失败翻译 (`语音 不支持 (asr_config_missing)` 等)、详情展开显示原因码与"已确认/未确认/失败"分段、平台筛选以 `adapter_id` 到达服务端、空列表/调度器不可用/查询失败各自状态与"实际重试"、终态停止轮询与进行中轮询、窄屏与键盘可操作; 手动区覆盖 `unknown` 不自动重发 (2.2 秒内请求数不增) 与存储降级显示 |
| 过程中发现并修复 | ① 前端 `getWakeStatus`/`listWakeRejections` 参数写成 `{params:{...}}`, 而客户端直接收参数, 平台筛选实际未生效 (已修, e2e 断言查询串); ② 手动状态面板的降级分支按 axios 错误对象取 `reason`, 而 `apiClient` 已包装成 `ApiError`, 该分支恒不可达 (已改为按 HTTP 状态判断); ③ 后端 `wake_rejections.stats()` 的 `records` 计数键与列表端点返回的 `records` 列表同名, 导致列表返回整数 (已改名 `requests_total`/`records_total`) |
| 剩余限制 | 诊断是进程内环形结构, 重启后历史阶段记录丢失, 手动请求的幂等与发送证据仍以持久账本为准 (界面明确说明); 平台筛选选项来自运行中适配器与已配置平台, 停用且未配置的实例不出现在列表; `wake_talk_value` 仍只支持平台级与群/时段覆盖, 不进入输入预算类字段 |

## 3. 全量门禁复核 (2026-09-24)

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 后端单测 | `python -m pytest tests/unit -q` | 2071 passed / 19 skipped (跳过项为集成开关、缺 reportlab、Windows 符号链接权限) |
| 类型检查 | `python -m pyright -p .pyrightcfg` | 0 errors / 1532 warnings (与开工基线一致, 未新增) |
| 前端类型 | `node ./node_modules/typescript/bin/tsc --noEmit` | 0 error |
| 前端单测 | `node ./node_modules/vitest/vitest.mjs run` | 18 文件 / 94 passed |
| 前端 lint | `node ./node_modules/eslint/bin/eslint.js . --ext ts,tsx --report-unused-disable-directives --max-warnings 0` | 0 warning |
| 前端构建 | `node ./node_modules/vite/bin/vite.js build` | 成功 |
| 浏览器回归 | `node e2e/platform-policy.mjs` / `e2e/manual-wake.mjs` / `e2e/chat-reconnect.mjs` | 三个脚本 PASS |
| 空白检查 | `git diff --check` | 通过 |

基线对照: 方案记录的开工基线为后端 1999 passed / 7 skipped、pyright 0 errors / 1532 warnings、前端 90 passed。本轮新增用例后为后端 2071 passed / 19 skipped、前端 94 passed; skipped 的增加来自既有集成开关用例的收集方式, 不是新增跳过。

## 4. 复审反例的复跑结论

| 复审反例 | 复跑方式 | 现状 |
| --- | --- | --- |
| B1 异群 forward_id 可读 | `test_onebot_admin.py` 反例组 | 拒绝且 `get_forward_msg` 调用数 0 |
| B2 隔离后重启恢复可写 | `test_manual_wake_store.py` 降级组 | 连续重启仍降级, 原 request_id 不重新 accepted |
| B3 取消被判明确失败 | `test_cancel_after_first_segment_is_unknown_with_confirmed_prefix` | 状态 `unknown` 且保留已确认前缀 |
| B4 flag 淘汰后重放 | `test_cache_eviction_keeps_ledger_identity` 等 | 账本身份不随缓存淘汰消失, 无第二次动作 |
| B5 扫描错误被吞 | `test_asr_config_references.py` 反例组 | 扫描不完整返回独立错误, 503 / 非零退出 |
| B6 零值被到期补偿触发 | `test_zero_talk_value_is_not_bypassed_by_max_wait` | 不触发, 且窗口不被消费 |
| B7 未验证文件回落报成功 | 混合链用例 + 探针 | `unknown`/`file_delivery_unconfirmed`, 界面不显示已送达 |
| B8 编辑中删行 | `e2e/platform-policy.mjs` 草稿行段 | 行保留并报错, 保存被阻止 |
| B9 后退绕过脏保护 | `e2e/platform-policy.mjs` 离开拦截段 | 后退触发确认, 取消留在原页且草稿完整 |
| B10 配置与诊断缺口 | 后端 15 项 + 两个 e2e 脚本 | 普通与手动请求的阶段结果在界面可见, 平台字段可配置且热更新 |

## 5. 仍开放的限制与本轮未覆盖的验收

1. 聊天页 `RunRecovery` 在聊天接口返回缺少 `runs` 字段的响应时整页渲染失败 (数据路由下显示 "Unexpected Application Error!")。该组件属于执行记录查询, 不在 B1-B10 范围内, 本轮只把它暴露出的测试夹具缺口补全 (`e2e/chat-reconnect.mjs` 与 `e2e/platform-policy.mjs` 补 `/api/chat/runs` 受控响应), 未改动组件代码。真实后端始终返回 `runs`, 因此该限制只在响应畸形或旧后端/代理返回非约定载荷时出现。
2. 诊断记录为进程内环形结构, 重启后丢失; 这是有意的容量与无阻塞取舍, 与持久账本的职责分离已在文档与界面标注。
3. 未在本轮重跑真实 ASR/LLM 或 SnowLuma 探针; 音频转写与模型入参未因本轮改动而变化, 相应历史覆盖沿用原范围。
4. 外部验收 (按方案): SnowLuma 侧实际网络模块 + 模拟 QQ 动作的来源消息→转发读取、文件缺动作回落、断连/取消; 若后续改动了音频补全或最终模型入参, 需补一次真实短音频的定向验收。
5. 平台改动仍需重启后端的路径 (连接、会话绑定、容量、实例增删) 保持原语义; 逐事件字段已热更新, 但界面上的 `saved_revision`/`active_revision` 与 `待重启` 标记仍依赖后端健康响应。
