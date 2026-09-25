# Issue #10 日志与错误边界整改独立复核

审查日期: 2026-09-25

提交范围: `b70b4e2..d20f992`, 重点为 `b3bb1b9`、`7bb39b8`、`fb829dc`、`fc63779`、`f13a3a2` 五批生产修复。审查开始时工作区干净, 本轮只新增复核记录, 不修改生产实现。

结论: **主要修复已完成, 但暂不能全部关闭。剩余 2 项 P2 与 2 项 P3。** 执行侧的全量验收记录与本轮独立验证分开列示, 不以已有绿测替代行为核查。

## R1 / P2: no-op 分类仍吞掉异常状态拒绝

位置: `satrap/core/pipeline/manual_wake_store.py:865-868`, `satrap/core/pipeline/scheduler.py:176`, `tests/unit/test_manual_wake_store.py:77-93`

此前裁定明确要求记录不存在、非法迁移归入 rejected, 仅明确幂等情形属于 no-op。当前命名采用 `persisted/no_op/degraded/invalid/io`, 名称差异本身没有问题, 但语义没有落实:

- 记录不存在直接返回 `no_op`
- 任意已有终态收到任意更新均返回 `no_op`, 未区分目标已满足与冲突迁移
- scheduler 对这些情况仅输出 debug, 因而状态丢失/错误调用仍被归为正常未推进
- 新增测试把 missing→executing 与 sent→failed 都断言为 `no_op`, 固化了该偏差

真实 store 探针结果:

```text
missing_result= no_op
sent_result= persisted
conflicting_final_result= no_op
```

建议: 使用现有 `invalid` 表示缺失记录与不允许迁移, 或改名 rejected; 同目标终态才按明确幂等规则 no-op。如果归档记录或停止后的迟到确认需要作为合法拒绝, 应明确识别这些情形, 不能把所有 missing/terminal 都认作幂等成功。保留不覆盖终态的保护, 修改分类而非放开迁移。调整对应反例测试并验证告警等级。

## R2 / P2: clear_manual_wakes 仍同步阻塞事件循环

位置: `satrap/core/pipeline/scheduler.py:96-112`, `satrap/core/backend/BackendManager.py:816,889`, `satrap/core/storage/file_lock.py:31`

此次增加了存储异常捕获, 已解决异常打断清理的问题, 但 `adapter_stopped()` 仍由异步热重载/实例替换流程直接同步执行。它会取得线程锁与文件锁, 文件锁默认等待上限为 30 秒, 等待实现使用同步 sleep。

独立探针由另一线程持有真实 FileLock 约 350ms, 事件循环先安排 call_soon 回调, 随后调用 clear_manual_wakes。回调延迟 **0.353s**, 证明等锁期间同一事件循环无法继续服务其他任务。持续争用可一直阻塞到锁超时。

建议: 清理入口改为可 await, 持久状态回写通过 `asyncio.to_thread` 执行; 两个生命周期调用点等待其完成, 保持内存票据撤销与诊断清理顺序、存储失败 log-and-continue。不要用未跟踪后台任务替代等待, 避免平台新实例启用与旧状态收尾竞争。补心跳/事件循环可推进的争用测试, 仅断言“不抛异常”不足。

## R3 / P3: 缺少约定的旧错误信封页面回归

位置: `satrap-ui/e2e/manual-wake.mjs:36-41,131-136`, `satrap-ui/src/api/client.test.ts:20-24`

409 + reason 的页面用例已通过真实请求拦截器, 并检查中文原因及输入保留。但旧夹具被直接替换, 没有新增要求的 400 + error 页面分支。400 目前仅通过 `toApiError()` 辅助函数单测。

这是验收缺项, 不是已证实的生产错误。建议并列保留两种响应模式, 两种都检查实际 toast 与输入保留。现有 409 测试不要撤回。

## R4 / P3: 发送收尾异常重复输出 warning

位置: `satrap/core/platform/onebot/adapter.py:918-932`

`completed` 初始为 False; 取消、超时、异常分支分别已记录 warning, 随后还会进入 `if not completed` 再记“未落盘”。同一次失败输出两条上层告警, 不符合此前单边界一次记录的要求。

建议: 仅在调用正常返回 False 时记录未落盘 warning, 放入 try/except 的 else 分支即可。保留异常分支原告警, 不改变 shield、超时和后台写入语义。此项来自静态控制流确认。

## 七项裁定落实情况

| 裁定 | 复核结果 |
| --- | --- |
| E1 同域 flag 摘要 | 通过: 共用 flag_digest, 未改变摘要算法; 成功日志带 adapter/self_id, 匿名禁言不写 flag |
| E3 五值结果 | 部分完成: 上层显式消费结果码, 未发现字符串当 bool 的这类改造错误; 分类偏差见 R1 |
| settlement 查询异常 warning | 通过: 带 adapter/request_id 与异常类型, 异常后直接返回, 不循环刷同一失败 |
| clear_manual_wakes 加固 | 部分完成: 存储异常被捕获, 清理可继续; 线程卸载未落实, 见 R2 |
| ApiError.code 与 CLI | 通过: code 保留 reason, UI 映射并兜底未知码; CLI 无 error 时展示 reason, 维持错误异常语义 |
| 409 reason 与 400 error 双 E2E | 部分完成: 前者通过, 后者只有辅助函数单测, 见 R3 |
| E5 三处转换兜底 | 通过: 普通消息/转发/文件复用脱敏失败日志, PermissionError 保持明确回执且不新增日志 |

锁进入异常已类型化, POST/状态查询的原因与状态码契约保持, 相关反例测试通过。发送收尾 False 已被检查, 但有 R4 的重复告警。

## 本轮独立验证

- 后端: `python -m pytest tests/unit/test_manual_wake_store.py tests/unit/test_onebot_admin.py tests/unit/test_pipeline_scheduler.py tests/unit/test_onebot_adapter.py tests/unit/test_platform_logging.py tests/unit/test_cli_client.py tests/unit/test_durability.py -q` → **233 passed**
- 前端: `npm test -- src/api/client.test.ts` → **4 passed**
- 页面: `npm run test:e2e:wake` → **PASS**
- 额外探针: 真实临时 store 的结果码分类, 真实线程文件锁争用下的事件循环延迟; 无真实平台网络动作
- 本轮未复跑全量 pytest、pyright、全量 vitest、tsc/eslint; 执行记录中的全量结果不冒充独立复测

关闭条件: 修正 R1/R2, 补齐 R3, 顺手消除 R4; 更新整改记录, 不再把当前状态描述为 E1-E5 全部闭环。
