# C1/C2/D1/D2 自动化验收

日期: 2026-10-05

代码, 自动化检查和真实平台现场验收分别记录. 真实 QQ 验收由用户在测试群发送消息, 本地核查工具调用, 来源和持久化结果

## 检查结果

| 检查 | 结果 |
| --- | --- |
| 后端全量默认测试 | 最终完整复跑 3169 passed, 20 skipped, 用时 427.95 秒 |
| 最后并发身份复核增量检查 | 记忆与提醒相关 36 passed, 新增运行时 HTTP 认证/创建/恢复检查 1 passed |
| 前端单元测试 | 27 个文件, 234 passed |
| 前端 lint | 通过, 无警告 |
| 前端 tsc 与生产构建 | 通过 |
| 本轮生产 Python 模块 pyright | 0 errors, 0 warnings |
| 新增记忆/提醒浏览器冒烟 | 通过, 已查看 390px 窄屏截图 |
| 既有浏览器回归 | 档案, Agent 路由, 对话编辑, 群聊插件, 好友, 摘要, 媒体共 7 个脚本通过 |
| 真实 QQ 现场验收 | 进行中; 成员偏好保存/注入/重启/更正/忘记, 群约定审批, 短提醒发送, 模型取消及前端创建后的重启发送已核验; 离线补发发现错误分类缺陷并修复, 现场复测及停用恢复待执行 |
| 现场重启误暂停修复 | 提醒相关 7 个文件 47 passed, 最终参数化启动/真实停用反例 2 passed; 生产模块 pyright 无错误, 前端 234 passed / lint / tsc / 构建通过 |
| 现场离线误暂停修复 | 6 项针对性检查通过; OneBot, 群聊, 管理与提醒相关回归 453 passed; adapter.py 的 pyright 无错误 |

20 个跳过项中, 16 项为需要显式启用的集成测试, 1 项缺少可选 reportlab, 3 项需要当前 Windows 会话没有的符号链接权限
默认全量测试没有调用外部模型, embedding 或 RAG 服务, 不据此声称这些外部服务已通过验收

## 契约与证据

| 批次或边界 | 自动化证据 | 覆盖内容 |
| --- | --- | --- |
| C1 独立插件与迁移 | test_memory_plugin.py, test_memory_migration.py | 旧记录 ID, 能力关闭状态, 新显式配置优先, 原子迁移与重复迁移 |
| C2 模型真实管线 | test_memory_platform.py | 同步/异步 Agent, 无 base_take/group_chat 依赖, 下一轮注入, 主/子工作流与读后身份复核 |
| 群记忆数据 | test_scoped_memories.py | 本人所有权, 当前来源, revision, 幂等, 提案审批, 失效来源与跨范围游标 |
| 人工记忆管理 | test_memory_management.py | 实际控制请求认证, 人工 CRUD, 冷数据访问与审批 |
| D1 发送权与证据 | test_reminder_store.py, test_scheduled_sends.py | 取消竞争, 事务失败不发送, 部分回执, sending 重启变 unknown, 未知结果不重试, 所有分段有确认才能 sent |
| D1 原生适配器 | test_onebot_reminders.py | 真实 OneBot 分段/串行队列代码, 独享发送账本, 队列等待后账号复核, 缺少消息 ID 不视为成功 |
| D1 时间与调度 | test_reminder_time.py, test_reminder_scheduler.py | 相对接受时间, 本地/显式时区, DST 歧义/不存在时间, 时钟跳变, 离线退避/宽限, 停用不自动恢复 |
| D2 模型工具 | test_reminder_plugin.py | 实际同步/异步模型工具管线, 创建幂等, 来源等待期间删除, 本人查询, 禁止子工作流写入与参数伪造 |
| D2 当前授权 | test_reminder_host.py | 真实命名配置/Provider, 非数字虚拟平台 ID, 平台实例, 账号, 群, 插件/能力, 来源覆盖与路由切换 |
| D2 运行时 HTTP | test_reminder_host.py, test_reminder_management.py | 完整 HTTP 认证边界, 创建幂等, 恢复版本冲突和当前权限, 冷读取/取消, 删除来源标不可用 |
| 长期数据清理 | test_conversation_long_term_cleanup.py | 默认保留, 独立勾选, 跨群/账号隔离, 提案正文擦除, 全事务回滚, 已发送权取得后不取消, 档案恢复不恢复记忆或任务 |
| 本轮人机界面 | e2e/group-chat-memory-reminders.mjs | 来源查看, 成员偏好增删, 群约定编辑/审批, 冲突保留草稿, 创建失败稳定幂等键, 日期转换, 取消/恢复, unknown 提示, 账号隔离和清理影响范围 |

