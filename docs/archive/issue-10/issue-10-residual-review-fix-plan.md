# Issue #10 残留评审问题修复方案 (2026-09-23)

依据: 2026-09-23 评审意见与逐条代码核验结论 (全部确认存在, 核验过程见会话记录)。目标: 消除合并前必修的安全/契约缺口, 顺手清理低项。分 A (合并前必修) / B (低项择机) 两批, 每批独立可提交。

通用约束 (沿用既有门禁, 并收紧一处口径):
- 不新增 `# type: ignore` / `pyright: ignore`; **pyright 门禁口径从"当批修改文件"改为全库**: 收官时 `python -m pyright -p .pyrightcfg` 全库 0 error, warning 数相对本方案开工基线 (1590) 不增。
- 每个新 except 分支至少一条日志; 日志不带配置正文/响应体。
- 每条整改配单测; 全量 `tests/unit` 绿; `git diff --check`。
- 不引入新的运行时依赖; 不改变已裁定的外部行为 (回执语义, 权限边界) 除非条目显式说明。

---

## 批次 A: 合并前必修

### A1. N1 — bool 配置加载 fail-open (安全)

文件: `satrap/edictum/plugin_config.py:105` (`ConfigField.validate` 宽松分支)

- bool 分支改为显式映射: `isinstance(value, bool)` 原样返回; `value` 为字符串时仅接受 `"true"/"false"/"1"/"0"/"yes"/"no"/"on"/"off"` (小写后比较) 映射; `value` 为 `0/1` 整数时映射; 其余 (含 `"False"` 之外的任意字符串, 非 0/1 数字, 容器) 打 `logger.warning(f"[插件配置] {self.name} 布尔值 {value!r} 无法识别, 回退默认 {self.default!r}")` 并回退默认。
- 与 `validate_strict` (写入口, 已严格) 的分工不变: 宽松路径负责加载/会话覆盖, 严格路径负责管理接口显式写入。
- 影响面评估: 既存配置若曾以非标准形式落盘 (两个官方写入口都走 strict, 只有手改 JSON 可能), 加载时从"误判 True"变为"回退默认 False", 对 group_admin 写开关是 fail-closed, 方向正确。

测试 (`tests/unit/` 新增或并入插件配置测试文件):
- `load_global` 读入 `{"write_tools_enabled": "false"}` 得到 `False`; `"true"` 得到 `True`; `"yesss"` 回退默认且有 warning。
- 端到端: group_admin 写工具在字符串 `"false"` 配置下仍被拒绝 (经 `load_global` → `_resolve` 链路)。

### A2. pyright 全库 47 error 清零

文件: `tests/unit/test_platform_config_runtime.py` (21), `tests/unit/test_group_admin_plugin.py` (12), `tests/unit/test_call_origin.py` (5), `tests/unit/test_manual_wake.py` (5), `tests/unit/test_onebot_forward.py` (2), `tests/benchmark/benchmark_platform_ingress.py` (2)

- 逐文件清零, 手段按项目既定偏好: 标注 / 定型替身工厂 / overload / cast / 辅助函数, 不用 ignore 不用 assert 收窄。多为测试替身的属性访问与未定型参数, 预期以补标注和替身工厂为主。
- 完成后全库 `python -m pyright -p .pyrightcfg` 0 error; 在 `../../platform/issue-10-progress.md` 记录全库口径数字 (errors/warnings), 替代以往"修改文件"口径的表述。
- 顺带核验: main 全库 216 error 与本分支合流后是否回潮 —— 合回 main 时以全库 0 error 为合并门槛之一 (main 侧存量不属于本方案, 但分支合入不得把 47 个带回去)。

### A3. `media_insecure_tls` 收敛为仅登记主机生效

文件: `satrap/core/pipeline/attachments.py:286`, `satrap/core/utils/outbound` (不动)

- `_download` 增加主机判定: 仅当 URL 主机名归一化后命中 `trusted_hosts` 时才允许 `ssl_verify=False`; 公网主机永远校验。实现放在 `_download` 内: `effective_verify = verify_tls or host not in trusted` (host 取自 `urlsplit(url).hostname`, 归一化复用 outbound 的 `normalize_hostname`)。
- 文档同步: `../../platform/platforms.md` 与 `config.example` 中 `media_insecure_tls` 的描述改为"仅对 `media_trusted_hosts` 登记的主机关闭证书校验"。

测试:
- 开启 `media_insecure_tls` 后: 登记主机下载 `ssl_verify=False`, 未登记公网主机下载仍 `ssl_verify=True` (mock `safe_async_get` 断言参数)。

### A4. 附件事件级总耗时预算

文件: `satrap/core/pipeline/attachments.py`

