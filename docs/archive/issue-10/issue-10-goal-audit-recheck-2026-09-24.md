# Issue #10 修复后独立复审

日期: 2026-09-24

审计提交: `4d8d31491548e536b592fa22e977fc63645c5db4`, 分支 `fix/issue10`; 对照修复前 `e683fce`、目标方案、A1-A10 原审计及 R1-R8 计划复审

## 结论

**仍不通过完整目标验收**。四批修复有实质进展, 输入合并、发送队列终态清理和后端输入预算的原始缺陷已得到修复; 但仍有两个 P1 问题及多项 P2 缺陷/交付缺口。现有测试全绿不能支持“10 项全部闭环”的结论。

本轮仅审计, 未修改业务代码。补充探针使用临时存储、受控平台动作和浏览器 API 替身, 没有读取真实凭据或执行真实 QQ 管理动作。

## 本轮实测

| 检查 | 结果 |
| --- | --- |
| `python -m pytest tests/unit -q --tb=short` | 1999 passed / 7 skipped, 280.08s |
| `npx pyright -p .pyrightcfg --outputjson` | 439 文件, 0 errors / 1532 warnings |
| 前端 `npm test -- --reporter=dot` | 18 文件, 90 passed |
| 前端 `npx tsc --noEmit` | 通过 |
| 前端 `npm run lint` | 通过 |
| 前端 `npm run build` | 通过 |
| `npm run test:e2e:platform` | 通过, 受控接口 |
| `npm run test:e2e:wake` | 通过, 受控接口 |

跳过项为 4 项显式集成测试、1 项缺 reportlab、2 项 Windows 符号链接权限。执行侧记录 1987+19 与本轮 1999+7 总数均为 2006; 本轮以实际输出为准。

## 待修问题

### B1 / P1 / A7: 新增转发读取工具仍可用允许群号读取异群对象

位置: `satrap/core/platform/onebot/admin.py:481`, `satrap/core/platform/onebot/adapter.py:464`

`get_forward_message(group_id, forward_id)` 只验证传入 group_id 在白名单内, 然后将任意 forward_id 交给 `get_forward_msg`。适配器仅验证传入会话范围及可选 self_id, 没有将转发 ID 绑定到可信的目标群消息。

受控反例: 适配器仅允许群 20, 调用 `get_forward_message('20', 'foreign-id')`; 回源替身返回 `group_id=999` 及 `FOREIGN_SECRET`, 工具仍返回该正文。实际调用只有 `get_forward_msg(id='foreign-id')`。这证明 Satrap 未实施对象归属防线, 不表示本轮访问过真实异群数据。

修复方向: 要求提供来源消息 ID, 经 get_msg 核验群归属且确认其中包含该 forward_id; 或使用按账号/群登记的可信入站关联。缺乏归属证据应拒绝。不能将再次检查传入群号当成实际对象核验。

### B2 / P1 / A5: 文件隔离后的再次启动丢失降级状态, 去重可被重置

位置: `satrap/core/pipeline/manual_wake_store.py:182`, `:198`, `:243`

主文件损坏后被改名隔离, degraded 只保存在当前实例内存。下次启动找不到原文件, `_read_payload` 返回空库, degraded 恢复 False, 旧 request_id 可再次 accepted。归档损坏甚至在当前进程就被直接按空归档处理, 同样失去归档中的去重记录。

本轮探针输出: `CORRUPT_REOPEN True False accepted`。第一次实例降级, 第二次实例自动恢复为空库并受理旧 ID。

修复方向: 持久化不可自动清除的降级/恢复标记, 主文件与归档使用一致的保守策略。只有明确恢复或重建流程才能解除, 不能靠重启或隔离文件等同于恢复去重能力。增加“损坏 → 隔离 → 再启动 → 重试旧 ID”和归档损坏用例。

### B3 / P2 / A5: 发送开始后取消, 手动状态被错误归为明确失败

位置: `satrap/core/pipeline/scheduler.py:369`, `satrap/core/platform/onebot/adapter.py:743`

在发送动作已经开始但回执未返回时取消管线, `CancelledError` 不进入普通异常分支, finally 中 `last_send_receipt is None` 导致请求写为 `failed/no_response`; 对应发送尝试仍为 submitted。远端是否执行此时无法确定, 不能宣称明确失败。

本轮实际运行接受事务和 scheduler, 在模拟 send_group_msg 开始后取消任务, 输出:

```text
CANCEL_AFTER_SEND accepted failed no_response
ATTEMPTS_AFTER_CANCEL submitted
```

修复方向: 结合是否已经提交发送尝试裁决取消状态; I/O 前可以明确取消, I/O 后无确认必须 unknown, 有已确认前缀时保留部分结果。请求查询与发送记录应一致, 不等下次启动才修正发送尝试。

### B4 / P2 / A1: flag 淘汰后重复入站会恢复审批资格

位置: `satrap/core/platform/onebot/request_registry.py:47`, `:68`, `:79`

占用过程已同步化, 但容量淘汰与 TTL 清扫无条件删除 executing/completed/unknown。旧 flag 被淘汰后, 再次收到同一事件会重新登记为 available。防重放只在记录尚未被淘汰时成立。

探针将容量缩为 1: old 登记并占用 → 标 unknown → new 挤出 old → old 重投 → occupy 再次成功, 输出 `FLAG_REPLAY_AFTER_EVICT executing`。默认容量 512 只是增加复现所需事件数。

修复方向: 不以普通容量轮转清除未决身份; 采用有界拒绝、保守 tombstone 或明确的不可重新注册窗口。重新登记需有可信的新请求身份依据, 不能用本地重新接收时间刷新同一请求的有效性。

### B5 / P2 / A6: ASR 引用扫描内部吞掉错误, 外层 fail-closed 实际失效

位置: `satrap/core/config/asr_references.py:79`, `:117`; `satrap/core/framework/BackGroundManager.py:630`

管理器捕获扫描异常并拒绝变更, 但插件 JSON 读取失败、SQLite 查询失败、部分元数据解析失败在扫描器内被 warning+continue 吞掉, 返回的空清单被当作“没有引用”。

本轮以损坏的插件配置文件调用真实 ModelConfigService.delete, 输出 `ASR_DELETE_WITH_SCAN_FAILURE True False`: 删除成功, 配置已不存在。测试中的“检查器直接抛错”未覆盖这些内部吞错分支。

修复方向: 区分“该来源确实不存在”与“无法完成扫描”; 后者向上返回扫描不完整/抛错, 禁止删除或重命名。补配置读取失败、数据库锁定/损坏和插件元数据失败用例。

### B6 / P2 / A10: talk_value=0 仍可被最长等待触发

位置: `satrap/core/pipeline/wake_window.py:186`

`max_wait` 提前返回 True, 位于频率映射的零值判断之前。配置 frequency、wake_talk_value=0、wake_max_wait=5, 一条正文等待 6 秒后, 普通判断为“不触发”, deadline 判断却为 `triggered=True, rule=max_wait`。本轮通过真实 dry_run/Window 逻辑复现。

这与映射函数和前端“关闭”的语义冲突。修复应在频率模式的到期路径同样尊重有效零值, 同时保持明确 @、手动唤醒及既定 necessity 语义。另须明确显式阈值优先时的展示, 避免 UI 显示关闭而实际仍使用继承阈值。

### B7 / P2 / A9: 未验证的文件兼容回落仍上报成功

位置: `satrap/core/platform/onebot/adapter.py:920`, `satrap/core/pipeline/scheduler.py:44`; `satrap-ui/src/pages/Sessions/ManualWakeModal.tsx:13`

上传动作不支持时改发旧 file 段, 代码明确承认“不构成文件送达证据”, 却原样保留普通消息 success 状态。后续持久化将 success 映射为 sent, UI 显示“已送达”; reason 中的 fallback_unverified 没有改变成功裁决。

探针令上传返回缺动作、普通消息返回 message_id, 得到 `UNVERIFIED_FILE success fallback_unverified`。

修复方向: 未验证文件投递保留 unknown/独立未确认语义, 或在核实目标实现确实支持该形式后才标成功。普通消息动作确认不能自动证明文件已交付。这里不主张当前 file 段必然失败。

### B8 / P2 / A8: 覆盖编辑器会在编辑中删除已有规则

位置: `satrap-ui/src/pages/Platforms/WakeOverrideEditor.tsx:116`, `:168`; `satrap-ui/src/utils/wakeOverrides.ts:122`, `:150`

每次输入即调用 fromGroupRows/fromTimeRows, 非法或不完整行被过滤。父级 value 改变又触发 effect 从过滤后的值重建 rows。因此清空一个已有群号准备输入新号, 会直接删掉整行及规则内容; 已有时段编辑成暂时无效值也有同一风险。

