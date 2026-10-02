# 安全审查与修复报告 (issue #12 / PR #15)

## 范围与当前结论

- 分支: `review/issue-12`; 初审基线 `1ecb13c`, 2026-09-26 增量复审基线 `31604d6`
- 当前状态: 2026-10-02, 已提交版本 `bafc868` 之后修复额外保护目录串会话和系统临时目录默认授权; 本轮修改尚未提交
- 使用边界: 默认本人本地使用, 不引入多租户策略或 OS 隔离架构; 重点核查平台消息中的媒体来源, 模型工具执行和可信代码加载
- 初审及增量复审共记录 19 项, 未发现 P0; 下文历史发现描述的是修复前状态, 不是当前缺陷清单
- 当前相关回归通过, 全量单元测试未全绿; 平台调用链使用模拟客户端验证, 未完成真实 OneBot / Misskey 上传验收

以前修复记录中的“Misskey 自动覆盖”“任意媒体目录名即可排除回收站”“全部修正”“唤醒单独运行全过”等结论缺乏对应证据, 已撤回。当前状态仅以下文修复表和验证记录为准, 不以历次测试通过数累计证明没有回归。

## 历史发现及处理状态

级别: P1 为高风险, P2 为明确边界缺口, P3 为加固或信息项。代码定位使用模块与函数名, 历史行号不适用于当前工作区。

| 编号 | 级别 | 修复前证据与影响 | 当前处理 |
| --- | --- | --- | --- |
| 1 | P1 | `message.py` 的 `convert_to_file_path` / `convert_to_base64`, OneBot 来源封装及专用文件上传, Misskey 来源解析均能将未经授权的本地路径交给读取或上传入口 | 已统一来源校验并补平台调用链回归; 远程消息诱导外发的完整攻击链未实测 |
| 2 | P2 | OneBot `access_token` 默认空, 反向 WebSocket 可接收本机进程伪造的事件; 非回环监听扩大可达范围 | 空 token 在回环监听时告警, 非回环时拒绝启动; 复用 `server_auth.is_loopback_host` |
| 3 | P2 | `CodeSandbox._execute` 只有工作目录与超时, 执行的 Python 仍可访问系统资源和父进程密钥 | 子进程环境剥敏已接入; 审批仍必需, 未提供 OS 隔离 |
| 4 | P2 | `satrap_coding/tools/shell.py` 批准后的 shell 继承完整环境; 文件中的密钥仍可由命令读取 | 子进程环境剥敏和每会话显式放行已接入; 不宣称能阻止任意命令访问系统文件 |
| 5 | P2 | `session_discovery` 顶层导入会执行用户代码, 扫描路径置于 `sys.path` 首位可能遮蔽标准库 | 可信目录说明, 路径末尾追加, 显式加载与冲突/失败处理已修正; 目录内代码仍会执行 |
| 6 | P3 | `server_auth` 的 `os.open(..., 0o600)` 在 Windows 不设置 ACL; 非回环 HTTP 传 token 无 TLS | 本 PR 未改; 单用户默认场景不新增 ACL/TLS 架构 |
| 7 | P3 | `/ui-config.json` 免鉴权返回服务 host/port, 不含 token | 保留前端 bootstrap 设计 |
| 8 | P3 | 部分 API 的 `str(error)` 和配置路径会回显内部路径, 仅持 token 调用方可达 | 未改, 不在本轮授权范围 |
| 9 | P3 | 文件注册 debug 日志打印完整回调 token URL | 日志改为 token 前 8 位加掩码 |
| 10 | P3 | `SatrapFileTokenService` 映射无 TTL/数量限制, 回调 URL 可留在平台记录中; `/api/file/` 路由未实现 | 注册增加 24h TTL 与 1024 条上限并校验来源; 不宣称回调文件服务可用 |
| 11 | P3 | 主 Bearer token 长期固定, 无轮换 API; 前端从 fragment 读取后抹除 token | 未改; fragment 不进入 Referer/服务端请求日志 |
| 12 | P3 | Misskey 显式允许不安全下载时可对同源实例跳过证书校验 | 保留默认关闭、同源与重定向约束; 未改 |
| 13 | P3 | `satrap_coding/tools/file_core.py` 的用户正则 `re.search` 无超时, 可能阻塞工具线程 | 未改; 不安装新依赖, 不添加线程补丁 |
| 14 | P3 | agent spawn payload 含模型 key, 部分调用允许不安全 base URL | 本 PR 未改该传递路径 |
| 15 | P3 | 平台成员可用审批斜杠命令改变自己会话的模式; shell 始终 ASK, 系统保护目录仍独立拒绝 | 保留已有边界, 不等于远程成员可批准本地执行 |
| 16 | P2 | `group_admin` 开启写操作且 `allowed_callers` 为空时跳过成员检查, 未接逐次人工审批 | 空调用者列表拒绝写操作; 可配置高危动作逐次审批, 无通道时拒绝 |
| 17 | P3 | 群窗口合成的 `[用户 ..., 消息 ...]` 文本标记可由成员正文伪造, 但不能改变冻结的调用来源 | 未改提示词格式, 不属于本轮修复 |
| 18 | P3 | 群管理只读工具默认不限成员, 可将名册带入模型上下文 | 新增可选 `allowed_read_callers`, 留空仍保持不限成员 |
| 19 | P3 | OneBot 合并转发展示名可由工具参数指定, `uin` 固定机器人账号但昵称可误导 | 未改转发展示语义 |

