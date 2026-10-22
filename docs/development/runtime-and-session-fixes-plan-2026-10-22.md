# Linux 兼容、配置加载、文件交付与会话边界实施计划

状态: 实施中, 完成情况以末尾执行记录为准
日期: 2026-10-22
范围: 本轮核查发现的问题, 不重新展开此前已经完成的搜索、媒体输入和插件交互迁移

## 1. 已确认的问题与证据

| 问题 | 核查结果 | 本轮处理 |
| --- | --- | --- |
| YAML 空节点导致加载失败 | session_classes、platforms、session_scan_paths 为 null 时构造失败; api 为 null 也报错 | 修复空值与类型校验 |
| 配置失败后静默使用默认值 | 已复现 max_sessions: 37 变为 1000, source_path 丢失, 随后仍打印加载成功 | 启动失败必须明确, 不再悄悄回退 |
| session_scan_paths 空列表语义不一致 | BackendConfig 保留 [], 下游扫描器和 Control 又恢复默认目录 | 保持显式 [] 关闭外部目录扫描的语义 |
| Linux 启动锁 | 原实现直接导入 msvcrt; main 已有 Linux 临时补丁, 本地也有相关暂存修改 | 核对并归并已有补丁, 补真实跨平台验证 |
| coding shell 只支持 Windows | 只接受 powershell/cmd, Linux 下解析到不存在的 Windows 路径; 原真实 shell 测试在非 Windows 跳过 | 接入原生 Linux shell |
| 模型不能统一交付普通文件 | OneBot/Misskey 底层已有上传, 上层目前主要接通群图片; Chat 缺少模型产物下载链路 | 增加统一文件交付 |
| 群档案跨 /new 回读 | 远端 SQLite 快照已确认: /new 切换成功, 模型随后主动调用 group_chat_recent_messages | 新增可强制执行的查询范围配置 |
| /new 保留待处理群窗口 | 远端首轮用户输入确有重置前未消费消息; 本地复现 | 成功切换后建立窗口重置边界 |
| 并发排队使用旧会话 | 本地调度器复现: /new 处理期间已解析路由的消息, 等待后仍执行旧会话 | 在真正执行前重新核对路由 |
| 隔离会话 /history 查询错误 | 从 scope_... 会话 ID 中猜用户身份, 查询不到对应历史; /switch 使用同一历史列表 | 改为可信路由范围查询 |

现场取证报告: 本地忽略目录 .toolkit/satrap-new-audit-20261022-c3bdf17b/analysis.md
现场数据库: 同目录 qq-platform.db, 仅作只读分析, 不提交 Git
远端服务未重启, 配置和业务数据未修改

本次群聊现象的主因不是 Linux 把 /new 执行失败, 而是新会话仍能通过工具读取同一个群档案
配置参数负责限制这条回读通道; 窗口残留、排队路由和历史切换分别修复, 不用一个开关掩盖其他缺陷

## 2. 新配置: group_chat.history_scope

放在现有 group_chat 插件配置中, 前端名称为“群消息查询范围”。
沿用现有插件配置继承和覆盖机制, 不增加新的独立配置文件。
全局、命名配置和会话覆盖仍按现有插件配置优先级合并; 管理界面展示最终生效值及继承来源。
该字段是管理员配置, 不是模型工具参数; 非法枚举值明确报配置错误, 不静默退回更宽范围。

| 值 | 前端显示 | 行为 |
| --- | --- | --- |
| current_session | 仅当前会话 | 默认值, 只允许查询当前真实会话开始后的群档案, /new 后边界推进 |
| group_archive | 当前群全部保留记录 | 保留现有查询能力, 允许读取 /new 前的群记录, 仍受保存期限、群权限和查询预算限制 |
| disabled | 禁止查询群记录 | 不向模型提供群记录及摘要读取工具, 后端也拒绝执行这些读取 |

配置示例, 以下是传给 group_chat 的配置内容:

```yaml
history_scope: current_session  # 只查本次会话开始后的群记录
```

默认选 current_session 是本计划提出的行为变更: 未设置该字段的旧配置也按此值处理。
希望保留旧行为的使用者可以明确设置 group_archive, 变更说明需写清楚。

### 2.1 参数控制什么

1. 控制 group_chat 对群消息档案的读取范围, 由后端执行, 不依赖模型听从提示词
2. 适用于最近消息、搜索、按消息 ID 读取、旧消息取图, 以及读取群摘要和摘要来源的相关入口
3. current_session 仍保留当前群、机器人账号、实际调用者、Agent 路由代次等现有校验, 不授予新的群访问权限
4. 模型参数不能指定或放宽 history_scope, 也不能自行填写会话边界
5. disabled 下同时调整模型工具清单、工具说明和技能说明; 缓存下来的旧调用名仍须由后端拒绝
6. 群资料、成员查询、普通回复、沙箱产物登记和提醒管理保持各自现有权限规则
7. 此参数不是“旧资料保密开关”: 用户本轮重新发送、明确引用或转发的旧内容, 以及独立的长期记忆, 仍按原输入规则处理

查询范围不等于模型调用意图。
current_session 不保证模型永远不会为“喵”查询当前时段的群记录; 要完全关闭这类读取可选 disabled。
同时优化工具和技能文案: “总结上下文”优先解释为当前模型会话, 问候不主动为补背景查询群史; 明确要求回顾群讨论时再使用允许范围内的档案。

### 2.2 current_session 的边界

- 由宿主根据当前实际 session_id 及持久化的会话创建信息确定, 不从用户正文、模型参数或 session_id 的字符串片段猜测
- /new 成功创建并绑定新会话后使用新边界; /switch 回到旧会话时使用目标会话原来的边界
- group 共享会话共用边界, group_member 的不同成员会话各自保存边界
- 边界绑定原始会话、平台来源和本轮执行; 查询等待后复核, 路由切换则拒绝返回失效结果
- 时间边界使用服务端可信接收和会话顺序信息, 处理平台秒级时间、延迟入库和同秒消息, 不能只比较未经校验的平台时间
- 无法确认边界时返回明确的范围不可用错误, 不能默默放宽成 group_archive
- 数据库查询阶段施加范围约束, 再执行条数、正文预算和分页; 不先取全量记录再在 Python 内过滤

### 2.3 不能遗漏的读取入口

- recent_messages 和 search_messages 的查询、coverage、计数与分页游标都绑定有效范围
- get_message 在本地查询或平台回源之前检查边界; 回源结果无法证明属于范围时不返回正文
- get_message_assets 不能经旧消息 ID 读取范围外图片
- prepare_summary、read_summary_sources 的来源快照和游标绑定会话边界, 不能复用重置前快照
- get_summary、list_summaries 不得通过旧摘要正文绕过范围; 按摘要实际来源判断, 不能只看摘要保存时间
- 已保存摘要含范围外来源时不返回其正文, 返回可理解的不可用原因, 不删除原摘要
- 同步、异步、插件间合法调用使用同一服务校验, 不为某一种入口单独放行
- current_session / disabled 下的错误要区分“范围外”“没有记录”和“存储不可用”

### 2.4 配置收紧与已有上下文

配置修改后限制后续查询, 不能宣称已经撤回模型之前读到并写入上下文的内容。
从 group_archive 收紧范围时, 管理界面提示“后续查询已受限, 请 /new 开始不含先前回读结果的新会话”。
本批不自动删除历史, 不自动清空长期记忆, 不修改远端用户配置。

## 3. /new 的配套修复

### 3.1 待处理窗口

- 仅在新会话创建、初始化和持久路由绑定成功后清理
- 撤销对应范围内、重置之前尚未消费的窗口消息
- 撤销该范围旧的延迟唤醒快照和定时任务, 避免过一段时间再次注入
- 明确区分命令前后进入窗口的消息, 保留重置后新收到的消息
- group 模式作用于该群共享范围, group_member 模式不清除其他成员独立窗口
- /new 失败时保留旧上下文和窗口, 返回明确失败
- /help 等普通命令继续保留窗口, 不把所有命令统一改成清空窗口

