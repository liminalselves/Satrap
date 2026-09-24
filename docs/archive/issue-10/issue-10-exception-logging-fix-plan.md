# Issue #10 异常传播与日志覆盖整改方案

依据: `issue-10-exception-logging-audit-2026-09-22.md`。目标是让"平台静默失效"和"UI 报错但日志无痕"两类问题消失, 且不改变已裁定的外部行为 (回执语义, 权限边界, 配置契约)。方案按五批推进, 每批独立可提交, 顺序即优先级。

通用约束:
- 不新增 `# type: ignore` / `pyright: ignore`; pyright 改动文件 0 error 且 warning 数不增。
- 每个新 except 分支至少一条日志; 日志只带 id/type/action/retcode/异常类型, 不带配置字典, 不带响应体。
- 每条整改配单测; 全量 `tests/unit` 绿; 触及入站热路径的项跑 `benchmark_platform_ingress.py` 对比 after.json (允许 ±5%)。
- 本方案不引入新的运行时依赖。

---

## 批次 1: 平台生命周期隔离 (致命 A1/A2, 高 A3/A7)

### 1.1 `_init_platforms` 逐平台隔离
文件: `satrap/core/backend/BackendManager.py:1197-1249`

- 循环体整体包 `try/except Exception as error`, 失败时 `logger.error(f"[BackendManager] 平台 {pid} ({ptype}) 初始化失败: {type(error).__name__}: {error}")`, 记录到新增字段 `self._platform_init_failures: dict[str, str]` 后 `continue`。
- `pid`/`ptype` 提取移到 try 外, 保证日志有标识; 1206 行 warning 改为只打 `id/type` (见批次 4)。
- `health()` 与 `list_platforms` (控制端 GET /platforms) 增加 `init_error` 字段, 让 UI 能看到"配置错误未加载"而不是平台消失。
- 不改变"零平台可用时后端仍启动"的现有行为。

### 1.2 `start_all` / `stop_all` 逐适配器隔离
文件: `satrap/core/platform/__init__.py:726-736`

- `start_all`: 每个 adapter 独立 try; 失败 `adapter.record_error(...)` (自带 ERROR 状态与日志), 继续下一个。
- `stop_all`: 每个 adapter 独立 try; 失败 `logger.warning`, 继续; 结束后不再抛出 (让 `BackendManager.stop()` 的 `platform_events.close()` 必然执行)。
- `stop_all` 保留 `CancelledError` 原样传播: `except asyncio.CancelledError: raise`。

### 1.3 `_run_task` 监督
文件: `satrap/core/platform/__init__.py:246`, `satrap/core/platform/onebot/adapter.py:141-153`

- `PlatformAdapter.start()` 创建任务后 `self._run_task.add_done_callback(self._on_run_task_done)`。回调:
  - `task.cancelled()` → 忽略。
  - `task.exception()` 非空 → `record_error(f"主循环异常退出: {type(exc).__name__}: {exc}")`。
  - 正常返回且 `self._status is RUNNING` (即 run 自己吞了异常后返回, 或服务器循环意外结束) → `record_error("主循环意外结束")`; 这一条覆盖 A2 而无需改 `OneBotAdapter.run` 的吞异常逻辑。
- `OneBotAdapter.run()` 的 `except Exception` 分支保留, 但把 `self._status` 交给回调统一处理; 不做自动重连 (端口占用类错误重连无意义, 由用户从 UI/CLI 重载或重启; 重连属功能扩展不在本方案范围)。
- `BackendManager.health()`: `healthy` 除 dispatch 外, 再要求所有 `enable=True` 的 adapter `status != ERROR`; 新增 `adapters_errored: list[str]`。`get_stats()` 已有 `status`/`last_error`, 不需改。

### 1.4 `_replace_platform_instance` 回滚可观测
文件: `satrap/core/backend/BackendManager.py:689-757`

- 进入时 `logger.info(f"[BackendManager] 替换平台实例: {platform_id} ({'删除' if candidate is None else candidate['type']})")`; 成功时 info。
- `except BaseException as error` 改为两段:
  - 先 `logger.warning(f"[BackendManager] 平台 {platform_id} 替换失败, 回滚旧实例: {type(error).__name__}")`。
  - 回滚体整体包 `try/except Exception as rollback_error: logger.error(... 回滚失败, 平台可能处于停机状态 ...)`, 回滚异常不覆盖原始异常; 回滚结束后 `raise` 原始异常。
  - 原始异常是 `CancelledError` 时同样执行回滚 (取消发生在 shutdown, 旧实例马上也会被停, 回滚代价可接受) 后原样 raise。