### 原始可达性与限制

问题 1 的本地读取/上传原语已由源码确认。远程成员提供本地路径, 再经管线或模型组装回回复链的完整攻击路径未复现, 不把推测写成已完成利用。

问题 3/4 的已有审批边界仍保留: 无本地 `user_input_provider` 即拒绝代码执行或 shell 执行。环境变量过滤不是进程隔离, 也不阻止已批准的代码读取配置文件。

问题 16 依赖用户主动开启写开关。原有适配器群范围检查、请求 flag 归属/状态/TTL 账本和冻结调用来源未移除。人工审批在插件配置开启时执行, 不替代这些权限检查。

## 当前修复与行为契约

### 媒体来源与配置

- `core/utils/paths.py` 统一分类 HTTP(S)、Base64、Data URL、裸路径与 `file:` URI; 所有本地来源先校验授权, 不依据文件是否存在决定是否检查
- URI 用标准 parser 和 percent-decoding 解析, 生成端用 `Path.as_uri()`; 中文、`#`、字面 `%20` 文件名可以往返, 无效 URI 与越界来源统一抛 `MediaSourcePermissionError`
- 默认目录按实际存储布局识别, 不因任意祖先包含 `uploads` / `cache` 就放行; 校验和消息临时缓存共用后端实际 `StorageLayout.root`
- 配置只接受字符串列表或 null, 缓存键直接使用路径元组, 不拆分 Windows 路径中的分号
- 消息组件、token 注册、OneBot 专用文件上传/消息段封装及转发视频、Misskey 上传来源解析均接入校验; 权限拒绝不能被属性回退吞掉
- 保留 `File.get_file(True)` 的 URL 优先, 普通获取的本地文件优先与白名单内缺失文件的下载回退; 同步属性的普通下载失败仍记录日志并返回空字符串, 权限拒绝向外传播; 保留 Misskey 通用 `get_file` 对象接口
- 白名单内不存在的路径仍可交给独立 OneBot 服务处理, 不强制 Satrap 与 OneBot 共享文件系统

