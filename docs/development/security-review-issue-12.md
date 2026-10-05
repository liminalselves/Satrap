# 安全审查与修复报告 (issue #12 / PR #15)

## 范围与当前结论

- 分支: `review/issue-12`; 初审基线 `1ecb13c`, 2026-09-26 增量复审基线 `31604d6`
- 当前状态: 2026-10-03, 已提交版本 `a153d5a`; 本轮处理 review `5400653004` 的实际沙箱保护遗漏及 TUI Demo 入口, 尚未提交或推送
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

### Coding 全配置与安装生命周期

`a153d5a` 已修复配置保存与执行入口脱节: 全局路径串会话、状态根未接配置和 Shell 默认超时失效。本轮根因是文件保护另读宿主沙箱, 没有复用免审批所用的实际沙箱解析; 默认与配置沙箱仍被 `.satrap` 保护误拒绝。

| 声明项 | 实际消费与本轮结论 |
| --- | --- |
| `workspace_root` | 工具实例基线, 文件路径与 Shell cwd 使用; 宿主动态路径优先, 不写可变全局 |
| `sandbox_root` | 文件写审批使用实例沙箱; 宿主属性优先, 计划模式与保护规则不能被免审批绕过 |
| `data_root` | 三类工厂传同份配置给 state; 权限、目标与日志实际落盘, 已绑定数据根不得原地更换 |
| `shell_timeout` | 同步/异步实例默认值, 显式调用覆盖; 默认 120, 1-3600 整数, 模型定义仅 command 必填 |
| `protected_dirs` | 六类文件工具的实例级不可变集合, 空配置仅保留内置保护 |
| `allowed_env_vars` | Shell 实例级不可变集合, 子进程过滤入口消费; base_take 沙箱独立接受自身配置 |