### 测试
- `test_admin_control_and_config.py` / 新增 `test_platform_lifecycle.py`: 配置含一条 `port: "abc"` 的平台 + 一条正常平台, `setup_runtime` 后正常平台可用, 失败平台出现在 `init_error`; `start_all` 中第一个 adapter `start` 抛出, 第二个仍启动; `stop_all` 同理且 `platform_events.close()` 被调用。
- `_run_task` done callback: 用 `run()` 立即返回的假 adapter, 断言 `status == ERROR` 且 `health()['healthy'] is False`。
- 替换实例: 让 `replacement.wait_ready` 抛出, 断言旧实例被恢复、日志含"回滚"; 再让回滚中的 `old.start` 也抛出, 断言最终抛出的是原始异常。

---

## 批次 2: 定时器与 handler 入口 (高 A4/A5/A6, 中 outbound 取消语义)

### 2.1 `WakeTimers.enqueue` 兜底
文件: `satrap/core/pipeline/wake_timers.py:65-76`

```
try:
    await asyncio.sleep(...)
    ...
except asyncio.CancelledError:
    raise
except Exception as error:
    logger.warning(f"[WakeTimers] 到期复查提交失败 adapter={adapter.config.id} key={key}: {type(error).__name__}")
finally:
    ...
```
- `ticket.cancelled` 或快照为空时 `logger.debug` 一条 (对应审计低项 13)。

### 2.2 复查事件不再零延迟重排
文件: `satrap/core/pipeline/scheduler.py:156-159`, `satrap/core/pipeline/wake_timers.py:36-76`

- `schedule(event)` 新增关键字参数 `earliest: float | None = None`; `due = max(snapshot[0].received_at + wait, earliest or 0)`。
- 调度器在"到期复查未触发"分支 (`deadline_ticket is not None`) 传 `earliest = now + 剩余冷却`; 剩余冷却由 `WakeWindow` 新增方法 `cooldown_remaining(event, now) -> float` 提供 (读 `_submitted` 与 `wake_cooldown`, 无剩余返回 0)。若 `decision.rule == "cooldown"` 之外的原因 (如 `_automatic_policy_current` 为 False), 则不重排, 直接 return。
- 保证 `due - now >= 0.5` 的下限, 防止任何路径退化为忙循环: `due = max(due, now + 0.5)`。
- `WakeDecision` 已含 `rule`, 无需改结构。

### 2.3 aiocqhttp handler 顶层兜底
文件: `satrap/core/platform/onebot/adapter.py` (`_handle_private_message`, `_handle_group_message`, `_handle_notice`, `_handle_request`)

- 抽一个装饰器 `_guard_handler(name)` 或在 `_register_handlers` 里用包装函数: `except asyncio.CancelledError: raise` / `except Exception as error: self.record_error(...)` 改为只 `logger.error(f"[OneBotAdapter] {name} 处理异常: {type(error).__name__}: {error}")` + `self._ingress_rejections["handler_error"] += 1`, **不**调 `record_error` (单条坏事件不应把 adapter 置 ERROR)。
- `_handle_message_event` 已有兜底, 包装后重复无害。

### 2.4 `OutboundTurns.run` 取消语义
文件: `satrap/core/platform/onebot/outbound.py:147-151`

- 区分两种情况: 外层 `asyncio.current_task().cancelling()` (3.11+) 或子任务未 done → 原样 `raise`; 子任务已 done 且非 cancelled → `raise RuntimeError("发送队列已关闭") from child.exception()` 保留原始异常; 子任务被 close 取消 → 现有 RuntimeError。
- Python 版本下限查 `setup.py` classifiers (3.10), 3.10 无 `cancelling()`: 改用 `self._closed` 标志判断是 close 触发还是外部取消。