- 新增 `ATTACHMENT_TOTAL_TIMEOUT = 90.0` (秒, 常量带说明字符串); `resolve_attachments` 进入时记 `deadline = monotonic() + ATTACHMENT_TOTAL_TIMEOUT`, 循环每处理下一个附件前检查剩余预算, 不足时将其余附件标记 `AttachmentResult(kind, display, "failed", reason="attachment_budget_exceeded")` 并停止获取。
- 不取消已在飞行中的下载/ASR (单项已有 20s/60s 上限), 只在项间收口; 最坏路径从 ~320s 收敛到 90s + 单项收尾。
- `render_attachments` 对 `attachment_budget_exceeded` 给出明确文案 "附件处理超时, 已跳过"。

测试: monkeypatch 使前两个附件各消耗超预算时长 (fake monotonic / 慢速 download), 断言后续附件为 budget_exceeded 且总耗时受控。

### A5. 事件循环内阻塞两处

- `satrap/core/pipeline/attachments.py:_extract_file`: 临时文件写入移入线程 —— 改为把 `data` 交给 `EXTRACT_WORKERS.run` 内的辅助函数 `_write_and_extract(data, suffix, max_length, max_file_size)`, 在线程里完成 NamedTemporaryFile 写入 + `extract_text`; 临时文件名在线程内生成后需回传登记 —— 让辅助函数返回 `(path, text)`, 事件循环侧先 `event.track_temporary_local_file(path)` 再返回 text; 失败路径由辅助函数内部清理未登记的临时文件。
- `satrap/core/platform/onebot/admin.py:get_record`: `base64.b64decode(encoded, validate=True)` 包 `await asyncio.to_thread(...)`; 长度预检 (字符串长度上限) 保留在循环内 (廉价)。

测试: 现有转写/文件提取回归即覆盖行为等价; 补一例 `_extract_file` 正常路径断言事件登记了临时文件且正文正确 (既有测试已覆盖, 确认不解体即可)。

---

## 批次 B: 低项 (同分支择机, 逐项独立)

### B1. N2 — Python 3.10 的 `except TimeoutError`

- `satrap/core/backend/http_api.py:232`: `except TimeoutError` 改 `except asyncio.TimeoutError` (3.10 捕获 wait_for 超时; 3.11+ 为内置别名, 两侧等价)。
- `satrap/core/backend/control_server.py:1726`: 兜底元组 `(OSError, TypeError, ValueError, TimeoutError)` 中 `TimeoutError` 改 `asyncio.TimeoutError`, 兼容 3.10 的 `concurrent.futures` 系超时。
- `store.py:141` 不改 (捕的是项目 FileLock 抛的内置 TimeoutError)。

测试: 无 3.10 环境, 以代码审查 + 现有 WS 日志流回归测试覆盖; 在文件头注释不动。

### B2. 公网明文 http 下载

- `_download` 增加策略: 公网主机仅允许 `https://`; `http://` 仅对 `media_trusted_hosts` 登记主机放行, 否则抛 `UnsafeOutboundURLError("公网附件地址必须使用 https")`。
- **风险确认点**: 真实 QQ 附件 CDN 若只给 http 公网地址, 此改动会切断直接下载路径 (get_record 路径不受影响)。实施时先用探针/真实消息确认 SnowLuma 上报的附件 URL 形态; 若为 http 公网, 改为默认放行但 debug 记录, 并在文档标注权衡。此项允许在实施时按实测降级为"仅文档说明"。

### B3. `get_record` 透传形态校验

文件: `satrap/core/platform/onebot/admin.py:get_record`

- `source` 增加形态约束: 长度 ≤512, 不含 NUL 与控制字符; 其余照旧透传 (合法值本就是实现自定义的文件 ID 或 URL, 不做路径语义假设)。

测试: 含 NUL/超长入参抛 ValueError。

### B4. 直收音频时长探测

文件: `satrap/core/pipeline/audio_convert.py`, `attachments.py:_fetch_voice`

- `probe_audio` 扩展: wav 用 `wave` 模块读帧率/帧数得时长 (纯标准库); 其他 accepted 格式在 PyAV 可用时用其容器时长, 不可用时依赖 16 MiB 大小上限兜底。
- `_fetch_voice` 的 accepted 分支 (含 get_record 返回 wav) 增加时长检查, 超 `AUDIO_MAX_SECONDS` 抛 `VoiceUnsupported("audio_too_long")`。

测试: 构造长 wav 头断言被拒绝; 短 wav 通过。

### B5. `comp.text` 冻结截断

文件: `attachments.py:323-324` —— `comp.text = text[:TRANSCRIPT_LIMIT]`, 与 results 截断口径一致。

测试: 长转写冻结后 `comp.text` 长度 ≤ TRANSCRIPT_LIMIT。

### B6. 附件/引用解析移到批次认领之后

文件: `satrap/core/pipeline/scheduler.py` (Step.4 → 移入 `_session_turn` 锁内 claim 成功之后)