OneBot 自动化使用替身客户端回包, 未连接真实适配器. 浏览器脚本运行真实前端与 Vite, API 使用替身路由; 后端认证和真实数据库操作由独立 HTTP 测试覆盖

## 复现命令

```powershell
F:/conda/python.exe -m pytest -q --durations=10
F:/conda/Scripts/pyright.exe --pythonpath F:/conda/python.exe satrap/core/memory satrap/core/group_chat/reminders.py satrap/core/group_chat/reminder_host.py satrap/core/group_chat/reminder_service.py satrap/core/group_chat/reminder_management.py satrap/core/group_chat/reminder_scheduler.py satrap/core/group_chat/reminder_time.py satrap/core/platform/scheduled.py satrap/expend/plugins/memory
```

运行命令前须按项目规定设置 PowerShell 输入/输出和 Python UTF-8, 大规模测试限制 OMP/BLAS 工作线程. 前端命令在 satrap-ui 下执行:

```powershell
npm.cmd test
npm.cmd run lint
npm.cmd run build
npm.cmd run test:e2e:group-chat-memory-reminders
```

## 真实 QQ 现场证据

测试群 1125293646, 平台 onebot-platform, 机器人账号 3588795965, 实际 Agent 为 onebot-edictum. 已核对保存配置和群运行时配置中的 group_write_enabled=true, reminders_enabled=true, get_memory=true; OneBot connection-test 返回 ok=true

开始验收前, OneBot 平台数据库的 memories 和 group_chat_reminders 均为空. 第一项成员偏好保存已核验:

