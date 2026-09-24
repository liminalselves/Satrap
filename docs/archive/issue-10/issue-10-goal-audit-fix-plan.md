# Issue #10 目标审计修复方案 (2026-09-23)

依据: [issue-10-goal-audit-2026-09-23.md](issue-10-goal-audit-2026-09-23.md) (审计)、[issue-10-goal-audit-review-2026-09-23.md](issue-10-goal-audit-review-2026-09-23.md) (复审) 与 [issue-10-goal-audit-fix-plan-review-2026-09-23.md](issue-10-goal-audit-fix-plan-review-2026-09-23.md) (方案复审)。用户裁定: File 出站分流与 talk_value 映射**确认缺失, 应补实现**; 历史验收记录有效性与发行包核验两项**搁置** (出处见文末"范围与交付口径"); 本文覆盖其余全部 10 项 (A1-A8 + File 出站 + talk_value 映射)。

修订记录: 2026-09-23 经方案复审 (R1-R8) 修订, 八条意见全部采纳并已逐条核验其代码引用属实 (含 `BackendManager.wake_platform:502-506` 无 prompt 唤醒把整份 snapshot 拼入 `event.message_str`、`wake_timers.py:55-66` 定时轻量事件保存正文副本、频率/必要性判断实际位于 `WakeWindow.decide` 而非 `evaluate_wake`)。各节标注对应复审条目。

约束沿以往批次: 全程在 `fix/issue10` 分支; 每批次一个提交; 门禁为全量 pytest 绿、pyright 全库 (`npx --yes pyright -p .pyrightcfg --outputjson`) 0 error 且 warning 不超过批次开始时记录的基线 (当前参考值 1532, 见文末口径); 前端 tsc/vitest 绿; 涉及入站/出站热路径的批次复跑 benchmark; 不使用 `type: ignore` 与 assert 收窄; 凭据仅运行时读取, 不执行真实 QQ 管理动作。

## 批次划分

| 批次 | 内容 | 理由 |
| --- | --- | --- |
| 一 | A1 群归属核验、A2 合并覆盖、A3 取消清理 | 安全面与数据丢失, 审计建议最先修 |
| 二 | A7 能力状态语义+补工具、File 出站分流、A4 统一输入预算、talk_value 映射 | File 分流依赖 A7 能力缓存; A4 预算约束 A2 窗口块 |
| 三 | A5 手动请求与发送状态持久化、A6 ASR 引用检查 | 状态存储与配置安全 |
| 四 | A8 前端交付 (编辑器/试算/脏保护/状态查询) | 依赖批次三查询端点与批次二试算逻辑抽取 |

---

## 批次一

### A1 撤回/审批核验实际操作对象的群归属 (含 R4 修订)

目标: `recall_message` 与 `handle_group_request` 从可信回源/接收登记核验实际群归属; 校验与占用在**首次网络等待之前原子完成**, 防并发与超时重放。

改动点:
- `satrap/core/platform/onebot/adapter.py` `_handle_request` (:425): 新增**请求登记**。先验证事件 `self_id` 等于本适配器 `bot_self_id` (可信账号身份先于登记), 再在 `_emit_notice` 订阅过滤与群白名单过滤**之前**登记: 群请求与好友请求**分表** (不复用同一命名空间), 键 = `(adapter_id, self_id, request_type, flag)`, 值 = `{group_id, sub_type, user_id, received_at, state}`; state ∈ `available/executing/completed/unknown`。有界 (每表 512, TTL 10 分钟, 惰性清扫)。**重复入站事件不得重置已占用状态**: 同键已存在时仅当状态为 available 才刷新时间戳, executing/completed/unknown 一律不改写。
- `satrap/core/platform/onebot/admin.py` `handle_group_request` (~:568): `_check_group` 后在**同一同步临界段内**完成"查登记 + 校验群号/sub_type + 置 executing" (纯内存操作, 不跨 await, 天然原子); flag 不存在/过期/非 available → 拒绝; 群号或 sub_type 不匹配 → 拒绝; 占位成功才发起 `set_group_add_request`。动作明确成功 → completed; 明确失败 → completed (OneBot flag 一次性, 失败亦不可重放); **超时/传输异常 → unknown, 永不自动回到 available, 不自动重试**。
- `satrap/core/platform/onebot/admin.py` `recall_message` (~:391): `get_msg` 回源校验 `message_type == "group"` 且群号匹配; 回源失败/不支持/不匹配 → 拒绝; **回源通过后、执行 `delete_msg` 前再次复查** `allows_group` 与写开关当前状态 (防回源等待期间配置被改); 复查失败 → 拒绝。
- 工具层不改, 防线在适配器层。

