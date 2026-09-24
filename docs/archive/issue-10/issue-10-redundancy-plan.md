# Issue #10 冗余清理方案

日期: 2026-09-24

依据: 审计冗余方向核验 (2026-09-24, 五项全部属实): 持久化清单框架重复 / 诊断命名遗留 / 前后端策略校验镜像 / 手动唤醒弹窗双面板 / 字段契约实为四份以上。本文件已随四个批次实施完成归档; 实施证据见 [进度记录](../../platform/issue-10-progress.md)。

实施基线: `fix/issue10`, `b30c7c2`

状态: 已实施完毕并归档 (2026-09-25)。四个批次已全部实施并提交 (诊断命名归一 / 手动唤醒弹窗双面板去重 / 策略字段契约单一来源 / 持久化
DurabilityManifest 抽取), 各批次证据见 [进度记录](../../platform/issue-10-progress.md)。本方案只做冗余清理与命名归一,
不改变对外行为契约 (除明确列出的诊断端点参数扩展与前端取值口径修正), 不替代最终验收。

## 1. 批次顺序与共同约束

| 批次 | 内容 | 风险与定位 |
| --- | --- | --- |
| 1 | R2 诊断命名归一 (RequestDiagnosticLog) | 零行为变化, 先行降低后续批次的认知负担 |
| 2 | R4 手动唤醒弹窗双面板去重 | 低; 依赖批次 1 的命名, 含一处端点参数扩展 |
| 3 | R3+R5 策略字段契约单一来源 | 中; 独立, 前端对齐现有后端取值契约 |
| 4 | R1 持久化 DurabilityManifest 抽取 | 高; 独立, 放最后, 以既有存储测试全绿为行为不变证据 |

每批一个独立提交, 在 `fix/issue10` 推进。共同约束:

- 旧路由与旧方法保留兼容层, 不删除对外端点; 进程内死代码 (前端函数, 内联组件) 可以删除
- unknown 不自动重试, 不显示为"已送达"或"明确失败"; 降级只能经 recover() 显式解除
- 持久化写入的失败顺序契约不变: 先持久降级标记, 后隔离文件; 标记写失败则保留原文件
- pyright 清零终态不用 ignore 注释, 不用 assert 收窄; 测试文件同在门禁内
- 门禁基线: 全量 `pytest tests/unit` 绿, pyright 0 errors 且 warnings 不超过 1532, 前端 tsc/eslint 0, vitest 绿, 受影响的 Playwright 脚本 PASS
- 全程 UTF-8; 不落真实凭据, 不登录 QQ

## 2. 批次 1: R2 诊断命名归一

现状证据: `wake_rejections.py:25` `DIAGNOSTIC_STAGES` 已是五阶段 (wake_decision/rate_limit/projection/model/send), `:28` `REJECTION_STAGES` 为前两者子集; `BackendManager.py:592` 旧接口 `wake_rejections()` 仅是新日志的 `stages=REJECTION_STAGES` 筛选; 生产端方法已叫 `_record_diagnostic` (scheduler.py:108), 新端点已叫 `/api/platforms/wake/diagnostics`, 测试文件已叫 `test_request_diagnostics.py` — 只剩类型名未跟上。

改动:

1. `satrap/core/pipeline/wake_rejections.py` 改名 `request_diagnostics.py`; 类 `WakeRejectionLog`→`RequestDiagnosticLog`, `WakeRejection`→`RequestDiagnostic`; 模块 docstring 保留 (已自述"按请求关联的有界诊断")
2. `REJECTION_STAGES` 常量名保留 — 它如实命名"旧拒绝查询的阶段子集"这一兼容语义; `DIAGNOSTIC_STAGES` 不变
3. `scheduler.py`: 属性 `wake_rejections`→`request_diagnostics` (:93 及全部引用); `_record_rejection` 方法名保留 (它确实只在拒绝点调用)
4. `BackendManager.wake_rejections()` 保留为兼容端点入口, 内部改用新属性名; `request_diagnostics()`/`request_diagnostic_detail()` 保持现名
5. `http_api.py` 两条路由均不动
6. 测试文件名保留: `test_request_diagnostics.py` 测新接口, `test_wake_rejections.py` 测兼容路由, 仅更新 import
7. 前端 `WakeRejectionRecord` 类型留到批次 2 随死代码一并删除

不做: 不合并两个测试文件; 不为旧类名留别名 (进程内模块, 全库引用一次改清)。

验收: 全量 pytest 绿; pyright 0 errors; `rg -n "WakeRejection" satrap/ tests/` 无旧类型的生产或测试引用。

## 3. 批次 2: R4 手动唤醒弹窗双面板去重

