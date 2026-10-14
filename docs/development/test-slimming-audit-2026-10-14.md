# 测试精简审计 2026-10-14

只审未改。范围: `tests/` 全目录 (137 个 py 文件 / ~64k 行 / 4019 个收集用例)。方法: 五个并行审计代理按域通读测试清单, 可疑处回 `satrap/` 源码核实是否同一代码路径。

## 结论概览

- 可合并/删除用例约 **390-500 个**(占 4019 的 10-12%), `tests/unit` 行数可压缩约 **7.4k-11.5k 行**(57.5k 的 13-20%)。
- `tests/manual/` 14 个脚本中 12 个可移除 (~980 行), `tests/benchmark/results/` 27 个 JSON 可缩到 7-9 个, 6 个历史 benchmark 脚本可归档。
- **发现一个真实缺陷**: `tests/integration/` 下 `test_chat_recovery_process.py` 与 `test_plugin_application_processes.py` 缺少 `pytestmark = integration`, conftest 只跳带 integration 关键字的用例, 因此**默认 `pytest` 会真正执行这两个起真实子进程/占端口/杀进程的重测试**, 是默认套件里最重最易抖的两项, 与目录语义不一致。

## 六大冗余模式 (按出现频率)

1. **sync/async 镜像对** — 本项目 sync/async 是分开的源码实现 (如 `simple_session/sync.py` vs `async_.py`), 成对测试各自覆盖不同文件不能删, 但可用项目已有的 `@parametrize("asynchronous")` 范式 (见 test_simple_session.py:2586) 合并为一份, 省一半行数不丢覆盖。重灾区: test_simple_session (~10 对)、test_session_command_flows、context_storage/context_editing、resource_protocol (6 对)、visual_input、coding_security、agent_sub_agent (6 对解析测试共用 `_parse_tasks`)。
2. **过度参数化矩阵坍缩** — test_plugin_migration_flows 72 case 坍缩为一条布尔式可缩到 ~12; test_workflow_execution_contract `iterations(0,1,2)` 中 0 与 1 行为相同 (24→16); test_coding_security_flows 24 case 同构可压 6-8; control_api/control_dispatch 三大 404 矩阵各裁半; dedup 容量测试循环 4100 次验上界、告警去重循环 200 次验一次即够。
3. **跨文件重复** (详见下节) — 多层/多入口对同一实现的重复覆盖。
4. **琐碎断言** — getter/常量/默认值/结构存在性: test_session_execution_flows 的 event 访问器段 ~17 个测试 (~250 行)、配置 CRUD 族、meta/capability 形状断言、wake_policy 的 frozen-set 常量回读 (TestFrozenSets 6 条 + TestPolicyDefaultSingleSource 6 条)。
5. **mock 镜像 / 钉死私有结构与文案** — 断言 `cache._pending`/`worker._slots`/timings_ms 键名/私有方法输出/精确日志文案与错误字符串, 属实现细节非契约。
6. **成族复制粘贴** — 同族 test_x_1/2/3 或相邻用例只差一个参数, 可收敛为参数表。

## 跨文件重复清单 (最值得先动)

| 重复内容 | 涉及文件 | 建议 |
|---|---|---|
| request_registry 注册/占用/过期/分页 | test_request_approval_flows ↔ test_request_identity_flows | 两个 registry 测试文件合并为一个 |
| 检查点回滚/fork 两层语义 | test_checkpoint_flows ↔ test_recovery_flows | 各留一层 (sync+async 共 4 条同义) |
| talk_value=0 / 显式阈值优先 (13+ 条) | test_wake_pipeline_flows ↔ test_wake_policy_flows | 同一 `wake_policy.resolve_message_threshold` |
| ManualWakeStore 降级标记写失败 | test_storage_flows L800 ↔ test_wake_recovery_flows L531 | 近乎逐行重复, 删一 |
| resolve_context_policy 校验 | test_context_policy_flows ↔ test_configuration_models | 同源, 留一处 |
| evaluate_plugin_permissions 权限授予 | plugin_configuration ↔ administrator_policy ↔ administrator_management | 三层重复同一判定路径 |
| 自服务删除/审批路径 | account_request ↔ administrator_policy ↔ group_administration | 昵称撤销矩阵多处重叠 |
| committed_session_type 逐字复制 | test_onebot_adapter ↔ test_platform_misskey_flows | 参数化合并 |
| OutboundTurns 满队列+close | test_platform_reply_flows ↔ test_platform_notice_flows | 各写一遍, 合一 |
| BackendManager.health() | platform_notice ↔ platform_lifecycle | 放错文件且重叠 |
| ActionFailed 归一矩阵 | onebot_admin ↔ platform_notice | 重复, 保留审计日志增量即可 |
| binding gate 禁用/无效绑定拒绝 | routing_flows ↔ pipeline_scheduler ↔ wake_pipeline | 同一契约三处各测一遍, 明确归属留一处 |
| shell 逐次审批 | coding_flows L794 ↔ coding_security_flows | 边界被越过, 二选一 |
| tools_agent 只留系统消息契约 | workflow_full_agent ↔ llm_protocol_flows | 成族重复 |
| Chat 插件应用"来源变化→拒绝" | display_service L918-1336 ↔ platform_application_flows | display 侧镜像 platform 侧 |
| 媒体白名单 TLS/plaintext 策略两层各测 | media_projection 内部 | 同策略两个层级各一遍 |
| project memory 作用域 | project_feature L161-194 ↔ memory_flows | 重复, 删/移 |
| Edictum CRUD | cli_configuration ↔ configuration_agents | 服务层 vs CLI 入口同路径 |