行为契约: 并发两个同 flag 请求至多一个进入 executing (占位无 await 窗口); 断线/超时后果为 unknown 而非可重放; 好友请求登记仅校验 flag 归属本账号且未占用。

测试 (`tests/unit/test_onebot_admin.py`):
- 撤回跨群反例 (get_msg 返回异群 → delete_msg 0 次); 回源失败 → 拒; 回源后权限失效 → 拒。
- 审批: 未知/异群/sub_type 不符/过期 flag → 拒; 正常 → 放行且 completed; 重复提交 → 拒。
- 同 flag 并发 (两协程交错) → 恰好一个成功; 动作超时 → 状态 unknown 且重试被拒; 错误账号 (self_id 不符) 的入站事件不登记; 重复入站事件不重置 executing; 断线重连后旧 flag 状态保持。
- 登记有界淘汰与 TTL; 登记不受 notice_types 订阅影响。

### A2 按事件类别合并窗口与补全内容 (含 R3 修订)

目标: 修复 batch 覆盖 projected 导致的内容丢失, 同时不引入重复或陈旧正文。复审核实"当前消息必在 batch 内"不成立, 必须区分事件类别:

| 事件类别 | 识别 | 输入组装规则 |
| --- | --- | --- |
| 真实当前消息 (正常入站) | 无 manual/ deadline 票据 | 保留完整 `projected.message`; 从实际 claim 结果中按稳定身份 (`request_id` + `message_id`) 剔除当前事件自身, 其余按到达顺序追加为窗口块 |
| 无 prompt 待处理手动唤醒 | `ManualWakeTicket.snapshot` 非空且事件为合成 (`BackendManager.wake_platform:502-515` 已把 snapshot 拼入 `message_str`) | **以实际成功 claim 的内容为唯一输入**, 不再追加, 也不保留合成事件上的 projected 拼接 |
| 定时补偿唤醒 | `wake_timers.tickets` 中 DeadlineTicket (`wake_timers.py:55-66` 保存的是过期风险正文副本) | 同上: 以到期时实际 claim 为准, 已消费/过期的旧正文不得重新带入 |
| 带 prompt / 带 message_id 手动唤醒 | snapshot 为空 | 现状不变 (prompt 或回源正文即输入) |

改动点 (`satrap/core/pipeline/scheduler.py` :145、:232、:255 区域):
- claim 之后分支: 窗口类合成事件 → `user_call.message` 由实际 claim 条目组装 (`[用户 x, 消息 y] ...`), claim 为空则按既有路径处理 (不构造空输入调用模型)。
- 真实当前消息 → projected + 窗口块追加; 显式 @ (`automatic=False`) 与自动触发同规则。
- 成员隔离继续由 `WakeWindow.key` 路由键保证, 不新增过滤。

测试:
- 复审要求的五类: 无 prompt 手动唤醒 (不重复整段窗口)、部分 claim (snapshot 中部分已消费, 输入只含实际 claim 项)、过期/淘汰、定时触发前已消费 (旧正文不出现)、缺失原消息 ID (手动合成事件不误判为真实消息去重失败)。
- 既有场景: frequency 显式 @ + Reply → 引用块与窗口块并存; necessity 混合附件 → 补全内容保留; 当前消息不在窗口块中重复。

### A3 任务与锁登记由子任务终态驱动 (含 R1 修订)

目标: 复审指出"调用方有界等待后 finally 仍摘除登记"只是把缺陷延后五秒。改为**登记清理由子任务实际完成驱动**, 调用方退出不再触碰登记。

改动点 (`satrap/core/platform/onebot/outbound.py`):
- 任务创建即 `task.add_done_callback(self._settle)`; `_settle(task, target)` 是唯一的登记清理点: 从 `self.tasks` 摘除并递减 `locks[target]` 引用 (引用归零才移除锁项)。正常返回、失败、取消全部经此路径; `run()` 的 finally 不再含 `tasks.discard`/锁操作。
- 取消路径: `task.cancel()` 后调用方有界等待 (`wait_for(shield(task), CANCEL_SETTLE_TIMEOUT=5s)`, 等待自身被重复取消时捕获 CancelledError 仍继续 re-raise); **无论等待结果如何, 登记保持到子任务真正终态**——超时只意味着调用方先走, 未终态任务继续登记、占用 64 容量, 同目标锁仍在表中, 新发送在同一锁后排队, 不并发。
- `run()` 在建任务前的早退分支 (closed/满载) 不得增加锁引用; 锁引用递增减只在建任务成功后配对。
- `close()`: 取消全部任务后有界 gather; 超时仍有未终态任务时**明确报告清理未完成** (日志 + 返回值/异常), 不得声称全部终态。