默认授权、显式覆盖、环境变量追加和下载缓存要求只有一个说明来源: [媒体来源白名单](../getting-started/configuration.md#媒体来源白名单)。回归直接运行真实默认策略, 不再替换授权列表而掩盖祖先目录放行。

### 会话模块加载

- 扫描目录只追加到 `sys.path` 末尾; cwd 顶层文件和非法包路径使用带目录摘要的合成名, 普通包保留真实模块名与相对 import
- 加载前校验已有同名模块和父包来源, 不覆盖标准库或其他来源的模块
- 首次执行失败移除半初始化注册, 更新失败恢复旧模块; `SessionClassConfigManager` 不因用户依赖抛 `ImportError` 再执行一次
- 父包初始化已导入同源子模块时直接复用该次结果, 避免首次扫描重复执行顶层代码

### 子进程环境与审批

- `sanitized_child_env` 按完整尾部分段、忽略大小写识别密钥类名称, 保留 `TOKENIZERS_PARALLELISM` 等正常名称
- allowlist 匹配遵循平台规则: Windows 忽略大小写, Unix 区分大小写
- coding shell 的 allowlist 和文件工具的额外保护目录均按实例保存为 `frozenset`, 空配置不继承其他会话权限; sandbox 独立接受自身插件配置
- 读取、写入、编辑、批量替换、glob 和 grep 共用实例保护检查, 内置保护始终保留; 同步/异步工厂回归覆盖交错与并发执行
- 群管理工具绑定会话, 执行时读取当前审批 provider, 支持安装后注入与替换; 同步通道返回协程时关闭协程并拒绝执行
- 审批前校验目标群范围, 提问显示实际目标, 不以来源群替代显式目标

## 验证记录 (2026-10-02)

使用项目 `.venv/Scripts/python.exe`, 命令设 `PYTHONUTF8=1`。Pyright 显式指定同一解释器, 不安装缺失依赖或加入忽略规则。

### 修复前基线

- 本轮修改前的 coding 安全、媒体边界和消息组件回归: **98 passed**, 3.62s
- 已提交版本 `bafc868` 的全量结果: 2294 passed / 5 failed / 8 skipped; 固定失败为 AMR 附件转换、ASR 音频转换、数据库恢复两个子进程测试、minihttp 413 原因短语断言
- 新增保护目录工厂回归修复前 12 项失败; 改用真实默认媒体策略后 7 项失败, 证实两项问题
- 非法 UTF-8 URI 三项回归在不修改 parser 的情况下通过: `UnicodeDecodeError` 属于 `ValueError`, 已转换为 `MediaSourcePermissionError`, OneBot 分类为 `media_source_denied`

### 当前验证

- 相关回归涵盖媒体边界、消息组件、OneBot、Misskey、会话发现/注册、群管理、环境变量、coding、sandbox、后端启动和配置文档
- 最终 16 个相关测试文件: **347 passed**, 14.83s; 包含 coding 契约与最新临时路径用例
- 完整 PR 的 21 个 Python 实现文件: Pyright **0 errors / 0 warnings / 0 informations**; `git diff --check` 无空白错误
- 最终实现的全量单元测试: **2313 passed / 5 failed / 8 skipped**, 114.53s; 五项失败与已提交版本基线一致
- 首次全量运行另外暴露 4 项契约测试夹具失效, 已去除对旧模块级私有入口的无必要替换, 使用真实保护检查
- 唤醒时序失败在此前全量运行也曾出现, 具体用例不同; 根因未确认

全量失败及较早运行的时序观察:

| 测试 | 实际失败证据 | 与修复前对照 |
| --- | --- | --- |
| `test_attachments::test_amr_voice_is_converted_locally` | 转换结果为空, 当前环境无 `av` | 与基线相同 |
| `test_model_config_service::test_asr_test_endpoint_probes_and_converts_audio` | `ValueError: amr 需要本地转码, 请安装 av 包` | 与基线相同 |
| `test_database_recovery::test_process_exit_after_sql_commit_recovers` | 子进程将 `logging.FileHandler` 替换为函数, 后续 `BaseRotatingHandler` 继承失败 | 与基线相同 |
| `test_database_recovery::test_cross_process_writers_and_stale_cache_publish` | 同一 `TypeError: function() argument 'code' must be code, not str` | 与基线相同 |
| `test_minihttp::test_request_body_over_limit_returns_413` | 实际 `413 Request Entity Too Large`, 测试要求 `413 Content Too Large` | 与基线相同 |
| `test_wake_window::test_deadline_recheck_in_cooldown_reschedules_after_cooldown_not_immediately` (本轮首次全量运行) | 预期 await 1 次, 实际 0 次 | 此前也有唤醒时序失败, 具体用例不同; 根因未确认 |

跳过项包括集成测试、缺少 `av` / `reportlab` 和缺少测试图片。本轮没有改动音频、数据库恢复、minihttp 或唤醒策略实现, 不将这些失败掩盖为全量通过, 也不在本轮顺带修复。

### 验收边界与范围门槛

- OneBot / Misskey 测试证明越界来源在平台 API 调用前拒绝, 合法来源参数保留; 模拟成功回执不证明真实文件送达
- Windows 原生运行验证了路径往返和环境策略; POSIX / UNC parser 分支通过平台模拟覆盖, 未在独立 Linux 系统做整套验收
- 本轮为两条行为路径的局部修复: coding 保护策略和默认媒体授权; 只修改对应实现、调用点、测试和文档, 不重写模块、不新增依赖
- 默认 `staged-refactor` 范围门槛为最多 5 文件 / 200 行; 完整未提交补丁为 **15 文件 / 291 行增删**, 超过门槛, **不能标为通过**; 两条路径涉及多个入口及夹具, 不缩小 diff 视图或忽略测试隐藏范围
- 代码沿用项目开发规范, 补新增函数参数/返回说明, 去掉失真修复记录, 不为存量导入/格式进行全仓清洗

## 审查覆盖与未验证内容

初审风险面全读: 后端服务与鉴权, 当时的平台适配器/存储模块, 消息组件, sandbox/outbound/媒体路径, 会话类加载, 插件加载及主要工具执行层。UI 鉴权入口精读, 其余 UI 与 CLI 采用定向读取和危险模式扫描, 不称为全仓逐行审查。

`1ecb13c` 至 `31604d6` 的增量复审精读请求管线、群管理/OneBot 管理与请求账本、ASR、持久化原语和来源身份; 修改模块结合 diff 与关键区段核对。当前复核范围为完整 PR 差异及其调用方和回归测试, 不重复开展无边界全仓审查。

历史正面证据包括 HTTP 鉴权与精确 CORS、配置脱敏、安全 YAML/SQL 参数化、出站 URL/DNS/重定向检查、请求账本归属与状态检查、冻结调用来源及持久化原子替换。这些是已检查区段的证据, 不构成对未来代码或未覆盖路径的保证。

保留缺口:

1. 未实测媒体外发攻击链、伪造 OneBot 事件或真实平台上传, 未核 aiocqhttp token 校验源码
2. 未比对依赖 CVE 数据库, 未审全部开发/启动脚本和 UI e2e 脚本
3. 未逐分支验收唤醒业务状态机; 较早全量运行的唤醒失败原因未确认
4. 文件回调路由未实现, 注册映射的 TTL 和容量限制不代表文件服务可用
5. 代码 sandbox / shell 仍依赖用户审批, 没有 OS 隔离; 媒体授权与实际读取之间也不是原子的文件系统隔离

剩余历史项维持上表状态, 本轮不扩展到依赖、OS 隔离、TLS/ACL 或其他架构修改。