## 按域明细

### 会话/聊天域 (~338 用例, 可减 70-85, ~1.2-1.8k 行)

- **test_simple_session.py** (118 用例, 2625 行): ~10 对 sync/async 逐字镜像 (plugin_empty_string_rewrite、command_lifecycle、skill_lifecycle、aggregate_enable_disable 等); `_assert_sync_callbacks` 同一校验函数的三个入口测试 (2025/2092/2111); close/abort/timeout 语义多组重叠; 琐碎: call_delegates_to_run、default_help_command、handler_config_frozen。可减 25-30。
- **test_session_execution_flows.py**: `parametrize(range(20))` 跑同一竞态 20 次 (2-3 次够); event 访问器段 ~17 个 getter 断言可并 2-3 个。
- **test_session_management_flows.py**: 配置 CRUD 琐碎族 62-148 可并 2-3; sync/async provider 生命周期镜像对。
- **test_session_command_flows.py**: command_entry 分支族 55-148 共 13 个测试全走 `extract_command_candidate` 一个函数, 一张参数表可覆盖; ToolsManager sync/async 镜像对。
- **test_agent_sub_agent.py**: 6 对解析测试共用同一纯函数 `_parse_tasks`, 参数化合并。
- **test_user_manager.py**: auto_create=False 守卫在 4 个入口复述, 可并 1。
- **test_message_components.py**: 常量映射成员判断、dataclass 字段回读等 4-5 个琐碎。

### 平台适配器域 (~50-58 可减, ~1.0-1.6k 行)

- **test_platform_application_flows.py**: "phase replacement" 与 "source changed→保留旧句柄" 两大复制族 (L117-178、L678-1207 多组), 收敛为参数化可再压 300-500 行。
- **test_platform_config_runtime.py**: dangling-binding 矩阵坍缩 (907-926 是 854-905 之和); "逐事件热更新" 机制三条子集用例。
- **test_media_projection_flows.py**: `TestSelectMediaEquivalence` 用测试内重写的参照实现比对生产实现, 留一条; 失败标记族、白名单正反矩阵、音频 probe 魔数矩阵均可坍缩。
- **test_display_service.py**: think/preload 两对逐字复制; L918-1336 chat 应用族镜像 platform 侧; 注册表四用例琐碎。
- **test_onebot_adapter.py**: `TestMediaSourceRejection` 三例仅组件类型不同; config defaults、别名注册表琐碎。
- **test_minihttp.py**: connection/websocket 限流同构模板可参数化; query 解码两处。
- **test_platform_misskey_flows.py**: L451 `test_misskey_session_builds_one_isolated_workflow` 加载 `tests/manual/misskey_session.py` 示例而非 satrap 源码, 属样例验证。

### 插件/管理/请求域 (~90-120 可减, ~2.0-3.0k 行)

- **test_plugin_migration_flows.py**: 72-case 有效状态矩阵坍缩为一条布尔式 → ~12; 昵称族与 admin/group 文件重叠。
- **test_plugin_installation_flows.py**: `_load_module` 七连 (模块索引缓存) 保 3 删 4; `_move_no_replace` 直测私有。
- **test_plugin_configuration_flows.py**: 权限授予族与 administrator_policy 重复; step-cache 六连并 3; `_FACTORY_DECLARATIONS` 私有字典断言。
- **test_plugin_runtime_flows.py**: 三插件 × loop 状态 6-7 个 3 断言用例参数化到 2-3。
- **test_plugin_catalog_contracts.py**: 兼容约束枚举坍缩; 200 次循环验告警去重; contract 主体 (回滚/漂移行为) 价值高不动。
- **control_dispatch / control_api / control_ui**: 404 与方法边界矩阵互有重叠各裁半; control_ui 11 个静态/SPA 用例有内部重复; `callable(getattr(...))` 形状断言五连价值低。
- **test_administrator_management_flows.py**: yaml/json roundtrip 孪生; find_config_path/autodetect 琐碎族; strict_scope 与 policy 层重复。
- **test_request_approval_flows.py / test_request_identity_flows.py**: 见跨文件表, occurrence 身份 12 连 + suspicious 17 连偏大, 内部亦有相邻重复。

