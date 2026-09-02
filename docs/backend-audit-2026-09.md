# Satrap 后端审计报告 (2026-09-01)

> 审计范围: `satrap/` 全部 130 个 Python 文件 (约 5.3 万行), 覆盖 HTTP 服务层、LLM 工具/沙箱层、
> 框架与存储层、API 调用与上下文管线、平台适配层。
> 方法: 五路并行逐行审查 + 逐条人工复核代码确认 (剔除 2 条不成立的报告)。
> 以下所有发现均附已核验的文件与行号。

## 第五轮修复验证 (2026-09-02, RB1-RB6 修复后)

第四轮发现的 RB1-RB6 已全部修复并补充回归测试。

| 发现 | 状态 | 修复要点 |
|------|------|----------|
| RB1 管理删除无租约保护 | 已修复 | 同步删除通过 `remove_if_idle` 原子拒绝活动租约; 异步删除复用会话操作锁等待当前调用完成, 并通过 expected identity 防止误删并发替换条目 |
| RB2 异步路径同步等待创建锁 | 已修复 | 新增异步创建辅助方法, 通过 `asyncio.to_thread` 等待 `_entry_creation_lock`, 事件循环不再被线程锁阻塞 |
| RB3 池容量软超 | 已修复 | 全池忙碌时抛出 `SessionPoolCapacityError` 并回收未入池实例, `max_size` 恢复为硬上限 |
| RB4 整数参数错误逃逸 | 已修复 | 同步和异步 shell/subagent 均统一使用 `_parse_integer_argument`, timeout 限定 `[1, 3600]`, max_turns 限定 `[1, 100]` |
| RB5 未注册工作流漏清 | 已修复 | `clear_memory` 在清理已注册上下文后按 `wf_list` 补清未注册工作流 ID, 同步临时连接显式关闭 |
| RB6 复制期间源数据不一致 | 已修复 | 在源锁内一次性物化轮次及 tool_calls 快照, 释放源锁后再持目标锁写入, 同时保持无反向嵌套锁 |

验证结果: 针对性回归 **99 passed, 1 skipped**; 全量单测 **934 passed, 16 skipped**;
Pyright **0 errors, 0 warnings**; `git diff --check` 无空白错误。

> 独立复核 (2026-09-02): 上述 6 项已经审计方逐行确认属实 — `remove_if_idle` 在池锁下查
> `active_calls/retiring` 原子拒绝、异步删除持操作锁+expected 防误删、`asyncio.to_thread`
> 等待创建锁、`SessionPoolCapacityError` 被 `_create_entry` 捕获并释放新实例、4 处
> `_parse_integer_argument` 全覆盖、clear_memory 双路补清、recorder 源锁内物化快照。
> 审计方复跑全量单测 934 passed / pyright 0/0, 未发现新问题。

## 第四轮复核 (2026-09-02, B 类 bug 修复后)

对第一轮 B 类 bug (中危 9 项 + 低危 17 项) 逐项复核 (含并发脚本实测 + 全量单测 921 项),
结论: **26 项中 25 项已修复, B3 在管理删除路径残留 1 项, 另有 6 项轻微残余/新观察**。

### 逐项状态

| 发现 | 状态 | 修复要点 |
|------|------|----------|
| B1 同步建会话无锁 | 已修复 | `_acquire_or_create_entry` 在 `_entry_creation_lock` 内原子 acquire→create→acquire (`SessionManager.py:2047-2065`); 300 轮双线程并发实测实例数恒为 1 |
| B2 reload 数据竞争 | 已修复 | `reload_model_configs` 取 `entry.sync_operation_lock` + double-check (`:1353-1365`); 实测 reload 阻塞至 run 结束才应用 |
| B3 淘汰/闲置 TOCTOU | **部分修复** | 租约计数 `active_calls` 在 `pool._lock` 下原子增减 (`:484-520`), LRU/idle 清理检查租约 — 实测运行中会话不被淘汰; **但 `remove_session`/`remove_session_async` 无租约保护 (见 RB1)** |
| B4 route_call 全局锁 | 已修复 | 锁只包用户创建/绑定段, `handle_call` 移出锁外 (`UserManager.py:956-967`); 3 用户并发实测并行度 3 |
| B5 分发循环死亡 | 已修复 | 指数退避自动重启 + `health()` 按 `_dispatch_state` 降级时返回 503 (`BackendManager.py:943-968,656-678`) |
| B6 AsyncLLM return_false | 已修复 | `LLMCall.py:1309` `return "" if not self.return_false else False` |
| B7 mem0 任务泄漏 | 已修复 | `_refresh_summary_guarded` + `self._summary_tasks` 强引用 + done 回调清理 (`mem0.py:612-650`) |
| B8 PID 误杀/硬编码端口 | 已修复 | kill 前校验 health 响应的 pid+`runtime_id` (`control_server.py:257-277,383-391`); 优雅关闭读实际配置地址 (`:216-254`) |
| B9 WS 不读客户端帧 | 已修复 | 两服务均在 `_ws_client_monitor` 下运行, FIRST_COMPLETED 取消传播 + finally 退订 (`minihttp.py:525-539`) |
| 低危1-9 | 全部已修复 | off-by-one (`commands.py:70-77` 转 0-based); tool_calls 容错 (`context.py:382-412` 实测); rate=0 短路+桶淘汰 (实测); limit 界 `[1,1000]`; 多模态 list content (`_response_content_text` 实测); AsyncReRank 归一化+状态码检查; clear_memory 走 `_all_contexts`; 同步 run 加 `threading.RLock`; sqlite `_connection()` 显式 close |
| 低危10-17 | 全部已修复 | `_parse_integer_argument`; 子代理改子进程+deadline 终止; docread 全套上限 (32MB/128MB 展开/压缩比 200/500 页); recorder 锁序 (SELECT 物化后释锁); misskey disconnect 清映射; 每适配器独立 worker 队列 (废忙轮询); process_buffer 空匹配防护+测试覆盖; `HTTPStatus(status).phrase` |