- 现状: Step.4 (190-193 行) 在进入会话锁与 claim 之前解析引用/转发/附件, claim 失败则白做。调整: 把 `resolve_quotes/resolve_forwards/resolve_attachments/project_input` 移到 `_session_turn` 内、claim 通过之后, 再组装 `user_call`。
- 权衡记录: 会话锁持有时间变长 (以 A4 的 90s 预算为上界), 换取唤醒批次未认领时不浪费下载/转写; 空输入早退检查 (`not message and not images and not videos`) 一并移入锁内。
- `event.set_extra("input_projection", projected)` 同步后移; 移不动的前置依赖 (Step.1-3 的停止/权限/限流) 不变。

测试: 既有 scheduler/quote/attachment 回归; 新增一例 —— 自动唤醒事件 claim 失败 (并发已被认领) 时 adapter 的 `fetch_quoted_message`/`_download` 未被调用。

### B7. 平台上报文件名消毒

文件: `attachments.py:resolve_attachments`

- `display` 统一过 `_safe_display()`: 去除控制字符与换行, 折叠空白, 截断 80 字符; 日志与投影标记只用消毒后的值。

测试: 文件名含 `\n`、`]`、ANSI 转义时日志与渲染块单行且不含原字符。

### B8. N3 — FORWARD_NODE_LIMIT 悬挂 docstring

文件: `onebot_utils.py:31-34` —— 说明字符串归位到各自常量之后 (同批次 5 model_service 修法)。

---

## 验收

1. 每批: 全库 pyright (A2 完成后 0 error, warning 不增); `python -m pytest tests/unit -q` 全绿; `git diff --check`。
2. A3/A4 实施后复跑一次附件相关 benchmark (`tests/benchmark/benchmark_audio_attachments.py`) 确认无回退。
3. B6 实施后进行入站 benchmark 对比 (热路径顺序变化, 允许 ±5%)。
4. 全部完成后更新 `../../platform/issue-10-progress.md`, 并把评审意见逐条标记处置结果 (修复/降级为文档说明/不修及理由)。

## 不做的事 (显式排除)

- main 分支存量 216 个 pyright error 的清零 (属另一议题; 本方案只保证分支自身 0 error)。
- `safe_async_get` 出站层的整体重构 (重定向策略/明文策略) 超出本议题, 仅按 B2 最小收敛。
- 附件并发化处理: 串行是既有裁定 (预算守恒), 本方案只加总预算, 不改并发模型。

---

## 实施状态 (2026-09-23 收官)

全部落地, 两个提交: 批次 A `eb3b712`, 批次 B `8f8f8ae`。

| 项 | 状态 | 说明 |
|---|---|---|
| A1 bool fail-open | 已修 | `ConfigField.validate` 显式映射 + load_global 字符串 "false" 回归测试 |
| A2 pyright 全库 | 已修 | 48 → 0 errors; warnings 1586 (≤ 基线 1590); `get_tools` overload; 测试替身统一 cast/辅助函数, 无 ignore 无 assert 收窄 |
| A3 media_insecure_tls 收敛 | 已修 | 仅登记主机生效; 原全局语义测试改写为新契约 + 直接参数断言 |
| A4 附件总预算 | 已修 | `ATTACHMENT_TOTAL_TIMEOUT=90.0`, 耗尽标记 `attachment_budget_exceeded`, 渲染文案 "附件处理超时, 已跳过" |
| A5 事件循环阻塞 | 已修 | `_write_and_extract` 线程内写+提取, 失败内部清理; base64 解码 `asyncio.to_thread` |
| B1 TimeoutError | 已修 | http_api.py / control_server.py 改 `asyncio.TimeoutError` |
| B2 公网明文 http | 已修 (按用户裁定) | 新增 `media_plaintext_http` 开关默认关闭, 前端平台设置勾选写入配置文件, 已入热更新键; 登记主机不受限 |
| B3 get_record 形态 | 已修 | ≤512 字符且无控制字符 |
| B4 时长探测 | 已修 | `probe_duration`: wav 标准库, 其他 accepted 编码 PyAV 容器/流时长, 不可测时由 16 MiB 上限兜底 |
| B5 comp.text 截断 | 已修 | 冻结值与 results 同按 TRANSCRIPT_LIMIT 截断 |
| B6 认领后解析 | 已修 | Step.4 移入会话锁 claim 成功之后; 新增并发认领失败不做下载/回源的回归测试 |
| B7 文件名消毒 | 已修 | `_safe_display` 去控制字符/折叠空白/截断 80 |
| B8 悬挂 docstring | 已修 | 说明字符串归位 |

评审意见逐条处置: 全部"修复", 无降级与不修项 (B2 的实施形态由用户裁定为前端开关)。

门禁终态: 单测 1862 passed / 19 skipped; pyright 0 errors / 1586 warnings; 前端 tsc 0 + vitest 69 绿; 附件 benchmark get_record +0.5ms (to_thread 预期), 入站 benchmark 全场景优于 after.json 基线。