### 配置/存储/唤醒/上下文域 (~78-84 可减, ~1.7-2.6k 行)

- **test_storage_flows.py**: `DurabilityManifest`/`validate_manifest` 家族 ~32 条集中减半; LiteVectorDB/DataBase 共享 API 双写 (L73-198) 用已存在的 parametrize 范式合一; 与 wake_recovery 的降级标记重复。
- **test_wake_pipeline_flows.py**: talk_value=0/显式阈值语义 13 条反复 (同一 `resolve_message_threshold`)。
- **test_wake_policy_flows.py**: TestFrozenSets/TestPolicyDefaultSingleSource/TestGeneratedContract 共 ~14 条常量回读。
- **test_checkpoint_flows ↔ test_recovery_flows**: 检查点回滚/fork/disabled 契约两层各测一遍, 共 ~10 条重复。
- **test_context_policy_flows.py**: 派生值复算重复 + sync/async 镜像对 + 与 configuration_models 同源校验。
- **test_context_editing_api.py**: `t1==t2 or abs(t1-t2)<t1` 近乎恒真断言; system 拼接三入参可参数化。
- **test_group_configuration_flows.py**: apply 族四条反复验 pending→failed→retry; legacy 采纳三入参坍缩。

### 其余 unit (~100-150 可减, ~1.5-2.5k 行)

- **test_workflow_execution_contract.py**: 12 函数 → 88 用例, 笛卡尔积砍组合省 30-45 用例 (不删函数)。
- **test_workflow_full_agent.py**: `uses_shared_engine` 4 条恒真式断言并 1; 与 llm_protocol 成族重复。
- **test_llm_protocol_flows.py**: error_contract 30 用例可压缩。
- **test_coding_security_flows.py**: 24/12 同构参数减半; sync/async 孪生合一。
- **test_knowledge_flows.py**: 空向量过滤 5 条同一路径并 2; chunker 8 条并 3-4。
- **test_skills_flows.py**: scan/冲突/adapter 孪生族并 6-8。
- **test_resource_protocol_flows.py**: 6 对 sync/async 合一; embedding 响应解析 9 条并 3。
- **test_project_feature.py**: 数据层/service 层重复 + memory 测试与 memory_flows 重复。
- **test_release_flows.py**: 端口残留三连并 1; legacy 迁移 5 条若已冻结可降为冒烟。
- **test_performance_timings.py**: 澄清 — `perf_counter` 被 monkeypatch, 完全确定性, 非 flaky, 可留可删。
- **test_routing_flows.py ↔ test_pipeline_scheduler.py**: binding gate 与窗口语义跨文件重叠, 与 wake_pipeline 三处同源。

## 非 unit 目录