### 3.2 排队与路由

调度器目前在取得执行锁前解析 session_id。
计划改为按稳定的对话范围串行处理会话切换与发言, 并在获得执行权后复核当前路由。
不能只在旧 session_id 的锁里重新查询后直接操作另一个会话, 否则会绕过目标会话并发保护。
已排队事件的策略和来源也必须复核, 保留原有取消、超时和路由代次保护。

### 3.3 /history 与 /switch

- 从可信调用来源和当前 ConversationRoute 获取归属, 不再解析 scope_... 充当用户 ID
- 为同一路由保存可切换的历史会话关联, 不只依赖 context_sessions 的当前指针
- 成员隔离和群共享采用各自归属, 共享群历史不绑定首位发言者
- /switch 校验目标属于同一允许范围, 不因一个成员知道 session_id 就允许跨群、跨成员或跨 Agent 切换
- 同步、异步共用同一领域操作, 成功回复只在实际创建或切换完成后返回
- 对旧记录仅采用能核验的存储关联做兼容; 无法确认归属的会话不自动纳入历史列表
- /new 不删除旧群档案或旧会话, 原有会话配置继承规则保持不变

## 4. YAML 与配置加载

### 4.1 空值与类型

| 字段 | 缺省 / null | 显式空集合 |
| --- | --- | --- |
| session_classes | {} | {} |
| platforms | [] | [] |
| session_scan_paths | 默认 .satrap/session | [] 表示不扫描外部目录 |
| api | 默认 API 配置, 保持现有平铺字段兼容优先级 | {} |
| workspace_roots | 保持现有默认目录语义 | 保持现有行为 |
| media_allowed_roots | 保持现有默认白名单语义 | 保持现有行为 |

- 先规范化 platforms, 再交给管理员配置校验, 避免管理员配置存在时又迭代 None
- 列表、映射检查真实类型, 不用 list("abc") 或 dict(...) 的隐式转换代替校验
- 不把所有 null 一概替换默认值; 数值、管理员授权、迁移版本和平台 settings 等字段保持各自明确的校验契约
- YAML 与 JSON 共享 BackendConfig 的字段规则
- 空白或纯注释的整个文档统一视为空配置对象, 与 Control 当前文档读取语义对齐; 列表等其他根类型明确拒绝
- 下游扫描器、管理端和 CLI 一并保留 session_scan_paths: [] 的语义

### 4.2 加载失败可见

- 已存在文件解析、读取或校验失败时抛出带实际路径和原因的配置加载错误
- 显式指定不存在的配置文件也报错, 不返回 BackendConfig()
- 未找到任何配置时保留自动生成默认配置的流程
- 不再通过缺少 YAML 依赖等异常偷偷切换到另一份 JSON 配置
- CLI 有显式 --config 时优先使用该文件, 不先自动探测另一份配置
- 排查其他捕获点, 尤其客户端地址探测, 不让同一个配置错误再次变成默认连接地址
- source_path 只在真实文件加载成功时设置; 不为失败回退伪造来源
- 只在成功之后打印“已加载配置”
- Control 启动前预检, 错误状态明确返回配置路径、失败类别和可读原因, UI 显示配置加载失败
- 保留运行宿主来源一致性校验, 不通过放松身份判断消除告警
- 修正 config.example.yaml 的注释空节点为 session_classes: {} 和 platforms: []

## 5. Linux shell 与启动兼容