- 上下文记录存在实际 add_memory 调用, tool_call_id=call_00_GQSvHUcTlhfNCrz4kgsI1620, 对应工具结果为 saved
- 记忆 mm_e3e3578ca8a24dc8b3d8f9202a2382fa 已写入平台数据库, kind=member_preference, owner_user_id=2410323775, purpose_key=preferred_name, revision=1, origin=model
- 范围为 onebot-platform / 3588795965 / group / 1125293646, 内容为本群称呼本人为“验收小麦”
- 来源消息 748720587 属于同一范围, sender_id=2410323775, direction=inbound, status=active, verified=1, truncated=0; memory_refs 和 create 审计对应一致
- 用户确认收到 QQ 回复
- 新对话 zDmBSH_main 中, C2 首次提问前仅有 system 消息, 没有旧聊天记录或 add_memory 结果; user 消息 1526 实际包含上述记忆的自动注入块, revision=1, 来源可用
- C2 回复消息 1527 为“验收小麦 👋”, 本轮没有工具调用; 新对话自动注入已核验
- 后端从 PID 39412 / runtime_id=SEWJFfRvkvqNPiihFMON4AH4HiJFDj67 重启为 PID 40628 / runtime_id=P1F0KHz5fJnqEVmaDJGUNui6s69v0jGu, OneBot 通信能力恢复正常
- 重启后的新对话 jSDdTR_main 首次提问前仅有 system 消息; user 消息 1604 重新注入相同记忆 ID, revision=1 和来源, assistant 消息 1605 回复“在本群里我称呼你为 **验收小麦** ～”, 本轮没有工具调用; 重启后持久化读取通过
- C3 实际先调用 get_memory 读取 revision=1, 再调用 update_memory 携带 expected_revision=1 和本轮来源 970468989; 同一记忆更新为“验收小竹”, revision=2, 所有者不变, 更新来源为同成员已核验入站消息
- 更新后的新对话 r0PeSV_main 中, 消息 1620 注入 revision=2 及更新来源, 消息 1621 未调用工具即回复“验收小竹，我会在本群这样称呼你。”; 修改及新对话生效通过
- C4 实际查询 revision=2 后调用 delete_memory, 携带 expected_revision=2 和本轮请求消息 73234945, 返回 deleted / revision=3; 数据库中该记忆及其 memory_refs 均为 0 条, 保留最小 delete 审计
- 删除后的新对话 loOd58_main 中, 消息 1634 和 1636 均没有注入该记忆; 用户补充允许查询后实际调用 list_memories(user_id=2410323775), 返回 items=[], has_more=false; 删除和停止注入通过, 其他成员的记忆未被清理
- C5 实际 add_memory 返回 pending / proposal_id=mp_6c00bab259ef43d592980e041f675ee8; 提案处于 pending, decision_at=null, base_revision=0, 同范围 group_rule / acceptance_test_token 的有效记忆数量为 0; 来源 1997719517 为当前成员已核验入站消息, 待审批未生效通过
- C5 人工批准后提案为 approved, 新增群约定 mm_e6d1c7c06f5748408f53404fc7c4a684 / revision=1, 最小审计 actor_id=authenticated_operator, 来源仍为 1997719517
- 批准后的新对话 sShzwV_main 首次提问前仅有 system 消息; user 消息 1652 自动注入有效群约定, assistant 消息 1653 无工具调用即回答“C5-青竹-1005”; 人工审批及下一轮生效通过
- D1 实际调用 group_chat_create_reminder(after_seconds=30, mention_user_ids=[2410323775]), 返回 created / scheduled, 提醒 rem_dc1e871a3a644b348bd830fb857fc04e; created_at=01:19:38.860919+08:00, due_at=01:20:08.860919+08:00, 相差恰好 30 秒
- D1 最终 state=sent, retry_count=0, 仅有一次 attempt 和一个确认消息 ID -962296133; 同范围提醒正文的确认出站档案仅 1 条, 包含原生 At(user_id=2410323775), source=confirmed_send, verified=1; 用户确认收到到期消息
- D1 attempt 开始于 01:20:13.330449+08:00, 比到期晚约 4.47 秒, 落在默认 5 秒扫描周期内; settled_at=01:20:20.580729+08:00, 本次发送阶段约 7.25 秒, 完成比到期晚约 11.72 秒; 已送达不代表严格准点, 后续场景继续观察发送时延
- D2 实际 group_chat_get_reminder 读取 revision=1 / scheduled 后, group_chat_cancel_reminder(expected_revision=1) 返回 cancelled / revision=2; 提醒 rem_0d5bbf7fce5445f7ab37c986108ead07 的发送尝试和对应出站档案均为 0 条; 原定到期 01:26:06.670411+08:00, 核查时尚未到期, 过期后无发送仍待复查
- D2 到期后再次核查, state=cancelled, 发送尝试和对应出站档案仍为 0 条, 取消后未发送通过

## 现场发现的重启误暂停

人工创建的 D3 提醒 rem_866623970b97444299e1b6994f9f6f31 写入成功, creator_kind=operator, 提及成员 2410323775, 未依赖来源消息. 后端重启为 PID 25416 / runtime_id=z4y92C_8ctRGxcsDCW3PdsgT6bnVoab0 后, 该任务被误判为 paused / group_disabled, 没有发送尝试或出站记录. 平台实例 ID 和当前有效提醒配置未变化, 群和通信随后均正常

根因是群接入快照尚未加载时, OneBot 的 group_route 返回代次 -1, 群可见性临时为 false. 提醒宿主没有区分“快照未就绪”和“群已停用”, 导致启动扫描永久暂停任务. 另外, 客户端对象存在也不代表实际通信在线

修复使用通用路由代次和能力状态, 不在提醒宿主写死平台类型: 路由尚未就绪时返回 waiting / group_state_pending, 文本发送能力临时 unavailable 时按平台离线等待. 真正停用仍暂停, 不自动恢复已有 paused 任务, 不改变发送证据和未知结果不重发规则