浏览器补充探针使用已保存的群 20 规则, 输出 `AUDIT_ROWS_BEFORE 1`、`AUDIT_ROWS_AFTER_CLEAR 0`, 确认并非仅保存时忽略空行, 而是输入过程中整行消失。

修复方向: 编辑草稿与已校验保存值分离; 输入过程保留完整行, 保存时显示字段错误并阻止误保存, 不以过滤非法行静默删除原规则。脏状态也应覆盖未完整填写的新行。

### B9 / P2 / A8: 浏览器前进/后退仍绕过脏保护

位置: `satrap-ui/src/hooks/useDirtyGuard.ts:11`

本次实现只有 beforeunload 和 Modal 关闭确认。浏览器站内前进/后退不触发 beforeunload, 可以卸载平台页面并丢弃草稿。源码及进度文档明确承认该边界, 但未有据此缩减原离开保护要求的授权。遮罩点击测试不能代替浏览器历史导航测试。

浏览器补充探针先从会话页进入平台页、修改唤醒词后执行实际后退, 输出路径 `/sessions`, `confirmations=0`, `modalVisible=false`, 已复现无确认离开编辑页。

修复方向: 补站内历史导航拦截或可靠的草稿恢复, 测试实际后退/前进, 而不只测试点击遮罩。

### B10 / P2 / A8: 配置与逐次诊断交付仍有缺口

位置: `satrap-ui/src/pages/Platforms/index.tsx:289`, `satrap/core/pipeline/scheduler.py:293`, `satrap/core/pipeline/wake_rejections.py:1`

- 平台表单没有 input_text_limit/input_media_limit 或平台级 wake_talk_value 控件; talk_value 仅在群/时段编辑器出现, 输入预算又明确禁止群级覆盖, 无法通过现有 OneBot 表单完成这部分配置
- 新记录只采集 wake_decision/rate_limit; 附件/转录失败仍停留于事件 input_projection 的 notes/attachment_status, 未进入可查询记录或前端展示
- 普通自动回复的部分成功/未知虽可能写入发送尝试文件, 本次查询与 UI 主要围绕手动 request_id, 仍缺普通事件的诊断闭环

修复方向: 补平台级配置字段与校验往返; 在补全、模型、发送阶段按 request_id 记录有界脱敏诊断, 提供查询与刷新恢复。测试“普通语音转录失败”和“普通回复部分成功/未知”在页面可见, 不只测试手动请求和限流。

## 原审计项状态

| 原项 | 本轮判断 |
| --- | --- |
| A1 对象归属与审批 | 撤回回源、审批即时并发检查已修; 防重放淘汰边界仍有 B4 |
| A2 输入覆盖 | 原缺陷可关闭: 真实当前投影保留, 合成窗口按实际 claim 组装, 回归通过 |
| A3 取消登记 | 原队列内缺陷可关闭: done 驱动清理, 超时不摘除未终态任务, 回归通过; 不等同于穷尽跨实例替换的所有生命周期竞态 |
| A4 输入预算 | 后端原缺陷可关闭: 顶层媒体实际裁剪、最终文字有界; 配置 UI 缺口列于 B10 |
| A5 持久化 | 已实现主要路径, B2/B3 阻止关闭 |
| A6 ASR 引用 | 常规引用检查已实现, B5 阻止关闭 |
| A7 能力与工具 | 四态及公共发送路径已实现; 新读取工具 B1 阻止关闭 |
| A8 前端 | 编辑器/试算/拒绝记录/手动状态已有交付; B8/B9/B10 阻止关闭 |
| A9 File 分流 | 专用上传与排序已实现; B7 的未确认成功语义待修 |
| A10 talk_value | 已改为配置来源并接入频率判断; B6 零值到期行为待修 |

## 证据边界与后续验收

本轮未重跑真实 ASR/LLM 或 SnowLuma 探针, 沿用执行侧历史记录的日期、版本与覆盖范围, 不将这些记录等同于本轮独立重现。前端构建成功也不等同于干净安装后由后端实际 HTTP 托管的发行验收。

这些证据边界不影响 B1-B10 的不通过结论: 本轮已找到具体缺陷, 并非仅因证据不足拒绝关闭。先修上述问题并增加针对反例的测试, 再对受影响链路定向补验。当前不应据“全量单测通过”宣称完整目标满足。