回归: 全量单测 **921 passed** (875+46), 0 失败。

### 残余与新观察 (按严重度)

- **RB1 [中低] `remove_session`/`remove_session_async` 无租约保护** (`SessionManager.py:1724-1727,1746-1749`):
  管理端删除进行中的会话仍直接 `pool.remove` + 关闭上下文 → 执行线程拿到已关闭连接 (异常被吞, 用户见空回复)。
  建议删除路径检查 `active_calls` 或先取操作锁。注: `unload_session_async` 在操作锁内移除, 安全。
- RB2 [低] `_entry_creation_lock` 为线程 RLock, `handle_call_async` 在事件循环线程同步阻塞等待 —
  worker 线程持锁慢速创建时事件循环卡顿 (延迟毛刺, 非死锁)。
- RB3 [低] `pool.put` 无候选可淘汰时无条件插入, 全池忙时容量软超 `max_size` (暂时性, 有界)。
- RB4 [低] `int(timeout)` (`tools.py:1199,1467`) 与 `max(max_turns, 1)` (`:1352,1523`) 未用
  `_parse_integer_argument` — 非数字参数 ValueError/TypeError 逃逸至 TCBuilder 兜底, LLM 收到原始错误类型。
- RB5 [低] `clear_memory` 依赖 `_track_workflow_context` 注册, Base 内部未注册路径的工作流上下文仍可能漏清 (边缘)。
- RB6 [低] `recorder.copy_turns_to` 循环内对源 conn 查 tool_calls 未持源锁, 复制期间源并发删除可能读不一致 (竞态非死锁)。
- 观察: 零适配器时 `dispatch_loop` 挂起等外部取消, 可接受兜底; `handle_call_async` 切换段
  `_get_or_create_entry` 无租约, 窗口极窄。

## 第三轮复核 (2026-09-01, R1-R6 修复后)

对第二轮残留 R1-R6 逐项复核 (50+ 对抗用例实机测试 + 相关单测 150 项 + pyright 门禁),
结论: **R1-R6 全部修复或充分缓解, 未发现修复引入的新安全问题**。