- 检查已有 backend_lock.py 暂存补丁与 main 的 Linux 补丁差异, 归并而不覆盖未提交工作
- 启动锁在 Windows 使用 msvcrt, POSIX 使用 fcntl, 验证重复启动互斥、退出释放和后续重启
- shell 默认值改为 auto: Windows 保持 PowerShell, Linux 优先 bash, 不存在时使用 sh
- 保留 powershell/cmd, 增加 bash/sh; 不支持的系统组合或缺少解释器时明确报错
- 固定解析出的解释器绝对路径、参数和实际工作目录, 审批与执行使用相同数据
- bash/sh 使用非交互执行, 不依赖个人登录配置, 保持 UTF-8 输入输出和环境变量剥敏
- 同步、异步工具共用解析及执行规则, 沿用 user/auto/full-access、计划模式和执行前复核
- 核对 POSIX 引号、复合命令、嵌套 shell 和危险命令分类; 不宣称字符串分类器是 OS 沙箱
- Linux 验证超时及取消后的进程组回收, 避免只结束 shell 而遗留后代程序
- 输出采集有上限, 避免只在完整捕获后截断显示
- 跨平台 CI 加入 coding 工具真实执行, 不再让所有 Linux shell 测试被 Windows 条件跳过
- CentOS 7 现场使用 Docker 容器内 Python 3.12, 需要验收该运行环境; 不要求宿主 CentOS 系统 Python 安装 Satrap

## 6. 普通文件交付

### 6.1 对外行为

- 在 base_take 提供统一 send_file 工具, 参数为文件路径和可选显示文件名, 默认目标仅为当前会话
- 支持普通二进制文件, 包括 PDF、DOCX、XLSX、ZIP 和图片, 不以“能读文档”代替“能发送文件”
- coding 和 sandbox 继续负责生成文件, 交付通过既有 plugin_host 宿主接口, 不互相导入私有实现
- 沙箱、工作区、上传目录以及目录外单文件读取采用现有文件保护和宿主审批规则
- 相对路径归属明确, 显式目录外文件按既有三档模式审批, 不扩张成整个父目录的访问许可
- 首版单文件上限 32 MiB, 平台更小限制优先; 同步和异步路径一致

### 6.2 读取、缓存和平台发送

- 执行前固定文件及指纹, 有界复制为不可变受控产物, 发送期间持有租约
- 不直接把模型填写的服务器路径传给平台上传 SDK
- 拒绝设备、目录、符号链接越界和受保护文件, 复核本轮真实来源和目标
- 采用流式读取、复制和下载, 不把整个文件编码进模型上下文或 SSE
- 复用既有文件组件、发送回执和媒体来源校验, 不另开匿名服务器目录
- OneBot 复用群/私聊上传接口, 验收 Satrap 与实现端分容器时的真实可达路径或实现支持的上传通道
- 无可用文件上传通道时明确不支持, 不把文件段兼容尝试当作确认送达
- Misskey 复用上传和附件流程, 文件必需上传失败时不能只发送文字后宣称文件成功
- 明确区分已登记、待发送、已上传或已发布、失败和未知; 未知结果不自动重复上传或发送

### 6.3 Chat 下载

- 增加绑定会话和回复版本的不透明附件 ID, 服务器真实路径不作为下载参数
- 增加鉴权的流式二进制下载接口, 使用 Content-Disposition: attachment
- 前端显示文件名、大小和下载按钮, 不自动执行或以内联 HTML 方式预览任意附件
- 刷新、重连和查看历史后仍能下载有效附件
- retry/fork 的不同回复版本不会误绑定另一个版本的产物
- 会话删除、归档、缓存清理和正在发送/下载的租约协调, 不提前删除仍被使用的文件
- 未接入交付接口的宿主明确返回不可用, 不假装已经发出文件

## 7. 测试与验收

沿用现有场景级测试模块, 按完整业务流程整合, 不为每个小分支新增独立测试文件。

