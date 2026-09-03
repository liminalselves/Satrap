# Satrap 后端审计报告 (2026-09-01)

> 本文档为精简版, 只保留**未完成项**与**长期参考**内容: 未完成的安全/bug 残余、
> 可通用化设计 (D1-D6)、过度保护代码专项。
> 已完成的 22 项安全修复与 26 项 bug 修复的五轮复核记录已于 2026-09-02 移除,
> 历史版本见 git 提交 `b26e708` 及之前。文中行号均为审计时快照。

## 未完成的安全/bug 残余

> 本节结论经 2026-09-02 用户复查 + 逐条代码/实机核验修正 (5 条复查全部成立);
> 修复情况经 2026-09-03 第七轮核验 (见下节)。

### 第七轮修复验证 (2026-09-03)

三项残余已全部修复, 修复核心是 `outbound.py` 重构为**统一安全出站层**:
`resolve_outbound_http_url` 返回校验过的连接地址; `safe_sync_get`/`safe_async_get`
经 `_PinnedResolver` (aiohttp) / `_PinnedHTTP(S)Connection` (stdlib) 把校验 IP 绑定到实际连接,
Host 头与 TLS SNI/证书校验保留原始主机名, 重定向逐跳重新校验并重绑 (上限 5 次),
响应按 Content-Length 预检 + 流式计数双保险限流 (普通 8 MiB / 下载 32 MiB)。

| 发现 | 状态 | 修复与核验要点 |
|------|------|----------------|
| 通用文件下载绕过 SSRF | **已修复** | `message.py:download_file()` 改走 `safe_async_get` (URL 校验 + 逐跳重定向 + 地址绑定 + 默认 32 MiB 上限); `search.py` 同步/异步搜索与 fetch_page、`misskey/client.py:_download_bytes` 全部迁移到统一层; misskey 的"不安全 TLS 仅限已配置实例"语义由 `restrict_redirects_to_origin` 保留。实测: 私网/回环/云元数据 (169.254.169.254) 均拦截, trusted_hosts 例外生效 |
| DNS rebinding TOCTOU | **已修复** | 校验阶段解析的地址直接用于连接, 不再二次 DNS; 实测 `_PinnedResolver` 拒绝错配主机, 仅返回校验地址族匹配的记录; HTTPS 以原主机名完成 SNI 与证书校验 (`_PinnedHTTPSConnection.connect`) |
| Misskey token 日志加固 | **已闭环** | `adapter.py:179`、`client.py:247` (监听中断)、`client.py:789` (URL 上传失败) 全部改记 `type(e).__name__` |

验证: 相关单测 35 项 + 全量单测 **934 passed, 7 skipped** (均为环境跳过);
pyright 门禁 (`.pyrightcfg`) **0 errors, 1 warning** (新代码 `utils/__init__.py:48`
`reportUnknownVariableType`, `dict` 迭代键类型未收窄, 可顺手清理)。

遗留小项 (不阻塞):
- `download_file` 无 `trusted_hosts` 通道: 私网 OneBot 客户端 (如 go-cqhttp 本地文件 URL)
  会被 fail-closed 拒绝 — 若部署依赖内网文件源, 需要加配置化可信主机入口
- 新 `safe_*_get` 固定地址行为与 `safe_parse_arguments` 非 dict 归一暂无专项测试
  (现有 `test_outbound_security.py` 覆盖兼容包装 API)

发布待办 (非安全问题): 本地 main 领先 origin/main 22 个提交未推送, 本轮修复亦未提交。

## 可通用化的设计 (D1-D6, 未实施)

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
- `sandbox.py:26 _safe_join` (fail-open 已修复为越界抛错, 但三套并存的问题仍在)

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

## 过度保护代码专项 (2026-09-02)

专项目标: 找出意义不明的校验、对极不可能发生情况的防卫、重复冗余检查。
方法: 四路并行初筛 (框架 / 服务层 / LLM 管线 / 平台与工具) + 对全部高置信项逐条人工复核数据流。
最终分类 (2026-09-03 重分类后): **可安全清理 11 项 (A1-A10+B8), 保留 6 项 (A11+B2-B6),
移出 2 项 (B1 纯表达冗余 / B7 必要逻辑), 驳回初筛误报 6 项**。
总体判断: 代码库的防御性写法大多集中在安全边界与不可信输入处, 属于合理纵深防御;
真正的死代码主要是"写后立即回读校验"和"已被前行条件覆盖的重复判断"两类, 均为低风险清理项。

> 2026-09-03 用户裁定重分类, 每条依据均经逐条代码核对 (含 1 处裁定依据修正: B8)。
> **A 类 11 项已于 2026-09-03 全部清理完毕** (见下), B/C/D 为存量结论。

### A. 可安全清理 (11 项: A1-A10 + B8) — 已全部完成 (2026-09-03)