| 残留项 | 状态 | 验证 |
|--------|------|------|
| R1 三条免审批越界读 | **已修复** | 中段 `..` (两种斜杠/引号/深层)、`powershell -Command` 引号包装 (单双引号/`-c`/`-NoProfile`/`-ExecutionPolicy` 各旗标位)、根相对 `\` 全部升 HIGH 审批; 另测 NT 设备路径 `\\?\`、8.3 短名、通配符、重定向、PowerShell 变量、下载 cradle (certutil/curl/iwr)、嵌套 `cd` 共 50+ 变体, 均需审批或拦截。`_has_outside_workspace_path` (`tools.py:380-453`) 现对任意含分隔符 token 做 resolve 复核, 并递归解析嵌套 shell |
| R2 bootstrap 发 token | **已修复** | Cookie 改为随机会话 ID (`secrets.token_urlsafe(32)`, 服务端存 SHA-256 摘要, TTL + 上限淘汰), token 不再进入 Cookie; 新增 `/auth/logout` 撤销; bootstrap 增加 `loopback_bootstrap` 开关 + Origin 白名单 (`server_auth.py`) |
| R3 默认可绑 `.satrap` | **已修复** | `_is_workspace_denied` (`display/service.py`) 恒拒绝数据目录, 实测 `.satrap` 及子目录 DENY、正常目录放行; 工作区根本身也不得位于拒绝根内 |
| R4 Misskey token in URL | **已缓解** | 协议限制无法移除, 但 websockets 库日志路由到带 `_SecretRedactionFilter` 的专属 logger (原文+URL 编码两种形态脱敏, `client.py:103-132`)。残余: `adapter.py:179` 主 logger 记录 `{e}`, websockets 异常一般不含 URI, 低危 |
| R5 `-e` 缩写/误报 | **已修复** | `powershell -e` → FORBIDDEN; `-ec` → WRITE (审批); `cmd /c dir`、`cd ..` 不再误判 (实测 FREE) |
| R6 连接数/WS idle | **已修复** | 连接上限 256 (超限 503); `_ws_client_monitor` 主动读客户端帧 + 协议 ping + 300s 空闲回收 + 帧大小/掩码校验 (`minihttp.py:49-55,637-680`) |

**观察项 (非安全问题)**: `test_shell_read_only_and_approval`/`test_async_ask_shell` 在冷启动的两次
批量运行中失败、随后同命令三次全绿 (150 passed), 疑似冷启动时序敏感, 建议留意是否为
flaky test。pyright 对本轮改动模块 0 errors 0 warnings。

**部署提醒**: 修复仍未提交, 且实测运行中 19871/19872 仍为旧代码 (`/config` 无鉴权 200), 重启后生效。

## 第二轮复核 (2026-09-01, 修复后)

22 项安全发现经修复后逐条复核 (含实机绕过测试), 结论: **17 项已修复、3 项部分修复、2 项未修复,
另有 6 项修复引入的残留/新问题**。注意: 修复全部在工作区未提交, 且**运行中的 19871/19872 进程
仍是旧代码** (实测 `/config` 无鉴权返回 200), 重启部署前修复不生效。

### 逐项状态

| 发现 | 状态 | 说明 |
|------|------|------|
| S1 shell 门禁 | **部分修复** | 新逻辑 (`tools.py:1094-1101`): 所有 WRITE/HIGH 一律审批, READ+越界升 HIGH 审批; 但残留 3 条免审批越界**读**通道 (见 R1) |
| S2 沙箱 save 任意写 | 已修复 | `save_to_file` 走 `_safe_join` (`sandbox.py:126-137`), 越界抛错; junction 跟随拒绝 |
| S3 `_safe_join` fail-open | 已修复 | 越界抛 ValueError; 删沙箱根抛 PermissionError; 校验失败中止; 执行有 30s 超时 |
| S4 reg/Remove-Item/EncodedCommand | 已修复 (残留小项) | `reg` 入 HIGH 仅 `reg query` 降级; 旗标顺序不再影响 FORBIDDEN 判定; `-EncodedCommand` FORBIDDEN; 残留 `-e` 两字符缩写 → WRITE 需审批但内容不透明 (R5) |
| S5 文件名穿越 | 已修复 | `_safe_download_filename` (`message.py:30-36`): 服务端生成文件名, 外部 name 仅贡献受限扩展名 |
| S6 服务零鉴权 | 已修复 (有残留) | `server_auth.py` 统一 token 鉴权; 非回环绑定强制显式 token (fail-closed); `hmac.compare_digest`; CORS `*` 已移除改白名单; 残留 R2/R3 |
| S7 凭据明文 | 已修复 | `GET /config` 经 `redact_config_document` 无条件递归脱敏; 模型接口恒定 `mask_api_key=True`, 不再依赖 `lock_api_key` |
| S8 WS 无 Origin 校验 | 已修复 | 握手先 Origin 白名单 (403) 再凭据 (401); token 不走 WS URL query |
| S9 class_path/skills | 已修复 | `_load_class` 校验模块来源须在包根/扫描目录且 `cls.__module__` 一致; skills `exec_module` 仅限 preset 与 `trusted_code_roots` |
| S10 restore SQLi | 已修复 | `_RESTORABLE_TABLES` 白名单 + `PRAGMA table_info` 实际列校验 (`database.py:51-93`) |
| S11 restore 路径穿越 | 已修复 | 共享 `_resolve_archive_path` (`maintenance.py:91-119`), restore/purge 一致, 拒绝分隔符与 symlink |
| S12 请求无上限 | **部分修复** | 头 64KB/体 16MB/头 10s/体 30s 已加; 但无并发连接数上限, WS 无 idle 超时 (R6) |
| S13 目录浏览/项目绑定 | **部分修复** | `_resolve_workspace_directory` 越界拒绝; 但默认 `workspace_roots: ["."]` 仍可绑定含凭据的 `.satrap` (R3) |
| S14 SSRF/URL 编码 | 已修复 | `outbound.py` 校验 scheme+解析 IP 须 `is_global`, 重定向逐跳复核; query 改 params 传参; 残留 DNS rebinding 固有 TOCTOU |
| S15 Misskey token/TLS | 部分修复 | insecure download 限定同源; 但 **WS token 仍在 URL query** (`client.py:90-98`, Misskey 协议限制, 见 R4) |
| S16 auto-agent 自批 | 已修复 | `_verdict_to_decision` 收窄 (`permission.py:393-407`): 模型无法产生 ALLOW, 一律 ASK 转人工 |
| S17 grep symlink | 已修复 | `_resolve_grep_file` resolve+`is_relative_to` (`tools.py:353-370`) |
| S18 插件名穿越 | 已修复 | `_PLUGIN_NAME_RE` 白名单 + 父目录复核 (`plugin_config.py:33,140-156`) |
| 低危 TCBuilder `str(e)` | 已修复 | 只回传异常类型名, 详情进日志 |
| 低危 ReRank key | 已修复 | 公开 `api_key` 锁定时置占位符, 请求走 `_api_key`; `_api_key` 仍是进程内明文属性 (可接受) |
| 低危 base_url scheme | 已修复 | 校验 http/https, 非回环 http 需显式 `allow_insecure` |
| 低危 500 带 `str(e)` | 已修复 | 固定 `internal server error` |

### 残留与新问题 (按严重度)

**R1 [中] S1 残留: 3 条免审批越界读通道** (`satrap_coding/tools.py:380-434`)
均经真实子进程确认可在零审批下读取工作区外文件:
- 中段 `..`: `type a\..\..\secret.txt` — `_has_outside_workspace_path:424` 只查 `token==".."` 或以 `../`/`..\` 开头, 中段漏检
- 引号包装: `powershell -Command "type C:\Windows\win.ini"` — `shlex.split(posix=False)` 把整串 `"type C:\..."` 当一个 token, 剥引号后以 `type` 开头不匹配任何路径分支
- 根相对单反斜杠: `type \Windows\win.ini` — `:414,422` 只认盘符/UNC/`/` 开头

**R2 [中] `/auth/session` bootstrap 发放等于 token 的会话 Cookie** (`minihttp.py:279-294`,
`server_auth.py:173-175`): loopback 绑定 + 回环 peer + 任意白名单 Origin 即 `Set-Cookie: satrap_session=<API token 本身>`。
本机进程本就可读 `.satrap/api-token` (Windows 下 0o600 不生效), 实际权限增量有限, 但:
建议会话 Cookie 改为可撤销的随机会话 ID (服务端映射), 而非 token 本体; 并文档化该信任假设。

**R3 [中] 默认 `workspace_roots: ["."]`** (`BackendManager.py:71,134`): 默认即可 `POST /api/projects`
把 `.satrap` (含 api-token/config.yaml/模型 key) 绑为工作区, 会话工具随后可读凭据文件。
建议默认排除 `.satrap` 目录 (类似 `_protection_reason` 的保护名单)。

**R4 [低中] Misskey WS token 仍在 URL query** (`misskey/client.py:90-98`): streaming 官方认证方式,
instance_url 为运维配置; 注意异常日志可能记录完整 URL。

**R5 [低] `powershell -e <b64>` 两字符缩写** 不归 FORBIDDEN (需审批但审批者只见 base64);
`cmd /c ...`、`cd ..` 因 `/c`、`..` 误判 HIGH → 误报 (fail-safe 方向, 影响体验)。

**R6 [低] 无并发连接数上限; WS 无 idle 超时** (`http_api.py:199`): 持 token 者可挂大量连接;
单连接 HTTP 最多挂 40s 已被超时兜住。

另: 静态文件与 `/ui-config.json` 在鉴权检查前服务 (`minihttp.py:296,300`), 泄露面为前端构建产物,
已扫描无硬编码密钥, 低危。

---
---

## 第一轮审计 (2026-09-01, 原始发现)

> 以下为修复前的原始记录。**当时所有问题均未修复。**

## 一、安全问题

### 高危 (建议优先修复)

#### S1. Shell 审批门禁形同虚设: 写/高危命令仅"检出越界路径"才审批

**位置**: `satrap/expend/plugins/satrap_coding/tools.py:997-1015`, 检测函数 `:326-351`

```python
elif risk > RiskLevel.READ:
    if _has_outside_workspace_path(command, _tool_root(self)):   # 唯一审批入口
        ..._approve_sync(...)
    elif self.engine.plan_mode:
        return "拒绝: ..."
