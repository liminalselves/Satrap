# Issue #10 目标实现审计

审计日期: 2026-09-23

基线: `fix/issue10`, `e683fce16d3c17e3a6cd08f011451e2412a29ec8`; 审计开始时工作树干净。本次不修改业务代码, 不执行真实 QQ 管理动作, 不调用付费模型。依据为分享对话中的用户裁定、`../../platform/issue-10-plan.md` 和当前源码; 前序收官记录作为历史证据, 不替代当前实现核查。

结论: **不通过完整目标验收**。主要能力已有实现, 但仍有可复现的跨群范围校验缺陷、输入丢失和取消清理问题, 且若干明确交付要求尚未实现。不能以现有测试全绿或“P2/P4 全部完成”作为关闭依据。

## 可复现缺陷

### A1 [P1] 撤回和加群审批没有核验实际操作对象的群归属

位置: `satrap/core/platform/onebot/admin.py:391` / `:568`; 工具入口 `satrap/expend/plugins/group_admin/tools.py:136` / `:175`

`recall_message` 仅检查参数 group_id, 随后只带 message_id 调用 delete_msg; `handle_group_request` 同样只检查传入群号, 再凭 flag/sub_type 审批。工具参数来自模型, 传入允许群号并不能证明 message_id/flag 属于这个群。因此开启写工具且限定群 A 时, 已知群 B 的消息 ID 或审批 flag 仍可通过 A 的范围检查, 最终动作作用于 B (前提是平台账号有相应权限)。

离线探针: 适配器只允许群 `123`; get_msg 替身返回消息 `77` 属于群 `999`; 调用 `recall_message('123', 77)` 后 delete_msg 实际收到 `message_id=77`, get_msg 调用数为 0。调用 `handle_group_request('123', 'flag-for-group-999', 'add', True)` 后审批动作仍提交该 flag。所有动作均为 AsyncMock, 未接触真实平台。

修复验收: 从可信回源/接收登记核验实例、账号、实际群和对象标识, 请求 flag 绑定 sub_type 并有界保存处理状态; 不匹配、缺失或过期时拒绝, 执行前复查当前群范围。补工具到协议动作的跨群反例, 不能只断言传入的 group_id 在白名单内。

### A2 [P1] 自动参与模式覆盖已补全的引用、转发和附件正文

位置: `satrap/core/pipeline/scheduler.py:255`

frequency/necessity 分支会观察顶层正文, 即使当前消息已经明确 @机器人也会产生 pending/batch。管线完成引用/转发/附件补全后先用 projected.message 构造 UserCall, 随后有 batch 时直接覆盖为 PendingText 的顶层纯文本。引用、转发正文以及已付费获取的 ASR/文件正文因此丢失。

离线探针使用同一条 `@机器人 + Reply + please summarize` 消息, 引用回源固定返回 `QUOTED_SECRET_CONTEXT`: explicit 模式的实际 UserCall 包含引用; frequency 模式回源同样调用 1 次, input_projection 也包含引用, 但实际 UserCall 仅为 `[用户 123, 消息 78] please summarize`。

修复验收: 将当前事件补全后的内容与已认领窗口合并并去重, 不覆盖整个输入; 保持成员隔离。覆盖 frequency/necessity 下明确 @、阈值触发以及正文混合 Record/File/Reply/Forward 的模型实际入参。

### A3 [P2] 发送取消后未等待子任务退出就释放锁登记

位置: `satrap/core/platform/onebot/outbound.py:149-164`

外部取消时调用 task.cancel 后立即 re-raise, finally 立即从 tasks 删除子任务并减锁引用。cancel 是请求取消, 子任务可能仍在异步 finally 中清理资源。若此时没有其他等待者, 原目标锁被移除, 新发送使用新锁, 且 close 无法再等待已被移除的旧任务。

离线探针让发送 operation 在 finally 中等待一个事件: 取消调用方后, 同目标第二个 operation 已启动, 日志顺序为 `first_cleanup_started, second_started`; close 返回时 tracked tasks 为 0, 第一任务仍未清理结束。

修复验收: 取消后等待子任务终态再释放登记, 保留外部取消语义; 关闭过程也要覆盖清理中的任务。补有异步清理的取消测试, 验证新回复不提前获得同目标发送权。

### A4 [P2] 缺少单次请求的统一文本与媒体总预算

位置: `satrap/core/pipeline/input_projection.py:238-243` / `:295`

当前有单条引用、单条转发、单个附件的局部限制, 但顶层正文和顶层图片/视频直接进入输出; `_MediaBudget(4, ...)` 只限制后来补入的引用/转发媒体, 不扣除顶层已有数量。多个资料块拼接后也没有整体截断。90 秒附件耗时上限不能替代模型输入预算。

纯函数探针传入 100000 字符正文与 100 张顶层图片, 输出仍为 100000 字符、100 图片, notes 为空。该测试证明投影层未实施主方案要求的全输入预算, 不宣称所有模型供应商都会接受这些资源。

修复验收: 当前问题、窗口、引用、转发、文件和 ASR 共享可配置总字符/媒体预算, 优先保留当前问题和来源, 超限记录删减状态; 在最终 UserCall 上验证限额。

## 明确交付缺口

### A5 [P2] 手动请求及发送状态未实现重启恢复

主方案第 0 节明确要求已接受手动请求和已提交发送尝试保留最小持久记录, 重启无法确认的结果标 unknown, 不自动重放。当前 `manual_wake.py:40` 使用内存 OrderedDict/WeakKeyDictionary, 状态只在本进程有效; BackendManager 唤醒只查询该表。`MessageEvent.last_send_receipt` 为事件内存字段, 未形成持久请求状态链路。重启后相同 request_id 可重新入队, 也无法查询先前未知发送。