| 批次 | 核心场景 |
| --- | --- |
| 配置加载 | 示例文件直接可用; null、空集合和错误类型; 显式路径优先; JSON 一致; 失败停止启动且不误报成功; Control/UI 错误可见 |
| 群记录范围 | 三种 history_scope; /new 前后; /switch 旧边界; 分页、按 ID、平台回源、图片和摘要读取不能绕过; 热收紧后旧调用拒绝 |
| 会话重置 | 旧消息已处理后 /new 的新模型请求不含旧历史; 待处理窗口、延迟任务和并发排队分别覆盖 |
| 会话历史 | group_member、group、legacy_user 的历史关联、重启持久化和越权切换拒绝 |
| Linux shell | 真实 bash/sh 执行、中文、空格路径、工作目录、退出码、三档审批、计划模式、超时、取消和后代进程回收 |
| 文件交付 | coding/sandbox 生成文件后交付 Chat、QQ 私聊/群和 Misskey; 内容哈希一致; 越界、超限、上传失败、取消及未知回执 |
| Chat 附件 | 下载鉴权、跨会话拒绝、刷新重连、回复版本及清理租约 |

Linux shell 与文件交付不能只依赖 mock。
本地使用 Windows 与可用的 Linux 测试环境, CI 覆盖支持的 Python 版本及平台。
远端验收需要明确安排部署和测试窗口, 本计划不包含立即修改或重启现有远端服务。

## 8. 分批实施与预计文件范围

A. 配置加载及启动锁

- satrap/core/backend/BackendManager.py
- satrap/core/config/loader.py, document.py
- satrap/cli/common.py, cmd_run.py, client.py, backend_lock.py
- satrap/core/framework/SessionClassManager.py, session_discovery.py
- satrap/core/backend/control_server.py 与现有前端错误展示接缝
- config.example.yaml、配置文档和现有配置/CLI/Control 场景测试

B. 群记录配置与会话边界

- satrap/expend/plugins/group_chat/meta.yaml, tools.py, skills/group_chat/skill.md
- satrap/core/group_chat/types.py, service.py, summaries.py
- satrap/core/config/platform_messages.py
- satrap/core/pipeline/scheduler.py, wake_window.py, wake_timers.py
- satrap/expend/command/session_commands.py
- satrap/core/framework/SessionManager.py, UserManager.py
- 现有路由、命令、群消息与摘要场景测试, 插件文档

C. Linux coding shell

- satrap/expend/plugins/satrap_coding/tools/shell.py, sync_interaction.py, async_interaction.py
- satrap/expend/plugins/satrap_coding/core/command_gate.py
- tests/unit/test_coding_flows.py, test_coding_security_flows.py
- .github/workflows/code-sandbox.yml、coding 插件文档

D. 文件交付

- satrap/core/plugin_interop/artifacts.py 及必要的类型化宿主产物契约
- satrap/expend/plugins/base_take/tools/ 中新增轻薄工具入口, meta.yaml 注册
- satrap/core/file_access/ 与平台发送接口的现有复用接缝
- satrap/core/platform/onebot/adapter.py, misskey/adapter.py
- satrap/display/service.py, server.py, recorder.py 及必要的附件元数据存储
- satrap-ui/src/api/chat.ts 和 Chat 附件展示组件
- 现有文件权限、群媒体、平台回执、Chat 后端与前端场景测试

批次之间分别验证和提交, 不把所有修改压成一次提交。
B 体量较大时可分为 B1 查询范围、B2 重置窗口与排队、B3 历史切换关联。
文档、注释和静态类型检查与各批实现同步完成, 不以 Any 代替新增契约。

## 9. 执行记录

### A. 配置加载及启动锁

- 已实现空节点归一化、错误类型拒绝、失败抛出 ConfigLoadError、显式配置路径优先和真实 source_path
- 已保留扫描器及 Control 的显式空列表语义, 修正仓库示例并补充配置说明
- Control 在启动和重启前预检, 预检失败不停止旧进程, 启动进程提前退出不再误报启动中
- 已归并原有 Windows/POSIX 锁补丁, 加入真实跨进程互斥、释放后启动及失败竞争不修改实例记录的验证
- 配置与 Control 场景测试: 81 passed; 同批较大范围此前为 227 passed, 1 skipped, 跳过原因是 Windows 无符号链接权限
- WSL Ubuntu 原生 fcntl 验证通过: 第二进程被拒绝, 释放后另一进程可取得锁
- 生产代码静态检查为 0 errors, 既有文件仍有 Unknown 类型警告; 不宣称全仓库零告警
- A 已完成本地验证, 未在远端部署; 其余批次见后续执行记录