现状证据: `ManualWakeModal.tsx:88-109` 内联 `WakeRejectionsPanel` (调 `/wake/rejections`, 跨适配器 10 条) 与 `:181-192` `RequestDiagnosticsPanel` 并挂于同一弹窗, 两次请求同一内存日志; 平台页 (`Platforms/index.tsx:551`) 只有诊断面板。语义等价已核实: `wake_decision`/`rate_limit` 阶段记录只在拒绝点产生 (scheduler.py:421-437 未唤醒分支, :460-463 限流分支, status 恒为 dropped), 且被决策拒绝的请求只有 wake_decision 一条, 被限流的只有 rate_limit 一条 (限流在唤醒之后), 每个被拒请求恰含一条拒绝阶段记录 — 按请求分组的诊断视图不丢信息。

改动:

1. 诊断端点 `GET /api/platforms/wake/diagnostics` 的 `stage` 参数接受逗号分隔多值, 逐值校验 ∈ `DIAGNOSTIC_STAGES` (现 http_api.py:368 单值校验); 空字符串或缺失表示不过滤, 非空参数按逗号拆分、去除首尾空白、去重, 空项或未知值返回 400。多阶段按 OR 匹配请求, 与 adapter_id/request_id 按 AND 组合; 匹配后仍返回请求全部阶段, 不裁掉详情。单值请求保持兼容, `list_requests` 与 `BackendManager.request_diagnostics` 相应透传
2. `RequestDiagnosticsPanel` 增加"仅看拒绝"预设开关 (`stage=wake_decision,rate_limit`), 用于近期请求浏览。平台页默认关闭, 弹窗未跟踪请求时可默认开启; 一旦提交受理并聚焦 request_id, 自动清除阶段过滤并展示全部阶段, 跟踪期间禁用拒绝筛选。提供"返回近期请求"入口清除聚焦后再使用拒绝筛选, 不能让正常请求因拒绝过滤而显示为空
3. `ManualWakeModal.tsx` 删除内联 `WakeRejectionsPanel` 组件与 `wake_rejections` 表单项
4. `satrap-ui/src/api/backend.ts` 删除 `listWakeRejections` 与 `WakeRejectionRecord` (前端客户端不是兼容面); 后端路由 `/api/platforms/wake/rejections` 与 `BackendManager.wake_rejections()` 按兼容接口保留
5. 测试: 在相同适配器、相同 limit、有效 request_id 且每请求一条拒绝记录的生产形态下, "仅看拒绝"请求集合与旧 `/wake/rejections` 的 request_id 集合一致; 单值、多值 OR、去重、空参数、空项和非法值分别覆盖。E2E 覆盖从拒绝预设提交 accepted/already_pending 后默认可见 projection/model/send, 特别是 partial/unknown 明细; 返回近期请求后可重新筛选拒绝, 请求聚焦和平台切换不残留旧过滤。更新旧 testid, 确认弹窗不再请求旧端点

不做: 不改变诊断环形容量与淘汰语义; 不为旧面板留 UI 开关。

验收: vitest 绿; tsc/eslint 0; manual-wake 与 platform-policy 两个 Playwright 脚本 PASS。

## 4. 批次 3: R3+R5 策略字段契约单一来源

现状证据 — 同一策略字段的事实目前分散在六处:

1. `wake_overrides.py:11-16` `GROUP_KEYS`/`AUTOMATIC_KEYS` (覆盖范围)
2. `platform_policy.py:12` `POLICY_DEFAULTS` + `:60-120` `validate_wake_policy` (默认值与范围)
3. `BackendManager.py:757` `hot_keys` (热更新集合, 27 键, 含 asr_model/media_* 等纯平台级键)
4. `wakeOverrides.ts:21-49` `OVERRIDE_FIELDS` (前端编辑器字段, min/max/offValue/groupOnly)
5. `adminMigration.ts:34-44` `POLICY_RANGES` (平台表单数值校验)
6. `WakeDryRunPanel.tsx:172` 试算重点展示键 (6 键硬编码); 另 `Platforms/index.tsx:355-380` 表单 label/placeholder 内嵌范围与默认值文本

已发生的漂移: `wake_cooldown` 上限三处不一 (后端有限非负 / OVERRIDE_FIELDS 86400 / POLICY_RANGES MAX_SAFE_INTEGER); `wake_max_wait` 后端 `0≤x<120`, 前端两处分别写 119.999 与 119。

改动:

1. 在 `platform_policy.py` 定义声明式字段契约表 `POLICY_FIELD_CONTRACT`: 每字段 `{kind, scope, hot_reload, display_in_preview, off_value, min, max, max_exclusive, integer, enum, default, nullable}`; `max` 为包含式上界, `max_exclusive` 为排他上界, 同字段不同时声明两者, 没有上界则省略。`scope` 三值: `platform` (仅平台级) / `group` (平台和群覆盖) / `time` (平台, 时段与群覆盖)。表覆盖策略校验涉及字段、GROUP_KEYS/AUTOMATIC_KEYS、POLICY_DEFAULTS、前端编辑字段和 hot_keys 的并集, 不局限于 27 个热更新键; `reply_with_quote`、`quote_lookup` 等不在旧 hot_keys 内的字段也入表, hot_reload 保持 False。未知扩展字段继续按原规则透传, 不因表驱动改为平台级全字段白名单
2. `validate_wake_policy` 的标量检查改为表驱动, 数值检查保持有限性及拒绝 bool, 布尔字段严格检查 bool。缺失、显式 null 和默认值分别处理: 无 default 表示没有默认值, nullable 明确是否接受 null (例如 wake_talk_value 当前接受 None); 校验和前端编辑不得向覆盖对象自动注入默认值, 缺失仍表示继承, 0/False/[] 仍是显式值。`POLICY_DEFAULTS` 从表派生。表无法表达的保留手写: notice_types 正则、asr_model 长度、media_trusted_hosts 列表约束、wake_words/wake_aliases 归一化、覆盖结构与条数上限 (512/32)。契约迁移不得漏掉这些检查
3. `GROUP_KEYS`/`AUTOMATIC_KEYS` 改为从 `scope` 推导; `BackendManager` 的 `hot_keys` 改为从 `hot_reload` 推导, 删除手写字面量
4. 口径按现有后端契约统一:
   - `wake_cooldown`: 保持有限非负, 不新增一天上限; 删除前端 86400/MAX_SAFE_INTEGER 的额外业务上限。后端在保存及适配器构造时均执行校验, 收紧范围会影响已有配置启动, 不属于本次冗余清理
   - `wake_max_wait`: `0 ≤ x < 120` 排他上界, 前端 119/119.999 两处废止
5. 脚本 `scripts/sync_wake_policy_contract.py` 由契约表生成 `satrap-ui/src/generated/wake-policy-contract.json` 并入库; pytest 断言"表→JSON 序列化与库内文件逐字节一致", 漂移即红 (控制端运行时不依赖该文件, 构建期亦不要求后端在线)
6. 前端 `OVERRIDE_FIELDS`/`POLICY_RANGES`/`WakeDryRunPanel` 展示键/平台表单数值字段的约束改为从 JSON 构建; label 中的范围文本按含上界、不含上界、无上界分别生成, placeholder 的语义提示保留手写。HTML max 不能表达排他上界, `wake_max_wait` 必须由校验器检查 `<120`, 不以 119 或任意 epsilon 代替; integer 字段同时提供整数步长和程序校验
7. 新增前端表驱动策略字段校验, 实际接入平台保存/试算及 `fromGroupRows`/`fromTimeRows` 对嵌套 override/settings 的检查; 这两个转换函数现状仅检查群号/时间等结构, 不能只更新控件 min/max。共享 `tests/fixtures/wake_policy_cases.json` 使用规范 JSON 值, 按平台/群/时段上下文列出合法、非法、边界及期望结论; pytest 跑后端真实校验入口, vitest 跑严格字段校验与实际行转换入口。覆盖 119.5、119.9999、120、冷却 86401、整数小数、bool 作为数字、枚举、缺失/null/显式关闭和禁止覆盖字段; NaN/Infinity 不能写入 JSON, 两种语言单独补测。表单数字字符串/空白的归一化独立测试, 不把 Number(true) 等宽松转换作为严格 JSON 校验依据; 非法字段保留草稿并阻止保存与试算
8. `adminMigration.ts` 的 `talkValuePriorityHint`: 静态提示文案保留, 数值结论以试算响应为准 (`threshold.hint`/`threshold.overridden`, wake_dry_run.py 已实现), 前端不再自行推导阈值优先级
9. 消除导入循环: `platform_policy.py` 移除对 wake_overrides 的顶层导入, 在 `validate_wake_policy` 内导入覆盖校验; `wake_overrides.py` 可从已无反向顶层依赖的 platform_policy 导入契约并派生字段集合。两模块分别先导入的独立进程烟测均须成功, 同步脚本不得启动后端服务

不做: 前端即时校验与后端权威校验两侧都保留, 不合并; `_merge` 合并实现与 `resolve_wake_policy_sources` 来源追踪不动 (运行时与试算已共用同一实现, 方向正确); 不让前端复制后端的归一化逻辑。

