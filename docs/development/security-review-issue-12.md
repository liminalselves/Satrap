# 安全审查报告 (issue #12)

- 关联 issue: #12 安全性审查 (数据安全, 代码沙箱等安全相关代码的系统性审查和检验)
- 审查分支: `review/issue-12`; 基线: `main` @ `1ecb13c` (2026-09-19, 工作区干净)
- 方式: 只读审查, 按风险面分 3 个切片 (网络 API 与前端 / 代码执行与沙箱 / 数据与平台适配器) 独立核查后汇总定级; `satrap/api/` 由汇总方补充核查
- 增量复审: 2026-09-26, 同步 main `1ecb13c` → `31604d6` (79 提交: issue #10 整改与冗余清理, 新增唤醒管线 / group_admin 插件 / ASR 调用 / 存储持久化原语); 初审问题逐条复核, 新增代码按两个切片 (唤醒管线与平台层 / 插件·ASR·存储·配置) 审查, 结论见"增量复审记录"
- 威胁模型: 项目默认仅供本人本地使用 (非多租户公网服务); 不可信输入源为 ① 远程平台用户消息 (Misskey / OneBot, 可经提示注入影响 LLM 行为), ② LLM 生成的代码与工具调用, ③ 本机其他进程 (共享机器场景)
- 级别定义: P0 致命 (直接可利用, 须立即修) / P1 高 (实际风险, 优先修) / P2 中 (设计性风险或纵深防御缺口) / P3 低 (加固项与信息性)

## 结论总览

整体防御水位较高: 鉴权 / CORS / 路径校验 / SQL 参数化 / SSRF 防护等基础设施均有真实实现 (见"确认安全"清单)。未发现 P0。

| 级别 | 初审 | 复审后 | 编号 |
| --- | --- | --- | --- |
| P0 | 0 | 0 | — |
| P1 | 1 | 1 | 问题 1 |
| P2 | 4 | 5 | 问题 2-5, 16 |
| P3 | 10 | 13 | 问题 6-15, 17-19 |

初审 15 项问题在 2026-09-26 增量复审中逐条复核, 均未消除 (逐项状态见文末"增量复审记录"); 新增问题 16-19。

---

## P1

### 问题 1: 消息组件可读取本地任意路径并随消息外发到远程平台

状态: 原语级已证实 (三段代码路径全部核实); 端到端利用链推测 (依赖管线如何把入站组件带回回复链, 本次未复现)。

证据:

1. 组件读取本地路径无目录白名单 — `satrap/core/components/message.py`:
   - `:409-413` `convert_to_file_path`: `file://` 源只检查 `os.path.exists` 即返回绝对路径;
   - `:424-425`: 非 http/base64 的裸路径同样 `exists → abspath` 放行;
   - `:436-437` `convert_to_base64`: `file://` 源直接读全文转 base64。
   - 对比: 下载**目标**路径有白名单 (`File._download_file` `:944-947` 的 `is_relative_to` 检查), 但读取**来源**没有。
2. OneBot 发送链路透传并主动封装本地路径 — `platform/onebot/onebot_utils.py:295-298` `_normalize_file_source`: `file://` 原样返回, 裸路径转 `file:///{abspath}` 交给 OneBot 客户端读取发送。
3. Misskey 发送链路上传本地路径 — `platform/misskey/misskey_utils.py:472-481` (`resolve_component_url_or_path` 对非 http 字符串按本地路径返回) → `adapter.py:453-461` → `client.py:642-644` `open(file_path, "rb")` 上传到远程 Misskey Drive。`file://` 前缀会在 `open()` 失败, 裸绝对路径成功。
4. 入站攻击面 — `onebot_utils.py:157-175`: image/record/video/file 段的 `data.get("file")` / `data.get("url")` 不做值校验直接进入组件 `file` 字段; `misskey_utils.py:352-371` 入站文件同样构造组件。

可达链 (推测部分): 远程用户发送带 `file://C:/Users/.../config.yaml` 之类 file 字段的消息 → 组件进入会话管线 → LLM 回复或跨平台转发再次携带该组件 → 适配器发送时读取本地文件并上传到远程平台。远程用户无法直接批准工具, 但可通过提示注入影响 LLM 组装回复内容。

修复方向: `convert_to_file_path` / `convert_to_base64` / 两个适配器的上传路径统一加来源白名单 — 仅允许 `.satrap` 数据目录与会话 uploads/临时目录 (复用 `File._download_file` 已有的 `is_relative_to` 模式), 白名单外的路径拒绝并记日志。

---

## P2

### 问题 2: OneBot 适配器默认无鉴权, 本机任意进程可注入伪造事件

状态: 已证实 (kwargs 构造层面); aiocqhttp 库在 token 非空时才校验, 其源码未核。

证据: `platform/onebot/adapter.py:59-60` (`access_token`/`secret` 默认空串), `:89-94` (未配置时反向 WS 服务端不启用鉴权, 默认 `127.0.0.1:8080`), `:162-176` (收到的消息无二次鉴权进入管线), `:170-172` (`self_id` 直接取自事件)。

影响: 本机任意进程可连 8080 发伪造消息事件 (可携带问题 1 的 `file://` 组件) 进入 LLM 管线并触发回复; 配置 `host: 0.0.0.0` 时暴露到局域网 (代码不阻止)。

修复方向: `access_token` 为空时拒绝启动反向 WS 或输出显著告警; 文档标注默认不安全的组合。与问题 1 组合时影响升级, 建议同批修复。

### 问题 3: CodeSandbox 名为沙箱, 实际只有"工作目录 + 超时"约束, 无 OS 级隔离

状态: 已证实 (无隔离 + 默认拒绝兜底均已证实)。

证据: `core/utils/sandbox.py:58-65` 仅 `subprocess.run([...], cwd=..., timeout=...)`; `:93-103` `run()` 直接 `['-c', code]` 执行 LLM 提供的任意代码 — 代码内可 `open()` 任意路径、`requests` 任意地址、读 `os.environ` (含全部 API key)。`_safe_join` (`:26-43`) 只保护文件操作的路径参数, 不约束执行的代码本身。项目自己的审批文案承认了这一点 (`expend/plugins/base_take/tools/utils.py:95`: "代码沙箱仅限制工作目录, 代码仍可访问系统资源")。

兜底 (已证实有效): 执行必须过 `execution_authorizer` (`expend/tools/sandbox_tools/sync.py:42-71`), provider 缺失时默认拒绝 (`utils.py:91-93`); `user_input_provider` 仅在本地 Chat UI 会话注入 (`display/service.py:838/1380`), 平台会话未设置 — 远程平台用户无法批准执行。

修复方向: 执行前最小化子进程 `os.environ` (至少剥离 `SATRAP_API_TOKEN` 与模型 key 相关变量); 在平台接入文档固化"user_input_provider 不得接远程通道"约束 (防回归); 长期可评估真实隔离 (Job Object / 受限令牌)。

### 问题 4: shell 工具批准执行的命令继承完整进程环境变量 (含密钥)

状态: 已证实 (审批闸门本身未被绕过; 环境泄露面证实)。

证据: `expend/plugins/satrap_coding/tools/shell.py:44-59` 整条 LLM 命令交给 `cmd /C` 或 `powershell -Command` (解释器即 shell, 命令内容天然任意执行, 安全闸门是审批而非拼接); `:79` `env={**os.environ, ...}` — 批准的命令可读父进程全部环境变量与 `config.yaml` 中的模型 key。审批文案已披露"可访问当前进程资源" (`:60-63`)。

闸门 (已证实无策略绕过): `core/command_gate.py` 黑名单仅辅助分类; `permission.py:386-387` 硬编码 shell 操作一律 `ASK`, 任何模式 / 持久规则 / judge 结果都不能放行 (`:399-413` 只能收窄为 deny/ask)。输出截断 2 万字符、超时 1-3600s、审批后 TOCTOU 复核 (`sync_interaction.py:138-143`) 均已实现。

修复方向: 与问题 3 同批做子进程环境最小化。远程平台用户无法批准 shell (无 provider 即拒绝, `approval.py:233-237`), 剩余风险是诱导本地用户点"y"的社会工程, 审批 UI 显示完整命令可缓解。

### 问题 5: session_scan_paths 目录下的 .py 顶层代码会被执行, 且 sys.path[0] 污染可遮蔽标准库

状态: 已证实 (执行点与当前无免审批写入路径均已证实; 属纵深防御缺口)。

证据: `core/framework/session_discovery.py:80-81` `importlib.import_module` + `reload` — 扫描目录下每个非 `_` 开头的 .py 顶层代码直接执行; `SessionClassManager.py:220` 惰性导入注册的类。`session_discovery.py:151-157` `sys.path.insert(0, import_root)` 且模块名用裸文件名 (`:251-271`) — 扫描目录放一个 `json.py` / `requests.py` 即可在整个后端进程遮蔽后续同名 import。

边界 (已证实): 管理端点只能扫描/创建 ⊆ 配置的 session_scan_paths (`http_api.py:698-706`, `control_server.py:1909-1933`); 默认扫描目录 `.satrap/session` 在项目 `.satrap` 下, 切片内所有 LLM 可达写入路径均被挡 (文件工具拒绝 `.satrap`: `satrap_coding/tools/constants.py:24` + `paths.py:130-133`; CodeSandbox 锁在 `.satrap/sandbox`; shell 需审批)。

修复方向: `sys.path` 改为 append 且用独立命名空间导入, 避免遮蔽; 文档把 `session_scan_paths` 标注为"代码执行目录"。回归警惕: 任何未来新增"远程可写文件到 `.satrap/` 下"的功能 (上传 / 解压 / 同步) 都会与 `.satrap/plugins` 即放即执行 (`edictum/plugin.py:250-265`) 组合成无审批 RCE。

---

## P3 (加固项)

### 问题 6: Windows 下 .satrap/api-token 文件权限设置不生效; 非回环绑定时 token 走明文 HTTP

`core/server_auth.py:194` 的 `os.open(..., 0o600)` 在 win32 上 mode 参数仅映射只读位, 不设 ACL, 共享机器上其他本地账户可见 (单用户场景无影响, 共享机器场景视为 P2)。`cli/client.py:644-646` Bearer token 经明文 HTTP 发送 — 远程绑定 (文档支持的场景) 时网络路径上的攻击者可截获。修复方向: Windows 下用 icacls/ACL 收紧或文档标注; 远程绑定场景考虑 TLS 反代。

### 问题 7: GET /ui-config.json 免鉴权, 泄露服务拓扑

`core/utils/minihttp.py:386-389` 跳过鉴权门; 返回三个服务的 host:port (`control_server.py:968-987`, `http_api.py:311-315`)。默认回环下仅本机进程可见; 不泄露 token; 属前端 bootstrap 的有意设计。可选加固: 响应最小化。

### 问题 8: API 错误响应直接回显 str(e), 可能泄露服务器内部路径

`control_server.py:1272-1273/1288-1289`, `http_api.py:485-486/923-924`, `display/server.py:343-344/451-452` 等; `GET /config` 还返回 `"path": str(CONFIG_PATH)` (`:1269`)。仅持 token 调用方可达。修复方向: 统一的错误净化 helper。

### 问题 9: debug 日志打印携带文件访问 token 的完整 URL

`core/components/message.py:461, :962` — `logger.debug(f"已注册: {callback_host}/api/file/{token}")`。URL 即凭据, 日志落盘等于泄露单文件访问权。修复方向: 脱敏 token 后缀。

### 问题 10: SatrapFileTokenService 注册无过期 / 撤销; /api/file/ 路由当前未实现

`message.py:164-201` `_files` 字典只增不减; `Video.to_dict` (`:506-513`) / `File.to_dict` (`:972-978`) 会把本地路径替换为 `{callback_host}/api/file/{token}` 发进平台消息 (留在远程平台记录里)。`set_callback_api_base` 仓内无调用方, 功能休眠。启用前必须给路由加鉴权并给 token 加 TTL (静态资源服务发生在鉴权之前, `minihttp.py:382`)。

### 问题 11: 引导 token 经 URL fragment 传递; 主 token 无轮换 / 撤销机制

`satrap-ui/src/api/auth.ts:6-14` 从 `#token=` 读取后 `history.replaceState` 抹除 (不进 Referer / 服务端日志; 不入 localStorage)。`server_auth.py:168-206` 主 token 长期固定, 无 TTL / 轮换 API (Cookie 会话有 8h TTL 可撤销, Bearer 主 token 没有)。低危, 可选加固: token 轮换命令。

### 问题 12: Misskey ssl_verify=False 下载回退

`platform/misskey/client.py:765-775` — 仅当 `allow_insecure_downloads` 显式开启 (默认 False, `adapter.py:87`) 且 URL 与实例同源 (`outbound/utils.py:301-325`) 才回退; `:730-731` 二次兜底, `:739` 重定向锁源。下载内容写入 uuid 临时文件后上传为媒体附件, 不执行。残余: 与自建实例之间的中间人可篡改媒体内容。双条件 opt-in, 可接受; 其余全部出站请求默认校验证书 (grep 证实 `ssl=False` 仅此受控路径)。

### 问题 13: grep_files 的 LLM 提供正则无超时, 恶性回溯正则可挂住工具线程

`satrap_coding/tools/file_core.py:180` `re.compile(pattern)` 后逐行 search。仅可用性 (DoS), 无提权。修复方向: 复杂度预检或带超时执行。

### 问题 14: spawn 子进程 payload 携带 api_key; allow_insecure_base_url 默认开启

`expend/tools/agent/utils.py:80` (api_key 进 payload, spawn 命令行参数在本机其他用户可见), `:92` (`allow_insecure_base_url: True`)。本地单用户影响极小; 与问题 3/4 的环境最小化同批处理。

### 问题 15: 远程平台用户可对自己会话执行 /approve mode full 等斜杠命令

`edictum/simple_session/async_.py:242-248` 平台消息同样过 `cmd_process`; `satrap_coding/commands.py:127-131` `/approve mode full` 持久化到该会话。影响已封顶 (已证实): 平台会话文件工具锁在独立会话沙箱 (`SessionManager.py:2306-2307`), shell 任何模式都需本地 provider, `.satrap/.git/.env` 独立拦截不受 mode 影响 (`file_core.py:56-58`); `/approve rule` 只能加 0-1 级规则 (`:139-143`)。信息性: 文档注明"平台消息可切换自己会话的审批模式"即可。

---

## 增量复审新增发现 (2026-09-26, 基线 31604d6)

### 问题 16 (新增, P2): group_admin 插件写工具 fail-open — 写开关一开且白名单留空即任意群成员可驱动管理操作

状态: 已证实 (配置语义与"未接审批闸门"均已核实); 实际利用依赖用户显式开启写开关。

证据: `expend/plugins/group_admin/tools.py:125-130` — `write_tools_enabled` 开启后, `allowed_callers` 为空时调用者检查整体跳过 (fail-open), `allowed_groups` 同理; `meta.yaml:8-14` — 写开关默认 `false`, `allowed_callers` 默认空 ("留空不限制调用者"); `onebot/admin.py:257-307` — 工具动作直接经 `OneBotAdmin._call` 下发 OneBot 动作 (kick/ban/whole_ban/set_admin/recall 等, `tools.py:47-100`); 全仓 `execution_authorizer` 仅存在于 sandbox_tools 与 base_take — **group_admin 写工具未接入任何按次审批闸门**, 且全文无频率限制。

可达路径: 群成员消息 → 唤醒机器人 → 提示注入诱导 LLM 调用 `group_admin_kick` 等写工具 → 真实踢人/禁言/撤回。

缓解 (已证实): 写开关默认关闭; 群范围双层校验 (插件 `allowed_groups` `tools.py:156-157` + 适配器 `allows_group` `admin.py:309-317`); 审批类动作有 flag 账本归属/状态/TTL 校验 (`admin.py:838-900`); 审计日志只记 flag 摘要不落原文 (`admin.py:297-306`); 参数面校验完善 (ID 强制十进制、时长上限、nodes 限额)。

修复方向: `allowed_callers` 为空时写操作默认拒绝 (fail-closed); 或写动作接入与 code_sandbox 一致的 `execution_authorizer` 逐次审批; 写动作按 (actor, action) 加最小频率限制。

### 问题 17 (新增, P3): 群窗口合成 prompt 的框架标记可被成员正文伪造

`core/pipeline/scheduler.py:584, 593-594` — 窗口批次合成为 `[用户 {actor_id}, 消息 {message_id}] {text}` 时成员正文未做框架样式转义, 群成员可发送含同类标记的文本伪造消息归属/窗口边界。属"正文即数据"的固有提示注入面, 无权限越界; 建议转义或在系统提示词声明标记不可信。

### 问题 18 (新增, P3): group_admin 只读工具无调用者限制, 可批量导出全群名册

`tools.py:128-130` 调用者检查仅覆盖写操作; `admin.py:355-379` 成员列表返回昵称/名片/角色/入群时间等, 上限 2048 条。任意群成员经注入可一次性把全群名册导出进模型上下文, 并可能随回复外泄给 LLM 提供商。

### 问题 19 (新增, P3): send_forward 节点昵称可任意指定, 可伪造"他人发言"

`onebot/admin.py:619-623` — 合并转发节点的展示名由模型参数控制 (截断 30 字符); `uin` 固定为机器人自身账号 (`:611-613`, 不可伪造), 但客户端展示昵称可冒充其他成员, 制造误导性转发截图。

---

## 确认安全 (已证实的正面结论)

- **HTTP 鉴权统一强制**: 后端 / 聊天 / 控制三服务全部路由过 `authorized()` (仅 `/ui-config.json` 与静态资源例外); WebSocket 升级前校验 origin + 鉴权 (`minihttp.py:512-517`); 15 个控制路由逐一核对均在门内; `/api/shutdown` 在门后。
- **token 机制**: `secrets.token_urlsafe(32)` 生成 (约 192 bit), `hmac.compare_digest` 常数时间校验 (`server_auth.py:341`); 非回环绑定强制显式 ≥32 字符 token, 否则拒绝启动 (`:296-300`); `.satrap/` 在 .gitignore 内 (验证过 check-ignore)。
- **CORS**: 精确 Origin 白名单 (本机端口 + 显式 env), 非白名单一律 403, 无通配反射; CSRF 面封闭 (写操作需 Authorization 头或 SameSite=Strict Cookie)。
- **workspace_roots 真实强制**: `resolve()` 后 `is_relative_to` 校验 (`display/service.py:2099-2119`), 抗 `..` / 绝对路径 / 符号链接; `.satrap` 硬编码 denied; 上传剥离目录成分 (`:2277`), conversation_id 经 slug+sha256 净化 (`storage/layout.py:24-39`)。
- **前端无 XSS sink**: React/TSX 全仓无 `dangerouslySetInnerHTML` / `innerHTML` / `eval`; LLM 输出经 react-markdown 且未启用 rehype-raw (原始 HTML 不渲染); token 不入 localStorage; 会话凭据为 HttpOnly + SameSite=Strict Cookie, 服务端只存 SHA-256 摘要。
- **SQL 全参数化 / 白名单**: 动态表名 / 列名均来自硬编码白名单或同表行键 (`storage/database.py:69-74` 等); LIKE 通配符转义 (`:34-45`)。
- **反序列化**: yaml 全 `safe_load` (`config/_yaml.py:22` 等); msgpack 无 object_hook (`core/database/__init__.py:38`); pickle 仅同进程 spawn 父子传递, 不跨信任边界 (`expend/tools/agent/sync.py:109-137`)。
- **subprocess**: 全仓仅 4 处, 全部列表参数, 无 `shell=True`, 参数无不可信拼接。
- **SSRF 防护完整**: `core/utils/outbound/` — 绝对 URL 校验、拒绝 URL 内凭据、逐 IP `is_global` 校验、DNS 解析钉定防重绑定、重定向逐跳复检、响应字节上限。
- **代码 / 插件加载**: 官方插件在包内; 用户技能目录只执行可信代码根下的 tools.py (`core/utils/skills/manager.py:72-86`), 其余只当文本; `.satrap/plugins` 即放即执行但 LLM 写不进 `.satrap`。
- **tempfile**: 全部随机名 + `os.replace` 原子替换, 无 `mktemp` / 固定路径竞态; 回收 / 恢复路径系统性地拒绝符号链接 (`storage/maintenance.py:121-157` 等)。
- **配置脱敏**: `GET /config` 与 CLI `config show` 均按字段名脱敏 (`core/config/document.py:16-92`), 回填保留原值; misskey WS 日志有 secret 脱敏 filter (`client.py:40-68`)。
- **资源限制**: shell / 沙箱 / subagent / 工具输出均有超时与大小上限 (问题 13 的正则是唯一缺口); 出站抓取有私网防护与 8MiB 上限。

增量复审新增正面结论 (31604d6 新代码中证实):

- **新增 HTTP 端点全部在鉴权门内**: `POST /api/platforms/wake` 及 wake 拒绝/诊断查询、`POST /config/wake-dry-run`、ASR 测试、`POST /api/config/reload` 均过 `authorized()`; manual wake 不能无鉴权触发, `operator` 取服务端常量并禁止从 payload 读取 (`BackendManager.py:461-463`)。
- **审批执行链加固**: flag 登记先核验账号 (`adapter.py:609-631`); 占用在持久事务内校验归属/状态/TTL 并原子落盘 (`request_registry.py:524-551`); 审批先 occupy 后网络动作, 超时记 unknown 不可重放 (`admin.py:838-900`); 审计日志只记 sha256 摘要。
- **诊断/回执最小化**: 只存脱敏原因码与定位字段, 不存正文/音频/密钥/供应商响应 (`request_diagnostics.py:336-338`); 管线错误对平台只发固定文案 (`scheduler.py:645-648`)。
- **ASR 链路**: 密钥锁定 (`APICall/ASRCall/base.py:38`, `lock_api_key` 默认 True); base_url 拒绝 URL 内嵌凭据与非回环明文 HTTP (`utils:103-141`); 音频三级获取全走出站防护 (16MiB/4 条/90s 预算), PyAV 内存转码无子进程 (`audio_convert.py:184-218`)。
- **存储新原语**: `storage/durability.py`/`persist.py` — 严格 json 校验, 无 pickle/yaml.load; 同目录临时文件 + `os.replace`; fail-safe 顺序固定 (先持久标记后隔离); `file_lock.py` 修复 POSIX inode 双持有竞态。
- **配置面**: `_yaml.py` 用 CSafeLoader (保持 safe_load 语义); 配置写入加文件锁 + sha256 乐观并发 (409 冲突检测) + 原子写; `plugin_config.py` 修复 `bool("false")==True` fail-open。
- **来源身份机制**: `call_context.py` ContextVar 在入站边界冻结, 会话结束撤销, 模型参数无法伪造调用来源 (`SessionManager.py:2355-2381`); 群上下文 scope 用 sha256 摘要隔离 (`conversation.py`)。
- **资源上限全面**: 窗口三表有界 (`wake_window.py:27-43`), 诊断 256 请求×16 条, 发送队列 64, `_seen_messages` 4096, 附件总预算 90s。

---

## 审查覆盖清单

基线 `1ecb13c`, 分支 `review/issue-12`。总量: satrap 包 66,334 行 Python, satrap-ui/src 约 15,048 行 TS/TSX。

**全读** (逐行): `core/backend/` 全部 5 文件 (control_server 2222 行, http_api 1133 行, BackendManager, static_ui, ui_config); `core/platform/` 全部 8 文件; `core/storage/` 全部 6 文件; `core/database/__init__.py`; `core/server_auth.py`; `core/utils/minihttp.py`; `core/components/message.py`; `core/utils/sandbox.py`, `outbound/` 全部, `media.py`, `paths.py`; `core/framework/SessionClassManager.py`, `session_discovery.py`; `edictum/plugin.py`, `plugin_runtime.py`, `config.py`, `registry.py`, `plugin_resources.py`; `expend/` 插件工具层大部 (satrap_coding tools 全部, sandbox_tools, tools/agent, base_take 关键文件); `satrap/api/` 全部 3 文件; `cli/cmd_control.py`, `client.py`, `common.py`, `backend_lock.py`, `cmd_platform.py`; `satrap-ui/src/api/auth.ts`, `client.ts`, `websocket.ts` 等关键文件。

**部分读** (定向区段 + 危险模式全量 grep 交叉): `satrap-ui/src` 其余 (全仓 sink 扫描: dangerouslySetInnerHTML/innerHTML/eval/localStorage/href/window.open/createObjectURL/硬编码 token 均无命中); `display/service.py` (2682 行, 路径 / 上传 / 媒体 / 审批区段精读), `recorder.py` (SQL 清单 grep 全参数化); `edictum/simple_session` 编排文件, `plugin_spec/catalog/compatibility/plugin_config/plugin_settings` (grep 无网络 / 写文件 / exec); `expend/` 其余 (memory_store SQL 参数化抽样, rag 工具路径白名单); `core/config/document.py` (脱敏 / 合并逻辑完整); `core/utils/skills/`。

**仅 grep 扫描** (无危险模式命中): `cli/` 其余 16 文件; `core/framework/Base/execution/` 引擎层; `main.py` argparse 面。

**未纳入** (排除理由): `tests/` 135 文件 (验证工具而非风险面; 修复时在此补安全回归测试); `satrap-ui` 的测试 / benchmarks / e2e; `node_modules` / `package-lock.json` (依赖 CVE 比对未做, requirements.txt 下限版本较新); `docs/` (除作为规范依据引用); `scripts/` 7 个开发脚本 (本地开发用, 未审查 — 待办项)。

**增量复审覆盖 (1ecb13c → 31604d6)**: 新文件全读 — pipeline 11 个 (manual_wake, manual_wake_store, wake_policy/window/timers/dry_run, request_diagnostics, input_projection, attachments, audio_convert, scheduler 全文), onebot 新模块 (admin.py, outbound.py, request_registry.py), platform/receipt.py, notices.py, group_admin 插件 3 文件, ASRCall 5 文件, config 新文件 (asr_references, wake_overrides, platform_policy), storage/durability.py, persist.py, call_context.py, ManualWakeModal.tsx 等。修改文件 diff 全读 + 安全区段精读 — onebot/adapter.py (1270 行全文), BackendManager, control_server, http_api, message.py 转换函数与 token 服务, onebot_utils, document.py, session_discovery, UserManager, SessionManager, edictum plugin_config/resources/settings, cli 变更, config.example.yaml。全仓新增行危险原语扫描 (subprocess/eval/exec/pickle/yaml.load/__import__/ctypes/mktemp): 仅命中开发脚本 `scripts/probe_snowluma.py:160-164` (本地 node 调用, 列表参数, 无运行时导入路径, P3 信息)。未覆盖移交: misskey/adapter.py 仅 diff (8 行签名对齐), e2e 脚本内容, scripts/ 其余。

## 验证缺口 (诚实声明)

1. 静态审查为主: 问题 1 的端到端利用链、问题 2 的伪造事件注入均未实际复现 (原语级代码路径已证实)。
2. `aiocqhttp` 库在 token 非空时的校验行为未读其源码。
3. 依赖版本未做 CVE 数据库比对。
4. 本次为只读审查, 未运行测试套件做基线确认。
5. `scripts/` 下 PowerShell / bat 启动脚本未审查。

## 修复路线建议 (Gate 建议)

均为 `Local Fix Only` 级别, 无需重构; 全部改动需你授权后另行实施:

| 批次 | 内容 | 预算 |
| --- | --- | --- |
| 第一批 | 问题 1: 消息组件读取来源白名单 (message.py + 两个适配器上传侧) | 1-3 文件, ≤120 行 |
| 第二批 | 问题 2-5, 16: OneBot 空 token 拒绝启动 / 告警; sandbox 与 shell 子进程环境最小化; session_discovery 的 sys.path 改造; group_admin 写操作 fail-closed (空 allowed_callers 拒绝) 并接入审批或加频率限制 | 分 4 个独立小补丁 |
| 第三批 | 问题 6-15, 17-19 按需: 优先 9/10 (日志与 token 生命周期)、13 (ReDoS)、18 (名册导出); 7/8/11/12/14/15/17/19 进 backlog | 各 ≤30 行 |
| 收尾 | 每批修复补安全回归测试 (tests/ 下新增); 更新平台接入文档 (user_input_provider 约束、session_scan_paths 定性、OneBot token 要求、group_admin 写开关语义) | — |

## 修复记录 (2026-09-26, 分支 review/issue-12)

已实施第一批 + 第二批 + 第三批优先项, 共 8 个独立小修复:

| 问题 | 修复方式 | 改动 |
| --- | --- | --- |
| 1 (P1) | 新增 `core/utils/paths.py` 的 `ensure_allowed_media_path` 白名单 (默认 `.satrap` 数据目录 + 系统临时目录, 可用 `SATRAP_EXTRA_MEDIA_ROOTS` 追加), 部署在 message.py 四个本地读取点、token 注册服务、OneBot `_normalize_file_source` 与 `_send_file` (拒绝时回执 `media_source_denied`); misskey 链路经 `convert_to_file_path` 自动覆盖 | 4 文件, +85 行 |
| 2 (P2) | OneBot 适配器启动时空 `access_token`: 回环地址告警, 非回环地址拒绝启动 (`_is_loopback_host`) | adapter.py, +15 行 |
| 3+4 (P2) | 新增 `core/utils/proc_env.py` `sanitized_child_env` (剥离名称含 KEY/TOKEN/SECRET/PASSWORD/PASSWD/CREDENTIAL 的环境变量), sandbox 与 shell 子进程统一接入 | 3 文件, +31 行 |
| 5 (P2) | `sys.path.insert(0)` 改为 `append` (扫描目录不再遮蔽标准库); config.example.yaml 与 configuration.md 标注扫描目录为"可信代码目录" | +3 行 |
| 16 (P2) | `_resolve` 改为 fail-closed: 写操作要求 `allowed_callers` 非空, 留空即拒绝; meta.yaml 同步描述 | tools.py + meta.yaml |
| 18 (P3) | 新增 `allowed_read_callers` 配置, 可选限制只读工具调用者 (留空保持不限制) | tools.py + meta.yaml |
| 9 (P3) | 文件回调 URL 的 debug 日志只保留 token 前 8 位 | message.py 2 处 |
| 10 (P3) | `SatrapFileTokenService` 增加 24h TTL 与 1024 条上限, 注册路径强制过白名单 (单点收口 `/api/file/` 暴露面) | message.py |

**问题 13 (ReDoS) 未修**: 彻底修复需要引入 `regex` 依赖 (带超时参数) 或接受泄漏卡死线程的权衡, 留待决策; 其余 backlog 项 (6/7/8/11/12/14/15/17/19) 未动。

**代码质量复审与修正 (2026-09-27)**: 安全修复完成后按 development-guidelines.md 做了一轮质量审查并全部修正 — ① 媒体白名单拒绝改用专属异常 `MediaSourcePermissionError` (继承 PermissionError), OneBot 发送链 5 个捕获点细分 `media_source_denied` 原因码并经 `_failed_receipt` 记 warning 日志, 不再与"目标范围拒绝" (`target_unavailable`) 混淆, 拒绝也不再静默; ② `get_allowed_media_roots` 内部改 `lru_cache` (以环境变量原始值为缓存键), 热路径不再每次做文件系统 resolve; ③ 6 处导入顺序对齐"路径字符数降序"规范; ④ `_resolve` docstring 补 fail-closed 约束; ⑤ `_LOOPBACK_HOSTS` 死条目清理; ⑥ 测试冗余行清理, 新增 2 条发送链 reason 断言 (image 段走 guard / file 段走分流)。改动区 228 用例全过, pyright 零新增错误。

验证结果: 新增 5 个安全回归测试 (媒体白名单拒绝/放行、OneBot 来源拒绝、group_admin fail-closed 与只读白名单), 并把 8 处既有写工具测试更新到 fail-closed 契约; 改动区 9 个测试文件 226 用例全过; 全量 unit 2228 passed / 8 skipped — 5 个稳定失败 (attachments AMR, database_recovery ×2, minihttp 413, model_config ASR) 经干净基线对照确认为环境存量问题, wake_window 为全量并发下的时序抖动 (单独运行全过, 两次全量失败集合不同); pyright 对全部改动文件 0 新增错误 (`get_tools` 重载告警在 HEAD 上已存在)。

## 残余风险与回归警惕

- workspace `resolve()` 校验与工具实际读写之间存在 TOCTOU 窗口, 需要本地文件系统写权限, 超出"远程消息进入"威胁模型, 接受。
- `SATRAP_TRUSTED_DOWNLOAD_HOSTS` 可放行指定内网主机 — 若配置指向内部敏感服务, 远程用户可控的媒体 URL 可触达; 属运维配置责任, 文档应标注。
- 回归警惕三件事 (未来代码审查时复查): ① 任何能远程写入 `.satrap/` 的新端点会与扫描目录导入 / `.satrap/plugins` 即放即执行组合成无审批 RCE; ② `user_input_provider` 一旦接到远程通道, code_sandbox / shell 审批闸门即失效为远程 RCE; ③ `MCPServerExporter` 支持 sse/http 传输但仓内无调用, 启用前需独立审查。

---

## 增量复审记录 (2026-09-26, 基线 1ecb13c → 31604d6)

### 初审问题逐条复核状态

| 问题 | 状态 | 复核证据 |
| --- | --- | --- |
| 1 (P1) | **仍在** | `message.py:401-437` (convert_to_file_path `:409-413`/`:424-425`, convert_to_base64 `:436-437`); `onebot_utils.py:293, :382-386`; 新增佐证 `adapter.py:1011-1024` (`_send_file` 对 `file:///` 与本地路径 `os.path.abspath` 后直接 upload); `misskey_utils.py:482-484` |
| 2 (P2) | **仍在并扩展** | `adapter.py:147, :217-222` (空 token 不启用鉴权, 默认 127.0.0.1:8080); 新增: 空 `self_id` 采纳首个上报账号 (`:327-333, :544-550`), 就绪探针 `/_satrap_ready/<token>` 为无鉴权本地端点 (`:168, :224-228, :1237-1250`) |
| 3 (P2) | 仍在 | `core/utils/sandbox.py` 本区间无变更 |
| 4 (P2) | 仍在 | `satrap_coding/tools/shell.py` 无变更 |
| 5 (P2) | 仍在 | `session_discovery.py:80-81` (import+reload), `:155-156` (`sys.path.insert(0)`) |
| 6 (P3) | 仍在 | `server_auth.py` 无变更 |
| 7 (P3) | 仍在 | `minihttp.py:386-389` 免鉴权面未变 |
| 8 (P3) | **仍在并扩展** | `control_server.py:1465-1468` 新增 ASR 测试 502 分支回显异常文本; 改善项: 管线错误对平台只发固定文案 (`scheduler.py:645-648`), 诊断只存脱敏原因码 |
| 9 (P3) | 仍在 | `message.py:461, :964` |
| 10 (P3) | 仍在 | `message.py:185, :512, :979` |
| 11 (P3) | 仍在 | `satrap-ui/src/api/auth.ts` 无变更 |
| 12 (P3) | 仍在 | `misskey/client.py` 无变更 |
| 13 (P3) | 仍在 | `file_core.py:180` 无变更 |
| 14 (P3) | 仍在 | `expend/tools/agent/` 无变更 |
| 15 (P3) | 仍在 | `simple_session/` 与 `satrap_coding/commands.py` 无变更 |

### 增量验证缺口

1. 仍为静态审查: 问题 16 开启写开关后的实际行为、问题 1 端到端链均未运行验证。
2. 唤醒策略决策树 (wake_policy/manual_wake 状态机) 只审了边界与存储, 未逐分支验证业务正确性 (issue #10 已有独立复核覆盖该面)。
3. `scripts/` 其余脚本、`satrap-ui/e2e/` 脚本内容仍未审。
4. `aiocqhttp` 校验行为、依赖 CVE 比对两项缺口延续。