### C. Linux coding shell

- 已实现 auto 默认选择、Windows PowerShell/cmd 与 POSIX bash/sh, 固定解释器绝对路径及审批 argv
- Windows cmd 通过隐藏控制台和 UTF-8 后启动的实际解释器解决中文乱码, PowerShell 保留原生命令失败退出码
- bash/sh 非交互运行, 清理继承的启动脚本变量, 固定 UTF-8 locale, 关闭 stdin
- stdout/stderr 持续排空, 每路只保留 80,000 字节尾部, 返回正文最多 20,000 字符并附截断说明
- POSIX 在超时、取消和异常时回收进程组, 异步取消等待真实回收; Shell 先退出但后代持有输出管道也受总超时限制
- 新增 POSIX 引号、换行、嵌套 Shell 分类, 动态展开按高风险处理; 分类器不是 OS 沙箱
- Windows coding 回归: 190 passed, 4 skipped, 58 deselected; 跳过为三个 POSIX 专用清理场景及一个无权限的符号链接场景
- WSL Ubuntu / Python 3.14.4 的 coding、配置与 Control 回归: 275 passed, 58 deselected; 未执行的 58 项为独立沙箱范围, 不是上述 shell 场景
- 本批四个生产代码文件静态检查: 0 errors, 0 warnings; git diff --check 通过
- CI 已加入真实 shell 与配置回归, 矩阵为 Windows/Linux/macOS 及 Python 3.11/3.12/3.13; 尚未触发远端 CI
- macOS 和远端 Python 3.12 容器仍未验收, 未部署或重启远端服务
- B 群聊会话边界与 D 文件交付仍待完成, 不据此标记完整计划完成

### B2/B3. 重置窗口、排队路由与历史切换

- 调度器先取得稳定路由锁, 在锁内解析当前会话, 再取得实际会话锁; 等待后复核来源、绑定和权限
- /new 只有目标初始化及持久路由比较交换成功后才发布重置信号, 失败保留原指针且返回明确失败
- 窗口以真实入队时刻排序, 成功信号采用命令入队边界, 清除此前内容并保留命令之后的消息; /help 不清窗
- 旧定时与手动快照撤销, 后续补偿使用真实后续消息的来源重新安排, 不借用 /new 发话者身份
- 新增 context_session_history 持久关联, 仅从已确认当前映射迁移旧记录, 同步/异步命令均按宿主路由读取历史
- /switch 再次在宿主侧验证归属及实际目标存在性, 拒绝跨群、成员、账号和 Agent 路由范围; 新旧实例配置继承规则保留
- 历史关联接入目录展示、会话归档、恢复、删除、断裂引用维护及一次性清洗器的会话枚举
- 真实 simple/async_simple 覆盖 9 种入口与范围组合, 验证 /new 的模型请求无旧历史, /switch 可恢复旧上下文
- Windows 较大场景回归: 468 passed, 1 skipped; 跳过为当前环境无目录符号链接权限
- Linux 同范围初次为 467 passed、2 failed; 两项失败来自既有 FileHandler 测试桩破坏日志子类导入, 已修正为只替换 _open, Windows/Linux 对应两项重跑均通过
- 最终窗口及命令相关场景在 Windows/Linux 各 253 passed, 包含重排来源身份、失败保留、并发排队和真实 Edictum 会话验证
- 相关生产代码静态检查 0 errors, 37 项既有 Unknown 警告, 不宣称全仓库零告警
- B1 history_scope 和 D 文件交付仍待实施; 本批不会阻止模型通过群档案工具回读旧消息, 不据此宣布现场问题已全部解决