配置选择顺序与默认目录只有一个面向使用者的说明来源: [Coding 插件](../plugins/satrap-coding-plugin.md#插件配置)。本轮没有移动旧文件, 不添加失效常量的兼容占位。

同步/异步实际安装回归验证目标命令与注入处理器一致, `/plan` 使用工具持有的同一引擎, 工厂顺序不分裂状态, 卸载换目录重装清除内存状态但保留文件。新增失败安装用例确认旧注册状态会残留, 已把现有 cleanup 接入回滚; 已安装插件的重复安装报错不会调用该清理。

另外确认文件写入审批期间进入计划模式或改绑工作区时仍会继续写入, 已为 write_file、edit_file、search_replace 的两版入口补执行前复核。沙箱内计划模式旁路和 Shell 模型参数必填矛盾均有专属回归, 不只是静态推断。

文件保护现在与免审批共用 `_session_sandbox_root(session, tool.sandbox_root)`; 例外只限工作区与实际沙箱的交集。工作区可与沙箱相同或互相包含, 沙箱内敏感目录及工作区到沙箱之间的非 `.satrap` 保护目录仍拒绝。默认、配置与宿主优先级覆盖普通/embedded 两类会话及同步/异步六类文件工具。

TUI Demo 通过安装配置传入工作区和自己的沙箱, 不再改全局路径; 自定义工作区不会顺带扩大免审批范围。同步修正已移走的记忆字段与离线模型 `video_urls` 签名。使用 AST 提取实际入口、真实会话/工具和隔离数据库验证默认写入、中文读取、计划拒绝及外部工作区审批; 终端渲染与完整启动因缺少 `rich` / `prompt_toolkit` 未验收, 没有安装依赖。

## 验证记录 (2026-10-02 至 2026-10-03)

使用项目 `.venv/Scripts/python.exe`, 命令设 `PYTHONUTF8=1`。Pyright 显式指定同一解释器, 不安装缺失依赖或加入忽略规则。

### 修复前基线

- 本轮起点 `a153d5a`: 工作区干净; coding 配置、安全与契约三文件 **106 passed**, 8.28s
- 新增 36 项实际沙箱矩阵修复前 **32 failed / 4 passed**: 默认/配置来源全部误拒绝, 宿主来源仅工作区完全相同可用
- `a153d5a` 历史验证为相关 490 passed / 1 skipped, 全量 2356 passed / 8 failed / 8 skipped; 后者含下表五项固定失败、检查点排序和两个唤醒时序失败

### 当前验证

- 九个相关测试文件 **281 passed**, 18.97s, 包含本轮 36 项组合回归; 覆盖六类文件工具、敏感目录/祖先、宿主优先级、工作区交集、其他运行数据和计划模式
- 完整 PR 的 27 个 Python 实现文件及配置回归文件: Pyright **0 errors / 0 warnings / 0 informations**; `git diff --check` 无空白错误
- 手动 Demo: 离线模型接口错误已清除, Pyright 仍有 **7 项缺失导入** (`rich` / `prompt_toolkit`); 实际入口业务的隔离验证通过, 不是终端界面验收
- 最终全量 **2395 passed / 5 failed / 8 skipped**, 111.86s; 仅下表五项固定失败。唤醒和检查点本次未失败, 不因此宣称根因已修复

全量失败及较早运行的时序观察:

| 测试 | 实际失败证据 | 与修复前对照 |
| --- | --- | --- |
| `test_attachments::test_amr_voice_is_converted_locally` | 转换结果为空, 当前环境无 `av` | 与基线相同 |
| `test_model_config_service::test_asr_test_endpoint_probes_and_converts_audio` | `ValueError: amr 需要本地转码, 请安装 av 包` | 与基线相同 |
| `test_database_recovery::test_process_exit_after_sql_commit_recovers` | 子进程将 `logging.FileHandler` 替换为函数, 后续 `BaseRotatingHandler` 继承失败 | 与基线相同 |
| `test_database_recovery::test_cross_process_writers_and_stale_cache_publish` | 同一 `TypeError: function() argument 'code' must be code, not str` | 与基线相同 |
| `test_minihttp::test_request_body_over_limit_returns_413` | 实际 `413 Request Entity Too Large`, 测试要求 `413 Content Too Large` | 与基线相同 |
| `test_session_checkpoint::test_auto_checkpoint_rollback_restores_stable_state` (历史) | `a153d5a` 全量回滚后多出 assistant 回复; 水位 1/2 的 created_at 均为 `1790993882.2631562`, ID 排序把水位 2 放前 | 本轮未出现; `StateStore` 无 PR 差异, 不经过插件/工具, 未修排序 |
| `test_wake_window::test_max_wait_applies_to_necessity_mode` (历史) | `a153d5a` 全量预期 await 1 次, 实际 0 次 | 本轮未出现; 其他历史运行有 max_wait_enqueues 失败, 根因未确认 |
| `test_wake_window::test_deadline_recheck_in_cooldown_reschedules_after_cooldown_not_immediately` (历史) | `a153d5a` 全量同样预期 await 1 次, 实际 0 次 | 本轮未出现, 没有声称已修复 |

跳过项包括集成测试、缺少 `av` / `reportlab` 和缺少测试图片。本轮没有改动音频、数据库恢复、minihttp 或唤醒策略实现, 不将这些失败掩盖为全量通过, 也不在本轮顺带修复。

### 验收边界与范围门槛

- OneBot / Misskey 测试证明越界来源在平台 API 调用前拒绝, 合法来源参数保留; 模拟成功回执不证明真实文件送达
- Windows 原生运行验证了路径往返和环境策略; POSIX / UNC parser 分支通过平台模拟覆盖, 未在独立 Linux 系统做整套验收
- 本轮结构决策为 `Local Fix`, 执行范围按 `Staged Refactor` 控制: 文件保护复用实际沙箱解析, Demo 通过已有工厂配置传值; 保持数据格式、加载协议和框架职责
- 预算为最多 **5 文件 / 200 行增删**, 包含路径实现、组合回归、Demo 和两份文档。禁止依赖安装、媒体策略、音频、数据库恢复、HTTP、检查点和唤醒实现变更; 不缩小差异视图隐藏测试或文档
- 修复前 106 项基线和矩阵失败为门禁, 如出现新实现回归用定向编辑收窄; 本轮未执行 git 写操作或外发 GitHub 评论, 未新增依赖、兼容层、迁移和模块重写

## 审查覆盖与未验证内容

初审风险面全读: 后端服务与鉴权, 当时的平台适配器/存储模块, 消息组件, sandbox/outbound/媒体路径, 会话类加载, 插件加载及主要工具执行层。UI 鉴权入口精读, 其余 UI 与 CLI 采用定向读取和危险模式扫描, 不称为全仓逐行审查。

`1ecb13c` 至 `31604d6` 的增量复审精读请求管线、群管理/OneBot 管理与请求账本、ASR、持久化原语和来源身份。`a153d5a` 前完成的 PR 差异复核为 50 文件的改动区段 (27 Python 实现、14 测试、9 schema / 文档), 不是文件整篇或全仓逐行审阅。

本轮为 review `5400653004` 的聚焦复核, 起点 `a153d5a`, 精读实际沙箱解析、六类保护调用方、同步/异步审批、真实工厂与手动 Demo。新增矩阵和最终差异均审阅; 27 实现文件类型检查和全量测试是回归证据, 不冒称再次全仓审查。下表保留 `a153d5a` 前的覆盖记录, Demo 入口本轮增补为 reviewed。

| 覆盖状态 | 文件或边界 | 本轮证据 |
| --- | --- | --- |
| reviewed | `core/utils/{paths,proc_env,sandbox}.py`, `core/components/message.py`, OneBot `adapter.py/onebot_utils.py`, Misskey `misskey_utils.py`, BackendManager 与 config/loader | 全部 PR diff 与相关媒体解析、发送、实际存储根、子进程入口; 真实默认策略和模拟平台回归 |
| reviewed | SessionClassManager、session_discovery | 完整加载 diff 与可信来源/模块命名/父包/失败注册路径; 发现和类注册测试 |
| reviewed | base_take 的 meta/tools, group_admin 的 meta/tools | 配置声明、工厂、审批通道与目标群检查, Shell/sandbox 环境入口; 来源身份和平台调用方定向检查 |
| reviewed | satrap_coding 的 meta、state、commands、handlers、tools 改动文件, sync/async_plugins | 六项配置全链, 同一状态消费、文件免审批和计划模式、模型工具定义、卸载与失败回滚 |
| reviewed | 14 个变更测试文件; config.example.yaml、docs/README、configuration、platforms、coding 文档和本报告 | `a153d5a` 前全部改动区段, 失效夹具与无宿主配置用例; 本轮只更新相关矩阵、配置说明与报告 |
| reviewed | 本轮 `tests/manual/tui_demo.py` | 实际安装配置、模型接口、状态读取与工作区显示; 业务入口通过, 完整终端渲染未验收 |
| partial | 未改调用方 `plugin.py/plugin_config.py/plugin_settings.py/plugin_runtime.py`, ChatService、SessionManager、TCBuilder、读工具 wrappers、core permission/goal、Misskey adapter、OneBot admin | 按上述行为链读取相关区段, 不审其无关分支或宣称全模块验收 |
| unread | 库存内无未读改动区段; 库存外代码不作为覆盖结论 | 无全仓完整性保证 |
| excluded | 本轮库存没有二进制、生成物、vendor 或迁移; UI/e2e、开发脚本、依赖 CVE、真实平台上传及无关失败根因排除 | 这些没有被测试通过数或静态检查替代 |

历史正面证据包括 HTTP 鉴权与精确 CORS、配置脱敏、安全 YAML/SQL 参数化、出站 URL/DNS/重定向检查、请求账本归属与状态检查、冻结调用来源及持久化原子替换。这些是已检查区段的证据, 不构成对未来代码或未覆盖路径的保证。

保留缺口:

1. 未实测媒体外发攻击链、伪造 OneBot 事件或真实平台上传, 未核 aiocqhttp token 校验源码
2. 未比对依赖 CVE 数据库, 未审全部开发/启动脚本和 UI e2e 脚本
3. 未逐分支验收唤醒业务状态机; 历史时序和检查点同时间戳排序缺陷保留, 本次全量未出现不等于已修复
4. 文件回调路由未实现, 注册映射的 TTL 和容量限制不代表文件服务可用
5. 代码 sandbox / shell 仍依赖用户审批, 没有 OS 隔离; 媒体授权与实际读取之间也不是原子的文件系统隔离

剩余历史项维持上表状态, 本轮不扩展到依赖、OS 隔离、TLS/ACL 或其他架构修改。