# 其余情况: 直接执行
```

与 `command_gate.py` 头注释 "WRITE(1) 常规写, 询问" 的设计相反。检测正则 `_OUTSIDE_PATH_RE`
只识别 `C:\` 反斜杠、UNC、`/x/` 三种形式, 因此以下全部**免审批直接执行**:

- 正斜杠绝对路径: `Remove-Item -Path C:/Users/x -Force` (PowerShell/cmd 均接受)
- 相对路径逃逸: `del ..\..\Windows\...`
- 无路径的高危命令: `rm -rf node_modules`、`git push` (HIGH 级但无绝对路径)
- READ 类命令引用任意路径根本不进审批分支: `type C:\Users\...\.ssh\id_rsa` → 任意文件零审批读取

**修复**: 所有 `risk > READ` 一律走 PermissionEngine, 路径检测只用于风险升级不作放行开关;
检测失败应 fail-closed。

#### S2. `code_sandbox save` 任意文件写

**位置**: `satrap/core/utils/sandbox.py:116-127`

`save_to_file` 是沙箱中唯一不过 `_safe_join` 的方法:

```python
abs_path = os.path.join(self.sandbox_path, path)   # 无任何校验
os.makedirs(os.path.dirname(abs_path), exist_ok=True)
open(abs_path, 'w', ...)
```

LLM 可传 `../../...` 或绝对路径 (`C:/...`) 写出沙箱, 且该工具 (`sandbox_tools.py:87-91`) 无审批。
**修复**: 使用 `_safe_join` 并改为越界拒绝; 沙箱写/删操作接入审批或强校验。

#### S3. `_safe_join` 越界 fail-open: 回落沙箱根后 `delete_directory` 清空整个沙箱

**位置**: `satrap/core/utils/sandbox.py:40-41`、`:143-162`

```python
if not real_target.startswith(real_sandbox + os.sep) and real_target != real_sandbox:
    return self.sandbox_path   # 越界 → 回落沙箱根
