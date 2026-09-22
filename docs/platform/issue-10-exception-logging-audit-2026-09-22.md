# Issue #10 异常传播与日志覆盖审计 (2026-09-22)

范围: `git diff 1ecb13c..HEAD -- satrap/ scripts/` 涉及的 50 个 Python 文件, 按入站/适配器层, 流水线/附件层, 后端/配置/CLI/插件层三组逐函数审读。审计目标只有两项: (A) 未捕获异常是否会让进程, 主循环或长期任务停止; (B) logger 覆盖是否足够 (每个 except 有日志, 级别合适, 无敏感字段, 无刷屏)。本文只记录结论, 未修改代码。

## 0. 结论摘要

**没有发现"一条坏消息把进程搞没"的路径。** 事件级异常在 `PipelineScheduler.execute` → `EventDispatcher.process` → `_adapter_dispatch_loop` → `BackendManager._dispatch_loop` (带退避重启) 四层都有兜底; 所有 `except Exception` 都不会误吞 `asyncio.CancelledError` (Py3.8+ 为 BaseException); 线程池 (`BoundedAsyncWorker`) 的异常沿 await 路径正确回传。

真正会"搞没"东西的是**平台生命周期**而不是消息处理:

| # | 位置 | 后果 | 严重度 |
|---|---|---|---|
| A1 | `BackendManager._init_platforms` 1197-1249 + `PlatformAdapterManager.start_all/stop_all` | 单个平台配置错误 (如 `port: "abc"`, settings 非字典) 在 `OneBotAdapter.__init__`/`validate_wake_policy` 抛出, 无逐实例隔离, `start()` 捕获后 `stop()` 再 `raise` → **整个后端进程退出** | 致命 |
| A2 | `OneBotAdapter.run` 141-153 | `run_task` 抛异常 (端口占用等) 被吞, `record_error` 后协程正常返回, **adapter 永久停摆且无人重启**; `health()` 不看 `_run_task` | 致命 (单平台) |
| A3 | `PlatformAdapter.start` 246 | `_run_task = create_task(run())` 无 done callback; `run()` try 之外的异常 (如 `add_url_rule`) 只在 GC 时打印, 状态仍 RUNNING | 高 |
| A4 | `WakeTimers.schedule.enqueue` 65-76 | 定时器 Task 无 try/except 且 `finally` 丢弃引用, `peek`/`commit_event` 抛错 → 异常永不被取回, 计时静默失败 | 高 |
| A5 | `scheduler.execute` 156-159 + `WakeTimers.schedule` | 到期复查命中 `cooldown` 后无条件重排, `due = 旧 received_at + wait` 已过去, `sleep(0)` 立即再入队 → **零延迟忙循环**直到冷却结束或 ttl 过期 | 高 (资源空转) |
| A6 | `adapter._handle_group_message` 194 / `_emit_notice` 392-407 | 入口无顶层 try, `normalize_group_whitelist` 抛 ValueError 时 aiocqhttp 派发 Task 静默死亡 (aiocqhttp 自身 create_task 无回调) | 高 |
| A7 | `BackendManager._replace_platform_instance` 742-757 | `except BaseException` 内继续 await 回滚, 回滚再抛会覆盖原始异常/取消; 全程无日志, 平台可能停在 detached+terminated | 高 |

日志方面的系统性缺口是**平台状态变化与平台动作失败完全不可见**: `admin.py` 全文件零日志, `outbound.py` 零日志, `group_admin/tools.py` 零日志, 发送回执 `action_rejected/unconfirmed` 不落日志, 连接建立/断开不落日志, 替换/删除实例不落日志, 两处 HTTP 兜底 500 不落日志。排障时会表现为"平台静默失效"或"UI 报错但日志无痕"。

敏感信息: 除 `BackendManager.py:1206` 在跳过无效平台配置时把整个 `pcfg` (含 access_token/secret) 打进 warning, 以及 ASRCall `suppress_error` 路径把 openai `APIError.__str__` (含服务端响应体) 打进 error 之外, 其余路径未见凭据入日志。

## 1. 异常传播 (A 类) 详细清单

### 致命

- **A1** `satrap/core/backend/BackendManager.py:1197-1249`, `satrap/core/platform/__init__.py:726-736`。`_init_platforms` 循环内 `dict(pcfg.get("settings"))`, `add_adapter` → `registry.create` → `OneBotAdapter.__init__` (`int(port)`, `validate_wake_policy`) 均无 try; `registry.create` 只对未注册类型返回 None, 不吞构造异常。`start_all`/`stop_all` 同样逐个 await 无隔离。`ConfigLoader.from_dict` 对 `platforms` 只做 `list()` 不校验。触发: 手改 YAML 或旧版配置。后果: 后端启动失败退出; `stop_all` 中某 adapter `terminate()` 抛出会跳过其余 adapter 与 `platform_events.close()`。建议: 每个平台独立 try/except → `logger.error` + 标记 failed + continue。
- **A2** `satrap/core/platform/onebot/adapter.py:141-153`。`run()` 的 `except Exception` 把服务运行失败转成 `record_error` 后返回, `_run_task` 正常完成。没有监督者: `_dispatch_loop` 只监督事件分发, `health()` 不检查 `_run_task.done()`。建议: `run()` 内有限重连, 或 `start()` 完成回调把状态置 ERROR 并上报。