- **tests/manual/**: 保留 `agent_demo.py` (文档化入口) 与 `tui_demo.py` (10-05 活跃维护、无等价正式测试); 其余 12 个 (agent_test/wf_test/coding_agent_demo/session_test/misskey_session/thinking_test/llm_call_test/rerank_test/search_test/sandbox_test/ctmger_text/log_test) 均为早期调试残留、硬编码空 key 或本机路径、功能已被正式测试覆盖, 建议移除 ~980 行。
- **tests/benchmark/**: 在用 — backend、platform_ingress、plugin_hotpath+compare_results、plugin_reads (10-11 活跃)。checkpoint/plugin_load/vector_db/audio_attachments/recovery/recovery_list 共 6 个为一次性历史探针, 建议归档或删。results/ 27 → 7-9 个 JSON (留 baseline/after/最新一轮)。
- **tests/integration/**: 三个 requires_api 文件默认 skip 合理; 两个进程级文件缺 integration 标记是缺陷 (见上)。
- **tests/fixtures/plugin_read_scale_probe.py**: 无测试代码引用, 仅 docs 审计手工复测命令用, 可移 manual/ 或删。
- **tests/support/**: 干净, 无测试混入。tests/agent_demo.py 根级包装保留 (文档化入口)。

## 绝对不能动

安全与正确性契约, 只可合并结构/裁参数, 不得删断言本身:

- SSRF/出站安全 (safe_async_get/_PinnedResolver/redirect 逐跳/字节上限)、媒体白名单与本地路径越权、NUL/UNC、ZIP 链接拒绝、路径穿越;
- 审批账本反重放、occupy/settle/recover、单赢家原子性、撤销后重检、strict_scope;
- DurabilityManifest "标记先于隔离、写失败保留原文件" 顺序与事务边界、fail-closed、不静默丢数据;
- 唤醒去重/回声/召回隔离、出站归档确认、冻结事件保留旧快照的否定断言;
- call_context 身份/取消/租约隔离、operator 权限门、handler error_policy/abort/超时并发;
- 检查点/恢复入口只接受完整记录集、发送结局裁定不得谎报 sent;
- 令牌/Origin/Bearer/Cookie、日志与凭据脱敏。

## 建议执行顺序 (若裁定动手)

1. 先修 integration 两文件的标记缺陷 (独立小改动);
2. 删 manual/ 12 个残留脚本 + 归档 benchmark 历史 (零风险);
3. 跨文件重复对逐个合并 (registry 两文件、checkpoint↔recovery 等), 每批跑全量门禁;
4. sync/async 参数化合并 (量大但机械, 用现有 asynchronous parametrize 范式);
5. 参数矩阵坍缩与琐碎断言合并 (逐文件做);
6. 每批后跑 `pytest tests/unit` + pyright 基线复核。

## 实施结果 (2026-10-14, 同日完成)

已按上述顺序全量实施, 五个并行执行代理按域分文件修改, 未动 satrap/ 源码。

- **收集用例 4018 → 3551 (-467, -11.6%)**, 通过 3523, 跳过 26, 失败 2 (仅本机缺 PowerShell 的既有环境失败 `test_redirected_management_directory_cannot_touch_outside_files[.runtime/.locks]`, 与本次无关)
- **pyright -p .pyrightcfg**: 334 → 311 error (-23, 零新增, 合并引入的 20 个 union narrowing/kwargs/Optional error 已逐点修复: cast 收窄/annotated kwargs/分支内化断言), warning 8497 → 8250
- **pyflakes**: 仅剩存量提示 (restore_manager usefixtures 字符串引用、cp2/batch_id 未用局部变量等), 非本次引入
- **行数**: tests/ 净减约 15.5k 行 (工作区 diff +3237/-6394, 另含 37 个删除文件共 12369 行, 主要为 benchmark results JSON 与 manual 脚本)
- **integration 标记缺陷已修**: test_chat_recovery_process / test_plugin_application_processes 补 `pytestmark = integration`, 默认套件不再跑真实子进程验收
- **manual/**: 删 12 个早期残留脚本, 保留 agent_demo/tui_demo; plugin_read_scale_probe 从 fixtures 移入 manual/ (docs 引用已同步)
- **benchmark/**: 删 5 个一次性探针 (plugin_load/vector_db/audio_attachments/recovery/recovery_list); benchmark_checkpoint.py 因 docs/core/checkpoint.md 现行引用而保留; results/ 27 → 8 个 JSON (backend 3 + platform before/after + hotpath baseline/after-batch7/after-overrides)
- **unit 主要动作**: sync/async 镜像对参数化合并 (simple_session/session_command/context_editing/resource_protocol/visual_input/agent_sub_agent/coding_security 等); 参数矩阵坍缩 (plugin_migration 72→12+4、workflow_contract 88→64、control 404 矩阵各裁半、dedup 4100 次循环改直接断言上界、告警去重 200 次改 1 次); 琐碎断言清理 (event 访问器族并契约测试/常量回读/getter/私有结构钉死); 跨文件重复按归属收敛 (checkpoint↔recovery、storage↔wake_recovery 降级标记、context_policy↔models 校验、permission 三层、binding gate 归属 scheduler)
- **落地偏差(如实记录)**: agent_sub_agent 新增 isolated_spawn_path fixture 根治基线中 mcp.py 遮蔽 spawn 的 2 条既有失败; misskey committed_session_type 保留 onebot 侧; display Chat 应用族多为 display 特有断言保守保留; group_admin v2 与 account_request v7 迁移断言不等价两边都留; 部分"留一处"条目实际留在与建议相反的一侧
- **保留未动**: 所有 fail-closed/反重放/撤销重检/原子性/脱敏/SSRF 与白名单契约断言; reminder 两文件基本未动; performance_timings 确认为确定性测试未动