测试 (`tests/unit/test_onebot_outbound.py`):
- 复审要求: 清理超过超时 (子任务 finally 卡 6s → 调用方 5s 退出后, 同目标新发送仍排在旧锁后, 不并发); 外层重复取消 (等待期间再取消调用方); close 与取消竞争; close 超时报告未清理任务数。
- 复刻原审计探针: 日志顺序必须 `first_cleanup_started, first_cleanup_done, second_started`。

---

## 批次二

### A7 能力状态语义 + 补齐读取/转发工具 (含 R6 修订)

目标: 传输连通性与动作能力分离; unknown/supported/unsupported/unavailable 四态; 被动学习; 能力缓存按连接代次失效, 不假设实现不变。

改动点:
- `adapter.py` `admin_capabilities` (:509-517): 四态。`unavailable` 判定改为**实际连接状态**而非 `_bot` 对象存在: 实现时先核对所装 aiocqhttp 版本是否暴露连接/断开回调, 有则挂钩维护连接标志与**连接代次计数器**; 无则以被动传输观测 (动作传输层错误/回声超时) 推导; 二者皆不可得时保持 unknown 并在文档声明限制, 不以 `_bot is not None` 充当已连接。
- 被动学习: 管理动作统一调用点成功 → supported; retcode ∈ `MISSING_ACTION_RETCODES` (10002/1404) → unsupported。**每次新连接代次把已学习状态降级回 unknown** (被动重新学习), 不做主动写探测。
- 新工具 (`group_admin/tools.py`): 只读 `group_admin_get_message`、`group_admin_get_forward`; 写 `group_admin_send_forward`。**发送类工具统一走公共发送路径** (`adapter.send_message` → 既有拆分/OutboundTurns 整轮排序/容量/关闭生命周期), 不得直接调私有 `_send_forward` 绕过约束; 上传/转发的调用结果同样接入能力学习。
- 读取工具同样核验对象归属: `get_message` 回源后校验消息实际群 ∈ 允许范围, 异群拒绝 (防借读工具跨群读消息); `get_forward` 复用 `fetch_forward_message` (:337-370) 已有的群范围校验, 不直接调裸 `get_forward_msg`。

测试: 四态迁移与连接代次降级; 实际连接状态来源的降级声明; 新工具经公共发送路径 (断言经 OutboundTurns); 读工具跨群拒绝; 写工具开关与群白名单。

### File 出站分流 (含 R6 修订)

目标: File 按目标实现能力分流, 混合链保持前后顺序; 措辞与验收均不断言现状经 send_msg 必然失败, 也不把"尝试了另一个动作"视为交付成功。

改动点:
- **先行核对** (实施第一步, 结果记入进度文档): 从本机 SnowLuma 1.14.17 发行包源码核对 `upload_group_file`/`upload_private_file` 的参数形态 (file 接受路径/URL/base64 的哪些)、成功响应结构 (是否含 message_id、完成语义), 不套用普通消息回执假设; 核对方式沿用 `issue-10-audio-convert-plan.md` 的事实核查模式, 不启动真实 QQ。
- `adapter.py` 发送路径 (:560-630 同层): File 组件拆出, 群 → `upload_group_file`, 私聊 → `upload_private_file`; 无 file 且无 url → `failed` 回执。
- 能力联动: 上传返回 10002/1404 → 标记 unsupported (接入 A7 学习); **回落现行 file 段 send_msg 仅为未验证兼容尝试**: 回执标注 `fallback_unverified`, 日志明示, 文档不宣称该路径构成文件送达; 同连接代次内不重复试错。
- 顺序: 普通段合并、File 上传、Node 转发按组件原序; 空普通段跳过。
- 验收: 补 SnowLuma 网络层 + 模拟 QQ 动作的混合链探针 (扩展 `scripts/probe_snowluma.py` 形态, QQ 侧仍模拟): Plain/File/Plain 动作序列与次序断言; 部分成功与未知结果分别断言; 不执行真实 QQ 管理动作。

### A4 统一输入总预算 (含 R7 修订)

目标: 复审指出"扣额度不裁剪顶层存量"不成立。改为**顶层媒体同样按总额度裁剪**, 字符预算覆盖全部拼接成分, 在最终 UserCall 验证。