参数化回归使用真实 OneBot 接入快照加载和原生队列代码, 替身客户端提供回执: 启动窗口不暂停未来任务, 快照和连接恢复后无需人工恢复即可单次发送; 真正不在允许群范围的任务仍暂停且不发送. 自动化通过后仍需真实 QQ 重启复测. 原现场误暂停任务已超出 10 分钟补发宽限, 保留为现场证据, 不用自动恢复修正历史状态

修复后真实重启复测已通过: 用户从前端创建 rem_6696f26e44864bb89e4c770033ac3a09, 正文 D3-重启复测-1005, 选择 2 分钟. 数据库 created_at=01:45:52.360480+08:00, due_at=01:47:52.360480+08:00, 间隔恰好 120 秒. 新后端 PID 18812 / runtime_id=Ohqa4TSQbJMyXE9xjYowci0oCxcMCX30, OneBot started_at=01:46:01.362674, 证明启动在创建后且到期前

最终 state=sent, paused_at=null, retry_count=0, 仅一次发送尝试, 一个确认消息 ID 2075637682, 一条 verified=1 / confirmed_send 出站档案, 含真实 At(2410323775). 用户确认收到消息. attempt 比到期晚约 3.98 秒, 发送阶段约 5.19 秒, 完成晚约 9.17 秒. 原误暂停任务已由人工取消为 cancelled / revision=3; 不再恢复原任务

### 现场离线补发误暂停与修复

前端创建 rem_c7d48190f8ec40219aea8e0ae69c9b9a, 正文 D4-离线补发-1005, 创建与到期相隔 60 秒, due_at=2026-10-05T01:53:04.903230+08:00. 用户暂时断开 SnowLuma 连接后, 任务变为 paused / revision=2 / member_unverified, 发送尝试和出站档案均为 0

01:53:06.930 日志记录 get_group_member_info 被平台拒绝, retcode=None; 01:53:06.945 提醒宿主记录成员核验失败. 代码将 SDK 的 ApiNotAvailable 与 ActionFailed 一并分类为平台明确拒绝, 导致暂时通信不可用进入不可重试的成员核验失败路径. 截图提示不证明成员已经退群

修复仅将 ActionFailed 视为明确拒绝. ApiNotAvailable 通过既有日志和未确认异常路径处理: 成员读取失败可重试, 调度器进入 waiting_delivery / member_unavailable, 连接恢复后重新核验并单次发送; 已提交的写操作或发送无法确认时保持 unknown, 不自动重试

回归覆盖读操作与写操作的异常分类, 真实 OneBot 成员读取断线/恢复, 调度器等待后重新核验且单次发送, 真正不可核验时仍暂停, 原生发送提交后不可用保持 unknown 且第二次调用不再次发送. 6 项针对性检查及相关 453 项回归通过, adapter.py 的 pyright 无错误. 原 paused 任务保留现场证据, 不自动恢复; 新任务的真实离线复测仍待执行

## 剩余真实 QQ 场景

沿用已授权测试群 1125293646, 启动包含本轮本地提交的最新后端和控制服务后进行:

1. 已完成成员偏好保存及数据库来源核验; 前端成员和来源展示仍待用户确认
2. 已完成新对话自动注入, 重启后读取, 更正及忘记后的新对话生效核验
3. 已完成群约定 pending 未生效, 人工批准后新对话读取核验; 验收结束时清理测试群约定
4. 已完成短提醒的模型创建和前端人工创建, 原生 @, 单次发送和实际消息 ID 核验; 前端最终详情展示仍待用户确认
5. 已完成到期前取消和等待过程中重启; 暂时离线恢复, 停用后暂停且再启用不自动恢复仍待现场验收
6. unknown/partial 和崩溃提交窗口继续使用隔离替身验证, 不在真实群故意制造重复消息

群聊 skill 引导模型仅在明确要求记住/更正/忘记时保存. 语义理解和是否正确采用偏好仍需实际模型回复验证, 自动化机械权限检查不替代这部分观察