...
if recursive: shutil.rmtree(abs_path)   # rmtree(沙箱根)!
```

`code_sandbox operation=delete_dir path=..` → `_safe_join` 返回沙箱根 → 存在性/类型检查通过
→ **删光全部沙箱文件**。且 `delete_file`/`delete_directory` 校验失败只 `logger.error` 不中止。
**修复**: 越界一律抛错拒绝; 校验失败应 return 而非继续。

#### S4. `reg` 误分类为只读命令 → 免审批写注册表

**位置**: `satrap/expend/plugins/satrap_coding/core/command_gate.py:31`、`:219-222`

`"reg"` 在 `_READ_COMMANDS` 中, 注释写着 "reg query 只读, 其他高危 (细粒度见下)",
但细粒度逻辑从未实现 —— READ 分支只查重定向符。后果:

- `reg add HKCU\...\Run ...` (开机自启持久化) 零审批执行
- READ 级连 plan mode 都不拦

同类问题:

- `Remove-Item -Path C:\x` (flag 在路径前) 不命中 `_FORBIDDEN_PATTERNS` (`:69` 只匹配 flag 在前)
- `powershell -EncodedCommand <b64>` 让审批弹窗和日志只见 base64, 门禁与人都看不到真实载荷

**修复**: `reg` 移入 HIGH, 仅 `reg query` 细粒度降级; 递归解析 `cmd /c`、`powershell -Command`;
禁止 `-EncodedCommand` 或审批前解码展示。

#### S5. QQ 文件消息文件名路径穿越 → 任意文件写入

**位置**: `satrap/core/components/message.py:920-929`, 触发链 `onebot_utils.py:167-175` → `:215-216`

```python
stem, suffix = os.path.splitext(self.name)   # name 来自 QQ 文件段, 攻击者可控
filename = f"fileseg_{stem}_{uuid...}{suffix}"
... os.path.join(get_satrap_temp_path(), filename)
```

`name` 未消毒, Windows 下 `..\..\x\evil.txt` 的 stem 保留反斜杠, 拼入路径后逃逸临时目录。
bot 转发/回显文件 (`File.to_dict()` → `get_file()` → `_download_file()`) 时自动触发,
文件内容为攻击者 URL 的下载数据。

**修复**: `stem` 用 `os.path.basename` 白名单化 + 最终路径 `resolve().is_relative_to()` 校验;
过滤路径分隔符与控制字符。

#### S6. 三个 HTTP 服务 (19870/19871/19872) 全部零鉴权 + CORS `*`

**位置**: `satrap/core/utils/minihttp.py:25-30`; `http_api.py:497-499`; `control_server.py:986-1036`

三个服务的全部路由无任何身份校验 (全仓无一处校验 Authorization/token), CORS 均为
`Access-Control-Allow-Origin: *`。暴露面包括:

- `POST /api/shutdown`、`POST /shutdown` (直接 `os._exit(0)`)、`/stop`、`/restart`
  —— 任意本地进程或**恶意网页**即可打停服务
- `DELETE /api/sessions/{id}`、`POST /api/checkpoint/rollback`、存储清理等全部管理操作
- 默认绑 127.0.0.1, 但 `api_host`/`--host` 可配 0.0.0.0 (`ui_config.py:44` 专门处理该场景,
  说明是预期用法), 绑定后即网络级未鉴权管理面

**修复**: 引入 token 鉴权中间件 (CLI 经锁文件/配置共享 token); CORS 改白名单;
绑定非回环地址时强制要求鉴权。

#### S7. 凭据明文泄露: `GET /config` + 模型配置脱敏可绕过

**位置**: `control_server.py:1038-1049`; `BackGroundManager.py:159-172`
经 `model_service.py:47-50`、`display/service.py:358`

- `GET /config` 原样返回配置文档 (含 Misskey/OneBot 平台 token)
- `_mask_payload` 只在 `lock_api_key=True` 时脱敏 —— `lock_api_key=False` 的配置即使走
  `mask_api_key=True` 的列表接口也返回明文 API key, 而该接口无鉴权

**修复**: `mask_api_key=True` 时无条件脱敏; `GET /config` 脱敏 + 鉴权。

### 中危

| # | 位置 | 问题 |
|---|------|------|
| S8 | `minihttp.py:201-235` | WebSocket 握手只查 `Sec-WebSocket-Key`, 无 Origin 校验 → 任意网页可 `new WebSocket("ws://127.0.0.1:19870/ws/logs")` 跨站劫持, 读取实时日志与 `/ws/chat` 全部对话内容 |
| S9 | `SessionClassManager.py:150-168` | `class_path` 动态 `importlib.import_module` (管理 API 可提交任意 `module.Class`) → 绑定非回环时 RCE; `skills.py:226-230` 对技能目录任意 `tools.py` `exec_module`, 信任边界未在文档声明 |
| S10 | `database.py:145-154` | `restore_session_domain` 把 records.json 的**列名**直接拼进 `INSERT INTO {table} ({columns})` (值已参数化, 标识符未) → 归档文件可控时 SQL 注入; 应经 `PRAGMA table_info` 白名单 |
| S11 | `maintenance.py:510-541` vs `:560-563` | `restore_archive` 的 `archive_id` 无 resolve/父目录校验 (同文件 `purge_archive` 有, 是正确范本) → 路径穿越可读任意目录 manifest、移动目录, 成功后 `shutil.rmtree(archive)` 构成删除原语 |
| S12 | `minihttp.py:104,127-132`; `control_server.py:317` | 请求头 `readuntil` 与 `Content-Length` 均无上限、无超时 → 内存/连接耗尽 DoS (slowloris) |
| S13 | `display/service.py:1820-1870`、`:1725-1744` | `browse_directories` 无鉴权枚举任意目录 (返回绝对路径); `create_project` 注释明示"任意绝对路径"可绑为工作区 |
| S14 | `search.py:262`、`:108,185` | `fetch_page` 任意 URL 无 SSRF 防护 (内网/169.254.169.254 元数据); 搜索 query 未 URL 编码拼进 URL |
| S15 | `misskey/client.py:80-81`、`:692-698` | WebSocket URL query 直接带 access_token (进代理/异常日志); `allow_insecure_downloads` 开启后对任意 URL 关闭 TLS 校验 |
| S16 | `permission.py:290-331` | auto-agent 模式用会话主模型自审自批 —— 提示注入 (在 LLM 读取的资料里写 `answer: allow`) 即可放行全部高危操作, LLM-as-guard 不构成安全边界 |
| S17 | `satrap_coding/tools.py:877-881` | `grep_files` 遍历不 resolve (同文件 `GlobFilesTool:834` 有 `resolve().is_relative_to` 防护) → 经工作区内 symlink/junction 读外部文件入模型上下文 |
| S18 | `plugin_config.py:136-137` + `simple_session.py:1067-1077` | 插件名未消毒拼配置路径 (`dir / f"{name}.json"`), `../` 可越界读写任意 .json; 实际影响低 (恶意插件本可执行代码), 但修复成本低 |

### 低危

- `TCBuilder.py:582`: 工具异常 `str(e)` 原样回传 LLM 并持久化 —— 内部路径/连接串泄给模型供应商,
  兼作注入放大面。建议只回传错误类型, 细节进日志。
- `ReRankCall.py:120,291`: ReRank 的 `lock_api_key` 只在 `get_api_key()` 做样子, `self.api_key`
  恒为明文 (LLM/Embedding 的实现是 `__init__` 即替换占位符)。
- `core/utils/__init__.py:81-101`: `normalize_openai_base_url` 不校验 scheme, `http://` 配置导致
  API key 明文传输。