改动点:
- 配置 (`platform_policy.py`): `input_text_limit` (默认 20000, 1..200000)、`input_media_limit` (默认 8, 1..32); 平台级, 不进群覆盖清单。
- `input_projection.py`: 顶层 `images`/`videos` 先按 `input_media_limit` **实际裁剪** (截断列表本身, notes 记 `top_media_truncated`), 余量再供引用/转发媒体消耗。
- 字符预算记账覆盖: 来源头 (`[引用 ...]` 等标记行)、分隔符、截断提示 (`…` 与块标记) 及 A2 窗口块拼接全部计入; `project_input` 内以 `ProjectionBudget` 逐块记账, scheduler 合并窗口块时消耗剩余额度。
- 极小限额契约 (防互相矛盾的测试): 固定优先级——当前问题正文 (保前缀) > 来源标记头 > 资料块内容; 预算不足以全保正文的, 按优先级截断并记 notes; 测试只断言优先级顺序与 notes, 不断言小限额下"正文与标记同时完整"。
- 最终验证: 在 scheduler 集成用例的实际 UserCall 上断言总额。

### talk_value 映射 (含 R2 重写)

目标: 复审核实两点——talk_value 是**发言频率配置** (用户此前确认), 不是事件载荷字段; 频率判断位于 `WakeWindow.decide` (`wake_window.py:165`) 而非 `evaluate_wake`。据此重写:

改动点:
- 配置: `wake_talk_value` (浮点, 0..1, 默认缺省=不启用映射), 进平台设置与 `wake_overrides` 群/时段覆盖键清单; `platform_policy.py` 校验范围与单调性。**不新增任何群活跃度采集调用**。
- 纯函数映射 (`wake_policy.py` 或新模块): `map_talk_value_threshold(talk_value, base) -> int`, 单调不减地把频率偏好映射为消息条数阈值 (如 talk_value 1.0→1 条, 0.5→3 条, 0.2→8 条的固定阶梯, 初值由实现测试确定并写入文档)。
- 接入点: `WakeWindow.decide` frequency 分支——有效配置 (经 `resolve_wake_settings` 合并平台/群/时段后已在 `policy_settings`) 中**显式设置 `wake_message_threshold` 时显式值优先**, 否则若设置 `wake_talk_value` 则用映射阈值, 皆无则用默认 3。`talk_value=0` → 自动参与不触发, 但**不得连带关闭**显式 @ 与手动唤醒 (后两者不经 decide automatic 路径, 加回归测试锁定)。
- 决策 `reason` 记录所用 talk_value、映射结果与配置来源, 供批次四试算展示。

测试: 以**不含 talk_value 的普通事件**验证配置生效 (修改有效配置即改变触发行为); 显式阈值优先级; 零值行为 (@ 与手动不受影响); 单调性与边界; 无新增网络调用。

---

## 批次三

### A5 手动请求与发送状态持久化 (含 R5 修订)

目标: 复审指出文件锁+原子替换只解决写入完整性, 不解决"查重→占位→入队"竞争, 且普通回复的发送尝试也在方案 :94 范围内。修订如下:

改动点:
- 存储 `satrap/core/pipeline/manual_wake_store.py`: 数据目录 (经 `storage/layout.py`) 下 JSON 文件, FileLock + 原子替换。记录 `{request_id, fingerprint, adapter_id, target, operator, status, detail, created_at, updated_at}`; status ∈ accepted/executing/sent/partial/failed/unknown。
- **接受事务**: 查重 + 持久占位 + 入队在 `BackendManager.wake_platform` 内由同一把异步锁保护; **成功落盘后才返回 accepted**; 落盘后入队失败 → 记录置 failed 并返回 rejected (不遗留假 accepted)。
- **幂等语义**: request_id 作用域 = 适配器实例; `fingerprint` (现有 `requests.fingerprint`) 覆盖目标/prompt/operator; 同 ID 异指纹 → 返回冲突 (409 语义), 同指纹 → 返回原状态。
- **发送尝试记录**: 适配器发送路径在**发送 I/O 之前**持久化尝试记录 (turn id、目标、段摘要、status=submitted), 回执到达后逐段更新 (混合链允许 partial); 重启时 submitted/executing 一律标 unknown, **不自动重发**。
- **降级与保留**: 存储文件损坏 → 保留原文件 (改名隔离) 并进入显式降级状态——无法保证去重时**拒绝依赖去重的新手动请求**, 其余功能照常启动; 保留期 7 天, 容量耗尽时未决记录 (accepted/executing/unknown) **不得静默淘汰**, 采用轮转归档 (`.1` 一代) 可恢复, 归档也满则明确拒绝新记录并告警。
- 查询: `BackendManager.manual_wake_status(request_id)` + http_api GET 路由 (批次四复用)。