### 高

- **A3** `satrap/core/platform/__init__.py:246`。建议 `add_done_callback` 取 `task.exception()`, 非 None 时 `record_error`。
- **A4** `satrap/core/pipeline/wake_timers.py:65-76`。建议 `enqueue` 内 `except asyncio.CancelledError: raise` / `except Exception: logger.warning`。
- **A5** `satrap/core/pipeline/scheduler.py:156-159`, `satrap/core/pipeline/wake_timers.py:36-76`, `wake_window.py:159-160`。已核实: `decide` 先判 cooldown 再判 max_wait; 复查事件未触发时 `schedule(event)` 用 `snapshot[0].received_at + wait` 计算 due。`key in self.tasks` 去重只防并发, 不防串行重排。建议: 对携带 `deadline_ticket` 的复查事件不再重排, 或 due 取 `max(due, now + 剩余冷却)`。
- **A6** `satrap/core/platform/onebot/adapter.py:194, 392-407`。建议两个 handler 入口加 `try/except Exception` + `logger.error`。
- **A7** `satrap/core/backend/BackendManager.py:742-757`。建议回滚体独立 try/except 记录, `CancelledError` 记录后原样 raise。

### 中

- `outbound.py:147-151` `OutboundTurns.run` 的 `except CancelledError` 分支: 子任务已 done 时 `raise RuntimeError from None` 丢弃原始异常; 外层被取消且子任务恰好 done 时把取消转成 RuntimeError。
- `adapter.py:519-522` 单块快路径未像分块循环那样隔离 `PermissionError`, 群移出白名单时异常抛到调用方而非回执。
- `onebot_utils.py:191-197` json 段只捕 `JSONDecodeError`, pydantic `ValidationError` 未捕 → 整条入站消息丢弃。
- `input_projection.py:104` `int(data.get("time"))` 对非数字字符串抛 ValueError → 整条消息丢弃且无用户反馈。
- `BackendManager.py:595` → `document.py:371` → `file_lock.py:65`: async 路径内同步 `time.sleep` 自旋等锁最长 30s + `_replace_with_retry` 最长 1s, 锁被 CLI/控制端持有时冻结整个事件循环。
- `BackendManager.py:486-510` `wake_platform`: `convert_message`/`register`/`commit_event` 未包 try, 异常穿透到 minihttp 兜底 500。
- `group_admin/tools.py:232` `future.result(timeout=15)` 超时不 cancel, 写动作可能仍生效却被报失败。
- `attachments.py:121-125` `finally: await client.client.close()` 抛出会覆盖原始转写异常。

### 低

`onebot_utils.py:305` `int(time)`; `onebot_utils.py:348` `os.path.exists` 对 NUL 抛 ValueError; `notices.py:139-141` done callback 不取异常 (BaseException 时 GC 警告); `wake_window.py:170` 阈值作分母无钳制 (当前校验保证 ≥1); `admin.py:382,412,492` `int(duration)` 在 bool 校验之前; `document.py:422-429` 只重试 PermissionError; `main.py:344-384` 无顶层兜底, Windows `add_signal_handler` NotImplementedError 静默; `_MediaBudget.merge` 图片预算耗尽时 `return` 跳过视频检查; `scheduler.py:295` `dict.get` 默认值每消息实例化 Lock; `scheduler.py:142`/`input_projection.py:82` 引用回源可能重复请求。

## 2. 日志覆盖 (B 类) 详细清单

### 缺日志 (中)

| 位置 | 缺口 |
|---|---|
| `onebot/admin.py` 全文件 | 零日志; `_call` 超时/拒绝/未确认转异常时原始信息只剩类型名与 retcode; 所有写动作无审计 |
| `onebot/outbound.py` 全文件 | 零日志; 队列满载, `close()` 取消在途任务均不可见 |
| `expend/plugins/group_admin/tools.py` 全文件 | 零日志; 踢人/禁言/退群执行与权限拒绝不可见 |
| `adapter.py:576-587, 635-642` | 发送回执 `action_rejected`/`action_unconfirmed`/`invalid_session` 全链路 (含 `event._record_send_result`) 无日志 |
| `adapter.py:130-139, 343` | 反向 WS 连接建立/断开, self_id 绑定无日志 |
| `adapter.py:270-273, 318-321` + `input_projection.py:91-94, 137-140` | 引用/转发回源失败 `except Exception: return None` 静默, 连 debug 都没有; `json.dumps(result)` 在 try 之外 |
| `attachments.py:173-174` | `except UnsupportedAdminAction: pass` 三级降级第一级无痕 (本批新增) |
| `BackendManager.py:600-619, 676-684` | reload 平台配置读取/校验/应用失败只写 result 不打日志; 533 行无条件 INFO "配置已重载" |
| `BackendManager.py:689-757` | 替换/删除实例的 start/detach/terminate/attach/回滚全程无 INFO/WARN |
| `BackendManager.py:486-510` | 手动唤醒拒绝/成功均无日志 |
| `http_api.py:150` `log_errors=False` + `minihttp.py:403`; `control_server.py:2205` | 两处兜底 500 无日志; `config_document_revision` 的 `json.dumps` 对 YAML 时间戳抛 TypeError 会落到这里 |
| `scheduler.py:217-218` | `resolve_session` 为空直接 return, 消息静默丢失 |
| `scheduler.py:267-273`, `attachments.py:233-244, 338-340` | 有日志但缺 session/request_id/asr_name 上下文 |
| `manual_wake.py:62-69`, `wake_timers.py:70` | 手动请求过期, 定时器发现 ticket 已取消/快照为空时静默 |
| `cmd_platform.py:157-160` | `remove` 未包 try, `TimeoutError`/`PermissionError` 直接 traceback (upsert 有包) |

