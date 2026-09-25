# Issue #10 缩减候选与日志、错误返回复核

日期: 2026-09-25

审查起点 `babc9a9`, 收尾 `b70b4e2`; 期间新增提交仅涉及文档、必填校验 E2E 与测试入口, 本文所引用生产实现没有变化。范围为 Issue #10 新增/修改实现及其直接调用边界, 重点检查此前规模比较指出的持久状态、控制面、专用 UI 与 ASR。只审查, 未修改生产代码。

## 一、缩减候选裁定

此前横向规模报告中的 2350 行持久状态、865 行控制/诊断、983 行策略治理是功能范围的物理行数, **不是可删除行数**。当前实现已经包含上一轮抽取, 不能继续把原先的“约 500 行平行实现”当作待消除余额。

| 候选 | 当前证据与裁定 | 建议边界 |
| --- | --- | --- |
| 好友/群审批执行流程 | `onebot/admin.py:811-830,855-874` 都是占用、调用、按异常分类结算; 属实 | 抽局部审批执行 helper, 参数归一化、群范围与 subtype 校验留在两个入口; 保持取消→unknown、明确结果→completed, 不扩大为通用管理动作框架 |
| 清单转发包装 | `durability.py:247` 的 `validate()` 没有检索到调用; 两个 store 的 `_read_manifest`、`_quarantine_names` 有纯转发 | 无调用成员可清理; 转发方法逐个检查是否作为故障注入边界再内联, 不为删几行破坏可测性; 实际收益有限 |
| 降级结果应用 | 两个 store 的 `_apply_degrade` 仍有赋值和日志相似, 但 MWS 还处理归档状态, 账本需兼容无磁盘模式 | 暂不抽取新的状态基类; 当前重复不足以抵消新增回调、参数和状态同步成本 |
| 写入/回滚重复 | MWS 已有 `_persist_attempt` (`manual_wake_store.py:1121`), 三个段状态操作在复用; 请求、归档、账本事务不同 | 不再把这一部分整体列为“尚未抽取”; 优先统一失败结果处理, 不统一缓存权威与锁内重读语义 |
| 前端数字字段归一化 | `adminMigration.ts:75` 显式列出的 5 个键已全部包含在 `PLATFORM_NUMERIC_KEYS` 中, 重复遍历 | 直接迭代该常量即可; 属于确定的小清理, 不能算大幅缩减 |
| 前端状态展示/错误提取 | ManualWakeModal 与 RequestDiagnosticsPanel 各有状态文案/颜色, 多个页面各自拆异常 | 可共享同语义状态展示和 ApiError 原因提取; 不把 manual 请求状态与所有诊断阶段状态强行合并成一个枚举; 错误提取应连同下文 E2 修复 |
| ASR 同步/异步包装 | 两个 transcribe 的异常分类、脱敏日志、None 返回基本相同; 参数准备和响应解析已经共用 | 只考虑小型错误描述 helper; 保留两种 API, 不用事件循环桥接来省代码, 不值得单独开展重构 |
| 手动请求缓存与持久记录 | `ManualWakeRequests` 承担在途 ticket/取消/弱引用关联, store 承担重启后状态和去重 | 不能直接删缓存或视为重复账本; 移除短期 records 索引需重新证明并发受理、无 store 场景、TTL 与容量行为, 当前不批准按重复代码删除 |
| 诊断与持久发送证据 | 前者包含普通请求的有界阶段记录, 后者支撑跨重启裁决和不可重放语义 | 保留, 不共用一个权威状态容器 |
| 试算与运行时策略 | 试算已调用同一决策/覆盖实现, 自身主要是输入与结果说明 | 保留模拟驱动; 不把无副作用试算强行接入生产调度器 |
| 严格 ASR 引用扫描 | 扫描失败必须阻止误删, 与插件加载的宽松扫描职责不同 | 保留严格失败语义与版本判断, 不以复用宽松扫描替换 |
| 清单 raw 与归一化视图 | 账本必须保留历史扩展字段, MWS 必须严格归一化 | 这是刚修复的兼容契约, 不再删 raw 或统一为一次字典重建 |