测试: 复审要求的七类——并发重复、落盘失败、入队失败、发送后未回写即崩溃 (重启 → unknown 不重发)、部分成功 (混合链部分段成功)、记录损坏 (隔离 + 降级拒绝新请求 + 其余功能可用)、容量耗尽 (未决记录不丢失, 归档可恢复)。

### A6 ASR 配置删除引用检查

目标与改动点同前版: `BackGroundManager.remove_asr_config` (:589-608) 删除/重命名前扫描平台 `asr_model` 绑定与插件 asr 引用, 命中抛 `ConfigInUseError` (含引用清单); `control_server.py` DELETE 分支 (:1475 附近) 映射 409 + 结构化 body; 前端 Models 页经既有错误通路展示清单。复审未提出异议, 契约不变。

测试: 已绑定删除 → 409 且配置仍在; 无引用删除正常; 最后一项空配置重置仅无引用时发生; 重命名冲突; 引用清单正确。

---

## 批次四

### A8 前端交付缺口 (含 R8 修订)

目标: 方案 7.2/7.3 交付范围; 复审指出试算与记录的数据源覆盖不完整, 修订如下:

后端:
- **试算**: 抽取与真实路径共用的决策逻辑 (不复制实现)——直接复用 `evaluate_wake` + 构造**隔离的临时 WakeWindow** 按样例消息时间注入 (`observe`/`decide` 已支持 `now` 参数, 可确定性推进), 覆盖 frequency/necessity/窗口/冷却/最长等待; 不触碰生产窗口, 不写状态, 不调模型。缺少必要上下文 (如生产冷却状态) 时返回明确的"无法判断"原因而非猜测。
- **逐次记录**: 未唤醒/限流消息可能从未进入投影, A5 存储与 `ProjectedInput.notes` 覆盖不到。在实际决策/限流位置 (scheduler 决策点、RateLimiter 拒绝点) 采集有界环形记录 (每适配器 256 条: 时间、路由键、操作者、阶段、决定、原因、消息/request id、发送结果), 只读端点供 UI 查询。**验收必含"未执行投影也能查到拒绝原因"的案例**。
- 手动唤醒状态查询: 批次三端点。

前端:
- 群/时段覆盖专用编辑器 (逐条行编辑, 继承/显式关闭/显式值三态) 替换 JSON textarea; 编辑器旁调 dry_run 展示有效策略预览。
- 脏状态保护: Modal 关闭 + **应用内路由切换 + 浏览器 beforeunload** 三处; 实施时先核查是否已有全局保护可复用, 有则验证并声明, 无则补齐。
- ManualWakeModal: request_id 状态查询与最终状态展示; 逐次拒绝/失败记录列表。

验收: vitest + tsc/eslint + Playwright 扩展脚本显式执行; dry_run 后端测试 (无模型调用, 含无法判断分支)。

---

## 范围与交付口径

- **搁置裁定的可追溯出处**: "历史验收记录有效性"与"发行包核验"两项搁置, 来自 2026-09-23 本会话用户消息 ("另外两项证据不足项先做搁置处理"), 已记录于 [复审文档](issue-10-goal-audit-review-2026-09-23.md) 第六节。即使搁置, 批次验收只报告本批范围完成与剩余项, **不得把搁置项计为通过完整目标验收**。
- 历史 ASR/LLM/SnowLuma 结果继续按原日期、版本和覆盖范围使用; 本方案改动影响到的链路做**定向补验**, 不要求全量重跑历史实验。
- 发行/静态资源验收口径: 按 README 的源码安装 + 前端构建 + 实际 HTTP 托管验证加载版本, 不新增 exe 安装器要求 (回应复审对 plan:262 前提的质询)。
- **pyright 基线**: "0 errors / 1532 warnings" 参考自 `e683fce` 收官门禁 (命令 `export PATH="/f/nodejs:$PATH" && npx --yes pyright -p .pyrightcfg --outputjson`, 记录于 progress 文档 A/B 批次验收); 各批次开始时重新记录实际基线, 验收记录区分原有问题与新增问题。

## 门禁与提交

每批次完成后: 全量 `python -m pytest tests/unit -q`; pyright 全库 0 error 且 warning ≤ 批次开始基线; 前端 tsc/vitest (批次四加 Playwright); 批次一/二复跑 `tests/benchmark` 入站与附件基准 (结果存 `tests/benchmark/results/platform/`); 同步更新 `../../platform/issue-10-progress.md` 与受影响文档。每批次一个提交, 提交信息沿 `fix: ... (目标审计批次 N)` 格式。全部批次完成后按方案 6 节口径重跑目标验收, 区分"功能存在/受控协议测试通过/真实网络与模型验收/全部目标满足"四档记录。