- `minihttp.py:141`: 500 响应体直接带 `str(e)`, 内部错误细节外泄。

---

## 二、可能的 Bug

### 中危 (并发与状态一致性)

#### B1. 同步 `handle_call` 创建会话无锁, 并发重复创建

**位置**: `SessionManager.py:1134-1136` (异步路径 `:1174-1177` 有 `self._async_lock` 保护)

两个线程同时首达同一 session_id 会各自 `_create_entry`, 后 `pool.put` 静默替换,
先建实例的连接/资源无回收而泄漏。**修复**: 同步路径引入创建锁, `pool.put` 检测到替换时释放旧实例。

#### B2. `reload_model_configs` 无锁改写运行中会话

**位置**: `SessionManager.py:1253-1294`

遍历池直接 `session.reload_llm(new_llm)`, 不取 entry 操作锁, 与正在执行的
`handle_call`/`handle_call_async` 数据竞争。**修复**: 逐 entry 取操作锁再改写。

#### B3. LRU 淘汰/闲置清理与执行中会话 TOCTOU

**位置**: `SessionManager.py:1601-1602`、`:2178`

淘汰后 `_release_session_memory` 关闭会话上下文连接, 不持该 entry 的操作锁。
`handle_call` 在 `:1149` 的"是否仍在池中"检查之后、执行完成之前被淘汰, 就会用到已关闭的连接。
**修复**: 淘汰前尝试获取该 entry 操作锁 (失败则跳过), 或延迟释放。