结论: 可以做小范围清理, 但没有证据支持当前还能无损削掉数百行。更大规模下降需要减少功能或更换存储方案, 属于另一个设计项目。建议先修错误边界, 顺手消除同一位置的重复, 再用实际 diff 统计净减少; 不给未经实现验证的行数承诺。

## 二、日志与错误返回发现

### E1 / P2: 审批成功日志写入原始 flag

- 位置: `satrap/core/platform/onebot/admin.py:291-292`
- `_call` 的写动作日志白名单包含 `flag`; 好友与群审批分别在 819、863 行把原始标识交给 `_call`, 匿名禁言也传入 flag
- 使用假客户端执行 `set_friend_add_request`, 注入 `SYNTHETIC_AUDIT_FLAG`, 捕获到 info 日志包含该完整值; 无真实网络动作
- 影响: 虽然持久审批账本只保存摘要, 日志仍保留原始动作标识; 降低了账本脱敏的效果。这里不声称持有日志就一定能绕过占用保护
- 建议: 日志去掉原始 flag, 如需关联仅用同域摘要, 补 adapter/self_id/action; 不记录 remark/reason/正文。测试直接断言成功与失败日志均不含原始 flag

### E2 / P2: 唤醒接口的结构化拒绝原因在前端丢失

- 位置: `satrap-ui/src/api/client.ts:42-50`, `pages/Sessions/ManualWakeModal.tsx:119-134`, `satrap/core/backend/http_api.py:108-118`
- 后端拒绝使用 `{status: "rejected", reason: "queue_full"/...}` 并返回 400/409; 通用 Axios 拦截器只读取 `data.error`, 不保留 `reason` 或响应体
- 因此非 2xx 不进入 modal 的 `result.reason` 分支, 而是抛出只有通用 HTTP 错误消息的 ApiError。队列满、请求 ID 冲突、存储不可用无法在这里被用户区分
- 这是既有通用客户端与新增接口契约的集成缺口, 不是声称 client.ts 本身由 Issue #10 新增
- 建议: ApiError 保留稳定原因码, UI 根据码显示文案并提供未知码兜底; 不改变现有 error 字段兼容性。用真实拦截器测试 409 + reason, 不只 mock backendApi 返回 rejected 对象
- 证据级别: 跨端静态路径确认, 本轮未运行浏览器复现

### E3 / P2: 关键持久写入的 False 返回被忽略

- 位置: `satrap/core/pipeline/scheduler.py:149-161`, `satrap/core/platform/onebot/adapter.py:900-927`
- `_update_manual_request` 不检查 `update_request` 返回值; `_finalize_attempt` 不检查 `complete_send_attempt` 返回值
- store 的 False 不仅表示写异常, 还包含降级、记录不存在、不允许的状态迁移等情况; 部分 False 分支本身没有日志
- 故障注入让两个 recorder 方法返回 False, 两个上层方法均正常结束, 捕获到的 logger 调用都是空列表
- 影响: 持久状态可能停在 accepted/executing/submitted, 上层没有报告本次收尾未落盘。不能说所有落盘失败都静默: store 的 OSError 分支已有 error 日志, 缺口是无日志的 False 分支以及统一的调用关联
- 另: scheduler 捕获异常只记 debug; 文件锁超时这类存储失败在不输出 debug 的配置下缺少该层可见告警
- 建议: 检查 bool; 将需要调查的未生效结果记 warning/error 并带 adapter/request_id/turn_id/status, 保留不重发、不覆盖真实网络结果的策略。若某些拒绝是合法幂等 no-op, 用少量稳定原因码区分, 避免一律报错或新增庞大结果框架
- 验收: False、异常、合法 no-op 分开断言; 不把“业务已发送”和“状态已持久化”混成同一个成功标志

### E4 / P2: 手动唤醒存储锁异常绕过业务错误归一