| # | 位置 | 清理方式 |
|---|------|----------|
| A1 | `SessionClassManager.py:register_config_entry` | 删写入后回读, 直接构造返回; params 防御复制双语义保留 (写入 `dict(params or {})` + 返回独立副本) |
| A2 | `state/mutation.py:state_mutation_context` | 构造入局部变量, set 后直接 yield, 删回读 None 检查 |
| A3 | `SessionManager.py:1359` | 删 `and new_id` |
| A4 | `SessionManager.py:678-683` | 空闲清理改 `pop(sid)` 直接解包 |
| A5 | `BackGroundManager.py:dump_named` | 删 name 自赋值行 |
| A6 | `LLMCall.py:143,201,234` | 删 `or len(...)` 长度复检 |
| A7 | `LLMCall.py` 工具参数解析 | 删外层无效 `except json.JSONDecodeError` (配合上轮解析器 `_as_argument_dict` 归一) |
| A8 | `permission.py:313-318` | 条件折叠为 `... and judge is not None`, 删 ASK 自赋值 |
| A9 | `tools.py:80-83` | 删第二道检查中的 bool 重复析取 |
| A10 | `platform/__init__.py:486-487` | `get(key)` |
| B8 | `control_server.py:1934-1940` | 删路由末尾第二次静态 serve。**已知行为变化**: 认证后 `GET /storage/*` 与未匹配 `GET /chat/history/*` 从 SPA index.html(200) 变为 404 JSON (`_is_control_api_path` 含但 `excluded_prefixes` 不含这两前缀, 原行为属意外 fallback, UI 无对应页面) |

回归测试 3 个: A1 返回副本隔离 (`test_session_class_config_service.py`)、
A5 dump 名称不变量 (`test_model_config_service.py`)、B8 行为锁定 (`test_control_static.py`)。
验证: 全量单测 **937 passed, 7 skipped**; pyright 门禁 **0 errors, 0 warnings**
(顺手清理了上轮 `utils/__init__.py:48` 的最后一个 reportUnknownVariableType)。

### B. 保留 (6 项)

| # | 位置 | 保留理由 |
|---|------|----------|
| A11 | `UserManager.py:642-644` | `store.get` 预检把正常的"用户不存在"与异常路径区分开: 删除后返回值相同, 但正常缺失会被外层 except 记成 error 日志 — 日志语义不同, 不是死代码 |
| B2 | `minihttp.py:497` | WS 处理器重复 origin 检查当前不可达, 但属信任边界纵深防御 (防未来新增调用点绕过 `_handle_connection`) |
| B3 | `tools.py` 8 处 | `_parse_integer_argument` 返回 `tuple[int \| None, str \| None]` 两个独立 Optional; `or X is None` 同时承担 pyright 类型收窄 — 重构返回类型前不宜批量删除 |
| B4 | `mcp.py:372-374` | `command`/`url` 是公有属性, 构造后仍可被外部改写, `__init__` 不变量不覆盖该情形 |
| B5 | `message.py:272`, `event.py:467` | 已核实 `model_config` 未启用 `validate_assignment` — 构造后赋值可写入原始字符串, 加上 `model_construct` 手工组件, `hasattr(t, "value")` 承重 |
| B6 | `misskey_utils.py:472-476` | `safe_getattr` 带默认值不抛 AttributeError, 但第三方对象的 property getter 可抛任意异常, try/except 防的就是这种, 成本为零 |

### C. 移出冗余清单 (2 项)

| # | 位置 | 定类 |
|---|------|------|
| B1 | `tools.py:362,464` | **纯表达冗余**: 判等被 `is_relative_to` 涵盖, 删留行为完全不变, 不构成"过度保护"; 属风格取舍, 不立项 |
| B7 | `sandbox.py:163-175` | **必要逻辑, 非重复保护**: 词法路径 (`abspath` 未解析) 的 `is_relative_to` 确认链接本体位于沙箱内, 据此 `unlink` 符号链接本身; `_safe_join` 解析真实目标, 授权的是后续递归删除。两者服务不同安全语义 (删链接 vs 删目标), 缺一不可 |

### D. 复核后驳回的初筛报告 (6 项)

| # | 初筛声称 | 驳回理由 |
|---|----------|----------|
| C1 | TCBuilder `execute_tool_call` 对 `call_info` 重复校验 4 次 | 实际只调 `validate_call_info` 一次; `create_call_message`/`get_call_info` 是公共静态方法, 各自容错供其他调用方使用, 非冗余 |
| C2 | `ReRankCall.py:80-85` else 分支冗余 | 该分支可达且承载 results/output 优先级语义 |
| C3 | `misskey/adapter.py:165-167` `_client` None 检查不可能触发 | `:611` close 路径会置 `_client = None`, while 重连循环内检查可达且承重 |
| C4 | `SessionClassManager.py:114` `inspect.signature` 的 except 过防 | 反射对 C 扩展/内建 `__init__` 可抛 ValueError/TypeError, 合理 |
| C5 | `SessionClassManager.py:290` 配置读取 except 过防 | 磁盘 JSON 是不可信输入, 合理 |
| C6 | 会话构造鸭子属性探测 (`_wf`/`wf`/`workflow`...) | 项目功能契约的兼容 shim (见 2026-08 项目功能实施), 非过度防卫 |