#### B4. `route_call` 持全局锁跑完整 LLM 会话

**位置**: `UserManager.py:955-968`

`with self._lock:` 包住 `self.sm.handle_call(...)` (一次完整同步 LLM 调用)。
一次长调用期间**所有用户**的消息路由全部阻塞; 会话级互斥本已由 entry 锁保证。
**修复**: 只对用户创建/绑定部分加锁, 会话执行移出全局锁。

#### B5. 事件分发循环异常后静默死亡, 健康检查仍报正常

**位置**: `BackendManager.py:916-924`

`_dispatch_loop` 捕获异常只记日志不重启, 此后事件不再分发, 但 `/api/health` 仍按
`self._running=True` 返回 200。**修复**: 带退避重启循环, 或置失败状态并暴露。

#### B6. `AsyncLLM.chat` 的 APIError 分支忽略 `return_false`

**位置**: `LLMCall.py:1267-1273`

恒返回 `""` (对照 `:1281` 通用异常分支有 `"" if not self.return_false else False`,
同步版也遵循)。配置 `return_false=True` 时行为不一致。

#### B7. mem0 后台摘要任务无引用 + 异常未捕获

**位置**: `mem0.py:151`

`asyncio.create_task(self._refresh_summary(...))` 不保存引用 (可能被 GC 中途回收),
协程内 LLM 调用无 try/except → "Task exception was never retrieved", 摘要静默不刷新。

#### B8. `_cleanup_backend` 按 PID 文件杀进程不验身份

**位置**: `control_server.py:234-243`

PID 复用时误杀无关进程; 且 `:214` 硬编码 `http://127.0.0.1:19870/api/shutdown`,
端口可配时优雅关闭路径失效。

#### B9. WebSocket 处理器从不读客户端帧

**位置**: `http_api.py:199-216`、`display/server.py:124-138`

断连检测依赖 `reader.at_eof()`, TCP 半开 (断网/杀进程) 时永不触发, 订阅队列与协程泄漏;
叠加无鉴权可被批量建立连接放大。**修复**: 主动读帧检测关闭/空闲超时; 订阅总数上限。

### 低危

| 位置 | 问题 |
|------|------|
| `goal_state.py:202` + `commands.py:70-75` | `/goal todo-done 1` 实际完成第 2 条子任务 (命令层 1-based 直传 0-based 实现), off-by-one |
| `context.py:577-589` | `load_context` 对 `tool_calls` 列 `json.loads` 无容错, 单行损坏触发外层 except → **整个会话历史被静默清空** |
| `rate_limiter.py:60`、`:30` | `rate=0` 时除零; 桶表只增无淘汰, 大量伪造 key 可致内存增长 |
| `http_api.py:936` | `/api/users?limit=-1`: limit 无上下界, SQLite `LIMIT -1` 等价全表 |
| `LLMCall.py:136,195` | 供应商返回 list 型多模态 content 时 `.strip()` 抛 AttributeError, 被吞后有效回复丢弃 |
| `ReRankCall.py:292`、`:171` | AsyncReRank 的 base_url 未归一化 (与同步版不一致); `response.json()` 前无状态码检查, 401/429 被当空结果静默吞掉 |
| `Base.py:1035-1046` | `clear_memory` 遍历 `wf_list` 而非 `_workflow_contexts`, 且新建 ContextManager 而非用已注册实例, 可能漏清 |
| `simple_session.py:674` vs `:1552` | 异步 run 有 `_run_lock` 串行化, 同步 run 无任何锁, 多线程并发竞争共享上下文 |
| `SessionManager.py:182-185` 等 | `with self._connect() as conn:` 只隐式 commit **不关闭连接**, 依赖 GC 回收 (StateStore 的显式 close 是正确范本) |
| `satrap_coding/tools.py:501,756` 等 | 工具参数 `int(offset)`/`int(index)` 无 ValueError 保护 |
| `sandbox.py:57-64`、`agent.py:122-129` | 沙箱 `subprocess.run` 与子代理 `future.result()` 均无超时, 死循环代码永久挂起 agent 循环 |
| `docread.py` | read_document 解析无文件大小/页数上限, max_length 只截输出不限解析 → 大文件内存 DoS |
| `recorder.py:925-982` | `copy_turns_to` 持 `self._lock` 中再取 `target._lock`, 两会话互相 fork 时锁序反置可死锁 |
| `misskey/client.py:98-105` | `disconnect()` 不清 `channels`/`desired_channels`, 重连累积残留映射 |
| `platform/__init__.py:676-683` | 事件分发 10ms 固定忙轮询 (CPU 空转) + 单事件慢处理阻塞全部平台队列 |
| `event.py:908-915` | `process_buffer` 遇可匹配空串的正则死循环 (当前全仓无调用方, 属隐患; 建议加 `match.end()==match.start()` 防护) |
| `minihttp.py:175` | `status_text` 非 200 一律 "Error" (201/204 也是), trivial |