### 测试
- `test_pipeline_scheduler.py`: 构造 cooldown 未过的到期复查事件, 断言 `schedule` 第二次注册的 Task 的 sleep 时长 ≥ 剩余冷却 (monkeypatch `asyncio.sleep` 记录参数); 另一分支 `_automatic_policy_current` False 断言不重排。
- `test_wake_timers` (若无则新增): `adapter.commit_event` 抛 RuntimeError, 断言 warning 日志且 `tasks` 已清理, 无 "never retrieved" (用 `pytest.warns`/`caplog` + `gc.collect()` 检查)。
- `test_onebot_ingress.py`: 运行时把 `group_whitelist` 替换为非法值, 调 `_handle_group_message` 不抛且 `_ingress_rejections["handler_error"] == 1`。
- `test_onebot_adapter.py`: outbound 外部取消时抛 `CancelledError` 而非 RuntimeError。

---

## 批次 3: 平台动作与状态日志

原则: 失败 warning, 状态变化 info, 预期降级 debug; 高频路径不加 info。

| 位置 | 改动 |
|---|---|
| `onebot/admin.py:_call` | 三个失败分支各加 `logger.warning(f"[OneBotAdmin] {action} {结果}: retcode={..}/超时/{类型名}")`; `UnsupportedAdminAction` 分支 debug (实现不支持是常态) |
| `onebot/admin.py` 写动作 | 在 `_call` 之后统一: 新增 `_audit(action, group_id, user_id)` 调 `logger.info("[OneBotAdmin] 写动作 ...")`, 只对 `ADMIN_CAPABILITIES` 中 `write` 类调用 |
| `onebot/adapter.py:576-587, 635-642` | `_send_chunk`/`_send_forward` 返回非 success 回执前 `logger.warning(f"[OneBotAdapter] 发送失败 session={..} reason={receipt.reason}")` |
| `onebot/adapter.py:343` | 首次绑定 `bot_self_id` 时 `logger.info("[OneBotAdapter] OneBot 客户端已连接 self_id=...")`; 用 `_bot.on_meta_event` (若存在) 的 `lifecycle.connect`/`disconnect` 补 info, 通过 `_register_optional_handler` 注册以兼容版本 |
| `onebot/adapter.py:270-273, 318-321` | 回源失败 `logger.debug(f"[OneBotAdapter] 引用/转发回源失败 id={..}: {type(error).__name__}")`; `json.dumps` 移入 try |
| `onebot/adapter.py:568, 624` | 客户端未初始化 error 改 warning 并按 adapter 限频: 新增 `_warn_once(key)`, 同 key 60 秒内只打一次 |
| `onebot/adapter.py:583` | 转发降级 info 改为首次 info, 之后 debug (同 `_warn_once`) |
| `onebot/outbound.py:139-140, 170-176` | 队列满 warning (限频), close 取消在途任务 info 带数量 |
| `input_projection.py:91-94, 137-140` | 回源 except 加 debug |
| `attachments.py:173-174` | `except UnsupportedAdminAction: logger.debug("get_record 不受支持, 回退直接下载")` |
| `attachments.py:233-244, 338-340` | warning 补 `asr={asr_name} session={event.session_id}` |
| `scheduler.py:217-218` | `resolve_session` 为空 `logger.warning(f"[PipelineScheduler] 未解析到会话 session={..} provider={..}")` |
| `scheduler.py:267-273` | 兜底 error 补 `session_id`/`request_id` |
| `manual_wake.py:62-69` | 过期/GC 置 cancelled 时 debug |
| `BackendManager.py:600-619, 676-684` | reload 失败分支 `logger.warning(f"[BackendManager] 平台 {pid} 重载失败: {reason}")`; 533 行改为 `有 failed 时 warning "配置已重载, N 个平台失败"` |
| `BackendManager.py:486-510` | `wake_platform` 拒绝 warning (含 reason/operator/adapter_id), 成功 info; `convert_message`/`register`/`commit_event` 包 try 转 rejected |
| `http_api.py:150` | `log_errors=True` (minihttp 已支持) |
| `control_server.py:2205` | 兜底加 `logger.error(f"[ControlServer] 未处理异常 {method} {path}: {type(e).__name__}: {e}")` |
| `group_admin/tools.py` | 引入 logger; `_execute` 权限拒绝 debug, 写动作成功 info, 失败 warning; `future.result(timeout=15)` 超时后 `future.cancel()` 并返回 `status: unconfirmed` |
| `cli/cmd_platform.py:157-160` | `remove` 包 try, `OSError/TimeoutError` 转 `CliError` (与 upsert 对齐) |