验收: 契约同步测试、独立导入烟测与共享样例两侧绿; 既有 validate 相关测试全绿; pyright 0 errors; tsc/eslint 0; platform-policy e2e 验证平台/群/时段合法边界可保存及试算, 非法字段被阻止且草稿保留。迁移测试冻结原 hot_keys、GROUP_KEYS/AUTOMATIC_KEYS 和 POLICY_DEFAULTS, 与派生结果对比; 存量大于一天的冷却配置继续通过保存与适配器构造, 缺失覆盖字段不会因迁移写入默认值。

## 5. 批次 4: R1 持久化 DurabilityManifest 抽取

现状证据: `ManualWakeStore` (manual_wake_store.py, 1177 行) 与 `RequestApprovalLedger` (request_registry.py:119-604) 在 `persist.py` 原子写/隔离与 `FileLock` 之上, 各有八组近乎逐行平行的方法: `_read_manifest`+清单校验, `_mark_degraded`, `_degrade`, `_quarantine_names`, `_startup_locked`, `_initialize_fresh`, `_adopt_existing`, `recover`, 合计约 500 行。全库确认该模式仅此两个用户 (maintenance.py 与 plugin_compatibility.py 的 "manifest" 是会话归档清单/插件元数据, 另一概念)。

边界 (按 2026-09-24 裁定收紧): 通用组件只做组合式小件, 不做基类, 不拥有启动决策树与恢复骨架 — 两侧的读取模型 (内存权威 vs 每次变更锁内重读) 与恢复校验 (严格查重 vs 结构校验) 本质不同, 抽出去会变成"策略回调+状态枚举"的隐性基类。

新建 `satrap/core/storage/durability.py`, `DurabilityManifest` 仅负责:

- 清单读写与基础结构/版本校验入口 (`expected_files` 的必需键与允许状态由业务方声明式传入; 保持原额外键的接受与归一化语义)
- "先持久降级标记, 后隔离文件"的不可变顺序原语 (标记写失败则保留原文件不隔离)
- 隔离文件名生成 (persist.py 已有 `quarantine_file`, 不重复造) 与 `corrupt-*` 扫描
- 事务上下文由业务方提供 (FileLock 持有方式不动: ManualWakeStore 双锁, 账本可选锁)

留在业务侧: 启动决策树 (`_startup_locked`/`_initialize_fresh`/`_adopt_existing`/`_load_with_manifest`), `recover()`, 记录解析与校验, 清单业务交叉校验与归一化, 容量/保留期/TTL, 归档轮转, 读取模型。MWS 要求未降级时 main 必须 present, 其 degraded.reason 必须非空且有时间归一化; 账本当前 reason 只检查字符串类型。抽取时保留各自行为, 不以统一校验顺带收紧或放松历史格式。

改动:

1. `durability.py` 新组件 + 自身测试: 版本非法拒读, 降级标记写失败不隔离的反例, 隔离扫描, 清单损坏语义
2. `ManualWakeStore` 迁移: 清单读写、降级标记/隔离与隔离扫描改用组件; `_validate_manifest` 保留业务交叉校验及归一化薄层, 基础结构委托组件。expected_files 状态机 (main: present/missing, archive: absent/present/missing) 作为声明传入
3. `RequestApprovalLedger` 同法迁移 (entries: 恒 present), 保留账本特有清单校验和可选内存模式; 组件不得新增缓存权威、副作用重试或自动恢复

估计净消除 120–180 行, 不追求把启动树与 recover 纳入后的 250–300 行。

验收: `test_manual_wake_store.py` 与审批账本既有测试 (含降级/隔离/恢复反例) 全绿; 新组件测试覆盖上述反例。迁移前固定两类旧清单样本, 对比迁移后接受/拒绝、归一化结果和降级结论; 包含合法、版本非法、缺键、额外键、空原因、main missing 但未降级等组合。补标记写失败时内存仍降级且原文件不隔离、隔离失败后重启仍降级、恢复失败不解除降级、无持久路径的内存账本不创建文件等断言; 锁顺序和锁内重读并发反例仍须通过。pyright 0 errors。

## 6. 修订后的实施约定

1. `wake_cooldown` 保留有限非负契约; 本轮不新增上限, 不引入存量配置迁移
2. 旧路由 `/api/platforms/wake/rejections` 保留兼容, 在平台文档标注为遗留查询接口, 推荐新诊断接口; 本轮不安排移除日期
3. 契约 JSON 采用"入库 + 同步测试", 固定 UTF-8、LF、字段排序与末尾换行, 保证跨平台同步检查稳定; 前端构建不要求后端在线
4. 持久化抽取以旧格式、状态语义及故障顺序不变为准, 净减行数仅为估算, 不作为验收目标