### 级别不当 / 刷屏 (低)

- `adapter.py:568, 624` 客户端未初始化期间每次发送 `logger.error`, 高并发回复刷屏。
- `adapter.py:583` 每次转发降级都 info。
- `__init__.py:150-155` (无 handler), `:311` (队列满), `notices.py:137` (inflight 满) 逐事件 warning, 持续过载时刷屏。
- `adapter.py:757-759` `wait_ready` 轮询吞 ClientError 可接受, 但超时时丢失最后一次原因。

### 敏感信息

- `BackendManager.py:1206` `logger.warning(f"跳过无效平台配置: {pcfg}")` 含 access_token/secret (既有代码, 但在本次改动函数内)。
- `ASRCall/async_.py:53,58`, `sync.py:53,58` `suppress_error=True` 时 `logger.error(f"... {e}")`, openai `APIError.__str__` 含完整响应体。api_key 不在其中, 但服务端回显内容可能泄露。附件路径 (`suppress_error=False`, 只记类型名) 是正确做法。
- 其余: 平台配置指纹用 sha256, 控制端统一脱敏, CLI 未见凭据输出, `probe_snowluma.py:758` 脱敏靠原串替换 (URL 编码形式不覆盖, 风险低)。

### 风格

- `model_service.py:16-19` 本批新增 `ASR_TEST_MAX_SECONDS` 时把两条说明字符串顺序写反: `"""测试音频本地转码允许的最长秒数"""` 紧跟 `ASR_TEST_MAX_SECONDS` 是对的, 但原 `"""ASR 转录测试接受的解码后音频字节上限"""` 被挤到其后, 成了悬挂字符串。

## 3. 做得好的地方 (不必动)

- 事件处理四层兜底 + 分发循环退避重启, 单条坏消息不会停消费。
- `_handle_message_event` 预占去重键 + `finally` 回滚 + 兜底日志。
- `_adapter_dispatch_loop` 有界 pending, 按会话保序, 取消清理与 `task_done` 配对完整; `receiver` 用 `gather(return_exceptions=True)` 且取值前判 `cancelled()/exception()`。
- `PlatformEventHub` create_task 有 done callback, `_dispatch` 逐处理器隔离, `close()` gather 收尾。
- `BoundedAsyncWorker` future 承载线程异常, `except BaseException` 释放槽位。
- `_extract_file` 先登记临时文件再写; `event.cleanup_temporary_local_files` 捕 OSError。
- 手动唤醒 `operator` 服务端注入, ID 全部 ascii+isdecimal, 无裸 int()。
- 配置写入 FileLock + 临时文件 fsync + os.replace + Windows 重试 + expected_revision 409。
- 新增 ASR 测试路由 400/502 分流双层兜底。
- 热路径 (每条群消息) 无 info/warning 刷屏, 丢弃/限流走 debug。

## 4. 建议的整改分组 (供决策, 未实施)

1. **生命周期隔离** (A1, A2, A3, A7 + `start_all/stop_all`): 逐平台 try/except, `_run_task` done callback 置 ERROR, `run()` 有限重连或明确报 ERROR 并让 `health()` 反映, 替换实例回滚独立 try + 日志。
2. **定时器与 handler 入口** (A4, A5, A6): `enqueue` 兜底, 复查事件不无条件重排, 两个 aiocqhttp handler 顶层 try。
3. **平台动作与状态日志**: `admin._call` 失败 warning, 发送回执失败 warning, 连接绑定 info, 替换/重载失败 warning, `group_admin` 写动作 info, HTTP 兜底 500 error, `except UnsupportedAdminAction` 降为 debug 日志。
4. **敏感字段**: 1206 行只打 id/type; ASRCall 错误只记 status/type/request_id。
5. **中低项择机**: 事件循环内同步等锁改 `to_thread`, `OutboundTurns.run` 取消语义, 单块快路径回执归一, json 段 ValidationError, `reply.time` 安全转换, `cmd_platform remove` 包 CliError, docstring 顺序。