应补持久幂等/状态接口和重启验收, 或由用户明确裁定缩减此要求; 文档里声明“进程内”不等于完成原要求。

### A6 [P2] ASR 配置删除没有引用检查

位置: `satrap/core/config/model_service.py:122`; `satrap/core/framework/BackGroundManager.py:589`; control 删除分支 `control_server.py:1475`

控制端直接调用 service.delete, 后者直接删除 ASR 配置; manager 对最后一项会重置为空配置, 其他项直接移除, 均不检查平台 asr_model 或插件引用。用户删除正在绑定的配置后, 原平台绑定仍存在, 下次转写失败或降级。主方案第 7 节要求删除时检查平台/插件引用。

应返回结构化引用冲突或原子迁移引用, 同时覆盖重命名; 新增“已绑定 ASR 不可静默删空”的管理接口测试。

### A7 [P2] 管理能力状态把客户端对象存在等同于动作支持

位置: `satrap/core/platform/onebot/adapter.py:509-517`

只要 `_bot is not None`, 所有 ADMIN_CAPABILITIES 均返回 supported; CQHttp 对象是在监听启动时创建的, 无需 SnowLuma 已连接, 更无需确认其支持动作。收到 unsupported 后也不更新能力记录。这违反方案要求的 unknown/supported/unsupported/unavailable 区分, 当前状态不能用于证明管理能力可用。

应将传输连通性与动作能力分别记录, 未核验显示 unknown, 明确不支持后缓存 unsupported, 不靠写动作探测。当前 group_admin 定义表也缺少方案列出的消息读取、转发读取/发送对应工具; 适配器内部有方法不等于模型可调用工具已交付。

### A8 [P2] 前端和运行状态仍未达到方案交付范围

位置: `satrap-ui/src/pages/Platforms/index.tsx:267-268` / `:490`; `satrap-ui/src/pages/Sessions/ManualWakeModal.tsx:37-41`

- 群/时段覆盖仍为 JSON textarea, 没有方案指定的逐条编辑、继承/显式关闭区分和有效策略预览
- 未找到复用真实 WakePolicy 的无副作用规则试算接口或前端入口
- 平台表单关闭直接 setShowModal(false), 没有脏状态离开保护; 当前修订冲突保留输入只覆盖一种草稿丢失路径
- 手动唤醒 accepted 后关闭弹窗并 toast, 没有按 request_id 查询执行/发送最终状态的界面; 页面不展示逐次未唤醒/限流/转录失败/发送未知记录

这些均是主方案 7.2/7.3 的明确要求, 现有三份浏览器脚本通过不足以证明它们已交付。

### A9 [P2] File 出站缺少按目标实现能力分流

复审后由证据不足升级为确认缺失。`onebot_utils.py:271` 将 File.to_dict 结果放入普通消息段, adapter 的分流只处理 Node/Nodes, 没有 File 专用动作或能力分支; satrap/scripts/tests 中未找到 upload_group_file/upload_private_file 实现或对应测试。这证明方案要求的能力分流尚未实现, 不单凭动作名零命中推断实际 SnowLuma 的全部行为。具体动作载荷、返回及混合链顺序仍需目标版本网络模块与模拟动作验收。

### A10 [P2] talk_value 到参与阈值的映射缺失

复审后确认为交付缺口。主方案唤醒策略表明确要求该映射, 当前只有显式 wake_message_threshold, 未找到 talk_value 配置/换算/前端入口。talk_value 是参考项目的机器人发言频率配置, 不应误解成 NapCat 上报的群活跃度或要求新增群活跃度 API。Satrap 应定义确定性映射、配置优先级及边界测试, 不要求照搬参考项目算法。

## 其他验收证据边界

- ASR 同层封装、模型配置、媒体提取和真实 ASR/LLM 历史验收记录已存在; 本轮没有重新调用外部 API, 不将历史记录说成本轮实测
- 发行检查历史记录证明前端 build, 但实际分发产物/静态服务版本核验仍缺证据。按 README 的源码检出、editable 安装、前端构建和后端托管链路在干净环境验收即可; 不额外要求新建 exe/安装器。历史证据按覆盖范围和后续代码变化评估, 无须仅因跨审计轮次全部重跑

复审回复和执行口径见 [复审确认回复](issue-10-goal-audit-response-2026-09-23.md)。A5/A8 保持原交付范围, 工作量本身不构成重新等待用户裁定的理由。

## 验证记录

- 定向测试: `python -m pytest tests/unit/test_group_admin_plugin.py tests/unit/test_onebot_admin.py tests/unit/test_attachments.py tests/unit/test_onebot_outbound.py tests/unit/test_manual_wake.py -q`, **92 passed**
- 额外只读探针: A1/A2/A3/A4 全部复现上述结果, 使用真实当前类/函数及受控 I/O 替身, 无真实群管理动作、下载或模型调用
- 全量单元测试: `python -m pytest tests/unit -q`, **1875 passed / 7 skipped / 0 failed**, 183.59 秒; 跳过包括显式集成开关、缺少 reportlab 和 Windows 符号链接权限。当前环境结果与旧收官记录的 1862/19 口径不同, 以本轮输出为准

建议先修 A1/A2/A3, 再补 A4-A8 及剩余验收证据。将“功能存在”“受控协议测试通过”“真实网络/模型验收”“全部目标满足”分别记录, 不再以部分批次收官代替完整目标完成。