---

## 三、可通用化的设计

### D1. 同步/异步全量双写 (最大的重复源, 估占数千行)

- `LLMCall.py` (LLM 634-1158 vs AsyncLLM 1160-1699)、`EmbedCall.py`、`ReRankCall.py` 三族:
  参数合并、thinking 处理、错误三分支逐字重复
- `TCBuilder.py`: `ToolsManager`/`AsyncToolsManager` 约 300 行逐字复制
- `context.py`: `ContextManager`/`AsyncContextManager` 约 1300 行成对重复 (预算派生、摘要链、校准)
- `satrap_coding/tools.py` 11 对工具、`base_take/tools.py`: 每对同步/异步孪生,
  已出现逻辑漂移 (如 B6、AsyncReRank base_url、SearchReplace 计数不一致)

**建议**: 抽公共基类承载纯计算与错误归一化, 执行层只留 client 调用与 `await` 差异;
或"单异步核心 + 同步适配器"。

### D2. 路径越界校验 3 套且语义不一致

- `satrap_coding/tools.py:306 _resolve_path` (resolve+拒绝)
- `base_take/tools.py:45 _resolve_doc_path` (多根+uploads 回落)
- `sandbox.py:26 _safe_join` (**fail-open 回落根**, S3 的祸根)

**建议**: `core/utils/paths.py` 提供统一 `resolve_within(root, path, allowed_roots)`,
全仓替换并统一"越界即拒绝"。

### D3. SQLite Store 样板 4 处

`SessionConfigStore`、`UserInfoStore`、`StateStore`、`LiteVectorDB` 均为
`connect + row_factory + PRAGMA 列迁移 + CREATE TABLE IF NOT EXISTS`。
**建议**: `SQLiteStore` 基类 (含显式 close 的事务上下文)。

### D4. HTTP 基础设施重复

`control_server.py:296-357,768-775` 完全绕过 `MiniHTTPServer` 手写请求解析/CORS/响应;
JSON body 解析 3 套; `session_refs` 校验在 `http_api.py` 重复 4 次;
edictum/storage 路由在两个服务各写一套。
**建议**: 控制服务复用基类, 只保留路由差异; 基类提供统一 `_read_json_body(size_limit)` 与 `_ok/_err`。

### D5. 平台层重复

OneBot 与 Misskey 的会话 ID 编解码 (`onebot_utils.py:25-104` vs `misskey_utils.py:88-163`)
几乎逐行重复; 重连退避两套; HTTP 客户端四种混用 (requests/aiohttp/httpx/urllib), 下载逻辑两处。
**建议**: 统一 `parse_session_id/format_session_id`、`ReconnectPolicy`、
单一异步 HTTP 封装 (超时/重试/大小限制/TLS 策略)。

### D6. 其他重复

- `_short_uid` 3 处: `SessionManager.py:52`、`session_instance_service.py:28`、`session_commands.py:17`
- `LiteVectorRAG`/`DataBaseRAG` (`rag.py`) 逐行重复, 应提基类注入 vector store
- 重试/超时逻辑散落 (LLMCall 仅 1 处重试、ReRank 无、摘要内联) → 统一 `with_retry` 装饰器
- Provider 参数合并 3 处、`_detect_params/_generate_template` 2 处、归档删除三段式 2 处

---

## 四、修复优先级建议

| 优先级 | 内容 |
|--------|------|
| **P0** | S1-S5 (LLM 可达的执行边界: shell 门禁、沙箱写/删、reg 分类、文件名穿越) —— 这些是"提示注入 → 宿主任意操作"的直接路径 |
| **P0/P1** | S6-S8 (鉴权 + CORS 白名单 + WS Origin 校验、/config 与模型接口脱敏) —— 若服务只绑回环可略缓, 但绑定 0.0.0.0 前必须完成 |
| **P1** | S10-S11 (SQLi + restore 路径校验, 照 `purge_archive` 范本补齐); B1-B4 (会话并发锁) |
| **P2** | 中危其余项 (S9、S12-S18) 与 B5-B9; 低危随日常迭代清理 |
| **P3** | D1-D6 抽象 (建议在动 P0/P1 时顺手统一路径校验 D2, 一次解决一类问题) |

---

## 五、正面确认 (无需改动)

- 全仓 `yaml.safe_load`, 无不安全反序列化 (无 pickle/yaml.load)
- SQL 绝大多数参数化绑定
- `static_ui` 静态文件路径校验正确 (`resolve()` + `relative_to`)
- `save_upload` 文件名消毒 (`Path(file_name).name`) + 10MB 限制
- `purge_archive` 路径校验 (`maintenance.py:560-563`) 是可复用的正确范本
- 流式降级重试有 `received_chunk` 守卫, 不会死循环
- 上下文按整轮截断可保持 tool_call/tool 结果配对