- 位置: `manual_wake_store.py:791`, `BackendManager.py:532-539`, `utils/minihttp.py:402-405`
- `accept_request` 的文件事务上下文在内部保存 try 之外进入; `FileLock.__enter__` 可抛 TimeoutError, 也可因打开锁文件失败抛 OSError
- BackendManager 仅捕获 ManualWakeStoreError, 未接住这些进入事务阶段的异常
- 在真实测试运行时中将事务入口注入 TimeoutError, 直接调用 POST `/api/platforms/wake` 路由, 得到未处理的 TimeoutError; 外层 MiniHTTPServer 会记录通用 HTTP 错误并返回 500/internal server error
- 影响: 并非完全无日志, 而是已知存储不可用场景丢失稳定的 rejected/reason/request_id, 且 HTTP 日志缺少这次唤醒关联上下文
- 建议: 在明确存储边界将锁等待/锁文件 I/O 转成已有类型化存储失败, API 保留稳定拒绝原因; 如果要把 store_unavailable 改成 503, 需同步明确状态码契约, 当前 POST 约定确实是 400/409, 不能把 503 当成既有要求
- 验收: 事务进入失败与保存失败均有结构化响应、关联日志, 且没有入队/网络副作用

### E5 / P3: 发送转换兜底丢失异常诊断

- 位置: `satrap/core/platform/onebot/adapter.py:929-945`
- `_send_chunk_guarded` 的 Exception 分支仅返回 `failed/message_conversion_failed`, 不记录异常类型与 turn/session 信息
- 回执和后续诊断仍能说明失败类别, 因此不是“错误被完全吞掉”; 但排查具体转换异常只剩一个固定码, 与 `_failed_receipt` 的其他发送失败路径不一致
- 建议: 在该边界补一次脱敏 warning, 或复用 `_failed_receipt`; 保留 PermissionError 的明确原因, 不输出消息链/附件内容

## 三、已确认合理的覆盖与边界

| 链路 | 当前评价 |
| --- | --- |
| 管理动作网络失败 | `_call` 区分 timeout/unconfirmed、平台明确拒绝、动作不支持; 错误日志多数只记类型/retcode, 不回显整份响应; 成功 flag 例外见 E1 |
| 审批终态写入 | RequestFlagRegistry 对 settle=False 有 warning, admin 对 settle 抛异常有 error; 不应重复列为“完全没有覆盖” |
| 普通发送记录 | `_mark_segment_submitted` 和 `_record_segment_result` 同时检查异常与 False, 有 turn/index; 收尾遗漏见 E3 |
| 发送失败回执 | `_failed_receipt` 有 action/session/reason/异常类型; file fallback 保持 unknown 并有限频日志, 不把未验证兼容当送达成功 |
| 清单损坏/隔离 | 共用组件报告降级标记失败、隔离失败; 标记失败时不隔离。两个 store 的初始化/恢复也有日志; 不需要每个纯校验函数再打一次日志 |
| ASR 客户端 | suppress_error=False 向调用方抛出, True 记录脱敏错误并返回 None; SDK 响应解析和输入准备已共用。此契约本身不是漏日志 |
| 唤醒诊断 UI | 查询失败有错误展示和刷新入口, 不是静默空表; 调度器未运行也单独显示 |
| 纯策略/试算校验 | 合法拒绝/无法判定用结果和原因说明, 无须把每次参数拒绝升级成 error 日志 |

日志的主要问题不是总量不够, 而是少数边界不一致: 原始标识泄露、bool 失败未消费、已知存储异常退化为通用错误、前端丢原因。建议先修这四类, 避免在每层追加同一条异常而产生噪声。

## 四、验证与限制

- 独立运行: `python -m pytest tests/unit/test_manual_wake_store.py tests/unit/test_onebot_admin.py tests/unit/test_durability.py tests/unit/test_asr_call.py -q`
- 结果: **146 passed**
- 额外进程内探针: 假客户端成功日志包含原始 flag; 两个 False 返回均无上层日志; 注入文件锁 TimeoutError 穿过真实唤醒路由
- 探针未连接真实 OneBot/模型, 使用临时目录, 没有修改生产实现
- 本轮不是全量 pytest/前端门禁复跑, 不据此宣称所有新增代码或真实平台验收通过; E2/E5 为静态链路结论, 未宣称做过浏览器/生产故障复现

建议顺序: 修 E1-E4, 同时做审批流程与前端错误提取的局部复用; 随后处理 E5 与确定的小清理。保持独立可评审 diff, 不再以引入新的通用框架换取纸面复用率。