### 测试
- 每处用 `caplog` 断言一条日志 (级别 + 关键字), 集中在 `test_onebot_admin.py`, `test_onebot_adapter.py`, `test_attachments.py`, `test_admin_control_and_config.py`, `test_group_admin_tools.py` (若存在), `test_cli_platform.py`。
- 限频: 连续两次未初始化发送只产生一条 warning。
- group_admin 超时: 断言 `future.cancel` 被调且返回 `unconfirmed`。

---

## 批次 4: 敏感字段

| 位置 | 改动 |
|---|---|
| `BackendManager.py:1206` | `logger.warning(f"跳过无效平台配置: id={pid!r} type={ptype!r}")` |
| `ASRCall/async_.py:53,58`, `sync.py:53,58` | `APIError` 分支只记 `status_code`, `type(e).__name__`, `getattr(e, 'request_id', None)`; 通用分支只记类型名 |
| `scripts/probe_snowluma.py:758` | 脱敏增加 `urllib.parse.quote(token)` 与 base64 形式 |

### 测试
- `test_onebot_adapter.py`/`test_admin_control_and_config.py`: 无效平台配置含 `access_token="tok"` 时 caplog 不含 "tok"。
- `test_asr_call.py`: 构造 `APIError` 含 body "secret-body", 断言日志不含。

---

## 批次 5: 中低项择机

| 项 | 改动 |
|---|---|
| 事件循环内同步等锁 (`BackendManager.py:595`, `document.py:371`) | `reload_platform_policies` 与控制端保存路径把 `load_config_document(locked=True)` / `save_config_document` 包进 `asyncio.to_thread`; `FileLock` 不改 |
| `adapter.py:519-522` 单块快路径 | 与分块循环共用 `_send_chunk_guarded`, `PermissionError`/`Exception` 归一为回执 |
| `onebot_utils.py:191-197` | `except (json.JSONDecodeError, ValueError, ValidationError)`, 并先 `isinstance(parsed, dict)` |
| `input_projection.py:104` | `reply.time = _safe_int(data.get("time"), 0)` |
| `onebot_utils.py:305, 348` | `int(time)` 与 `os.path.exists` 加保护 |
| `admin.py:382,412,492` | 类型校验前移到 `int()` 之前 |
| `document.py:422-429` | `_replace_with_retry` 捕 `OSError` 中 errno 为 EACCES/EPERM/EBUSY 的重试 |
| `attachments.py:121-125` | `close()` 独立 try/except debug |
| `notices.py:139-141` | done callback 取 `exception()` 记 error |
| `wake_window.py:170` | `max(1, int(...))` |
| `_MediaBudget.merge` | `return` → 按媒体类型 `continue` |
| `scheduler.py:295` | 先 get 再判空创建 Lock |
| `main.py` / `cmd_run.py:94-98` | 顶层 `except Exception: logger.exception` 后 `sys.exit(1)`; 信号注册失败 debug |
| `model_service.py:16-19` | 两条说明字符串顺序对调 |

### 测试
- 各项一例, 重点: json 段 ValidationError 不丢整条消息; `reply.time` 非数字不丢消息; 单块快路径 `PermissionError` 返回回执。

---

## 验收

1. 每批: 改动文件 pyright 0 error / warning 不增; `python -m pytest tests/unit -q` 全绿; `git diff --check`。
2. 批次 2 后: `benchmark_platform_ingress.py` 对比 after.json, ingress 场景 ±5% 内 (handler 包装与 `_warn_once` 都在非热路径或极低频路径, 预期无变化)。
3. 批次 1+2 后做一次真实 SnowLuma 探针 (`scripts.probe_snowluma`) 确认连接日志与回执日志出现, 且刻意配置一个坏端口平台时后端仍启动、UI 显示 `init_error`。
4. 全部完成后把审计报告的对应条目标记"已修", 更新 `../../platform/issue-10-progress.md`。

## 不做的事 (显式排除)

- `OneBotAdapter.run()` 自动重连: 端口占用等错误重连无意义, 由 done callback 置 ERROR + health 暴露即可; 如需重连另开议题。
- `FileLock` 改异步实现: 用 `to_thread` 包裹足够, 避免改动跨进程锁语义。
- `revision` 对含密钥文档哈希等审计报告"有意保留"项, 维持原判。
