# satrap_coding 插件: 简易 Coding Agent

`satrap_coding` 是官方预设目录 (`satrap/expend/plugins`) 下的目录插件, 把 SimpleSession / AsyncSimpleSession 扩展成一个可用的 Coding Agent: 文件读写、shell、子代理, 以及目标 / 计划两种工作模式。安装后能力自动注册进会话, 卸载时全量回收。

> 搜索 (search/fetch_page) 与代码沙箱 (code_sandbox) 由 `base_take` 提供, 长期记忆由独立 `memory` 插件提供; 两插件在同一会话内共享私有 sandbox。

## 安装

`satrap_coding` 位于官方预设目录, 可一键安装全部预设插件, 也可以手动安装:

```python
from satrap import SimpleSession
from satrap.edictum.plugin import install_all_plugins

session = SimpleSession("conv-1", llm, db_path="chat.db")
install_all_plugins(session)   # 安装全部预设插件 (含 satrap_coding)

# 或只安装单个插件
session.install_plugin("./satrap/expend/plugins/satrap_coding")

plugin = session.list_plugins()[0]
plugin.name          # "satrap_coding"
plugin.tools         # 11 个工具 (名称 -> 独立启用状态)
plugin.commands      # goal / plan
plugin.skills        # goal / plan
plugin.handlers      # satrap_coding.inject
plugin.capability_descriptions   # meta.yaml 声明的五类能力描述 (kind -> {名字: 描述})
plugin.list_capabilities()       # 每项能力带 name / enabled / description

session.uninstall_plugin("satrap_coding")   # 全部回收
```

异步版 `AsyncSimpleSession` 同样支持 (`await session.install_plugin(path)`)。

## 能力清单

| 类别 | 能力 | 说明 |
| --- | --- | --- |
| 文件 | read_file / write_file / edit_file / search_replace / list_dir / glob_files / grep_files | 工作区内操作, 外部单文件读取与写类按下表审批 |
| 询问 | ask_user | 获取缺失信息, 建议提供 2-3 个推荐选项, 未配置输入通道时返回占位说明 |
| 执行 | shell | 本机 Windows PowerShell/cmd 或 POSIX bash/sh, 默认按系统选择, 按三档模式审批, 计划模式禁用 |
| 任务 | todo_write | 会话级任务清单: add / done / list / clear |
| 子代理 | subagent | 独立上下文, 继承主会话工具与审批策略 |
| 命令 | /goal /plan | 目标与计划模式的用户侧接口 |
| 技能 | goal / plan | 同命令的面向 Agent 的技能指令 |
| 处理器 | satrap_coding.inject | 用户消息进入模型前注入目标块 |

> `ask_user` 需要宿主特殊适配: 宿主必须设置 `session.user_input_provider`, 展示工具提出的问题, 等待用户回答并把回答回填到当前工具调用。未适配时工具只返回占位说明, 不会自动暂停并跨消息恢复

### Shell 平台选择与生命周期

`shell` 参数默认 `auto`: Windows 使用 PowerShell, POSIX 优先 bash, 缺少 bash 时使用 sh。也可显式选择 `powershell`、`cmd`、`bash` 或 `sh`; 不支持的系统组合和缺少解释器会明确报错。审批记录包含实际解释器绝对路径、完整 argv 和工作目录, 执行使用同一份参数。

bash/sh 非交互运行, 不读取个人登录配置或继承 `BASH_ENV`/`ENV` 启动脚本。输出使用 UTF-8, stdin 关闭, stdout/stderr 分别持续排空且每路只保留最多 80 KB 的尾部, 返回正文最多 20,000 字符, 另附状态和截断说明。POSIX 执行使用独立进程组, 超时和取消会回收该组内后代; Shell 提前退出但后代仍持有输出管道时也受总超时限制。异步取消会等待实际回收后再结束调用。主动脱离进程组的程序不在此清理保证内。

命令分级支持 POSIX 引号、换行和常见嵌套 Shell, 动态展开保守按高风险处理。它提供审批辅助, 不是操作系统沙箱; 已批准的本机命令仍具有运行进程的系统权限。

## 三档审批

审批策略由宿主统一持有, /approve 和 approval_model 已迁至独立的 [permissions 插件](permissions-plugin.md)。先启用该插件, Chat 可在「对话设置 → 对话 → 执行审批模式」切换当前会话模式, 与 `/approve mode` 共用同一份状态, 保存后立即生效并持久化; 默认 `user`:

| 操作 | user | auto | full-access |
| --- | --- | --- | --- |
| 工作区及当前会话上传/沙箱内普通文件读取 | 直接执行 | 直接执行 | 直接执行 |
| 实际会话沙箱内普通文件修改 | 直接执行 | 直接执行 | 直接执行 |
| 工作区其它文件修改 | 人工审批 | 独立模型审批 | 直接执行 |
| 本机 Shell | 人工审批 | 独立模型审批 | 直接执行 |
| 显式外部单文件读取 | 人工审批 | 独立模型审批 | 直接执行 |
| 外部目录枚举, 搜索和文件修改 | 拒绝 | 拒绝 | 允许, 修改仍受计划模式限制 |
| 硬保护路径, 黑名单命令, 已禁用或失效工具 | 拒绝 | 拒绝 | 拒绝 |

`/plan on` 独立于三档审批, 始终禁止文件修改和所有 Shell, 保留普通文件读取
Shell 运行在本机进程权限下, 没有操作系统文件或网络隔离; 命令分类只用于风险提示和硬拒绝, 不能证明命令只读
因此 `user` 和 `auto` 对所有 Shell 创建审批请求, 不采用命令首词白名单免审批

### 单次精确动作

审批请求绑定会话, 轮次, 真实调用者, 工具, 权限版本和十分钟有效期
文件包含实际修改前后内容及 diff, Shell 包含完整命令, argv, 解释器, cwd, 超时和环境变量放行名称
审批等待后及实际副作用前重新核验模式, 原始文件字节, 路径, 工具安装启用状态, 平台账号与路由代次
原文件或宿主边界改变时取消执行, 不用旧批准覆盖外部修改
这些核验用于避免等待期间的普通状态变化, 不代替操作系统沙箱或抵御恶意宿主进程的并发文件替换

`read_file` 与 `base_take.read_document` 共用 `core/file_access/` 的文件边界。外部读取审批固定真实路径, 文件指纹和精确分页/解析参数, 不提前读取正文; 每次调用重新审批, 不持久授予文件或父目录权限。异步工具先在事件循环完成审批, 再在线程内复核和解析, 读取后变化也会丢弃结果。目录枚举, glob/grep 和写入的原有范围不变。

### auto 的独立审查

默认使用当前模型客户端; permissions 插件的 `approval_model` 可指定宿主模型表中的配置名称
请求使用独立消息列表, 不注册工具, 不运行主 Agent 或子代理循环
证据包含有界主会话及实际执行工作流的用户消息, 可见助手消息和工具调用结果, 不读取隐藏思考
执行协议在工具边界冻结本轮尚未提交的可见消息, 嵌套子代理保留主代理证据入口; 作用域结束即撤销, 不为了审批提前写入历史
子代理证据单独标记, 不能冒充主会话用户的授权
模型必须返回严格 JSON `{"decision":"allow|deny|ask","reason":"依据"}`:

- `allow`: 真正批准当前精确动作
- `deny`: 不执行, 返回 `approval_denied` 与具体原因; 主模型应选择实质上更安全的做法或向用户澄清, 不应变换写法绕过
- `ask`, 格式错误, 模型异常或六十秒超时: 转人工
- 无人工通道: 返回 `approval_required`, 不执行

同步模型调用在有界等待线程中完成; 超时后晚到的模型结果不能授权动作, 网络请求本身仍受模型客户端超时控制
超过模型审查材料限额的动作直接转人工, 不截掉实际修改后自动批准
审批日志保存动作摘要, 决定, 审查来源和原因, 不落盘完整文件与对话

### 人工回填与宿主权限

`ask_user` 只负责询问信息, 不再授予执行权限
宿主通过 `session.approval_provider` 接收不可变 `ApprovalRequest`, 返回绑定该请求的 `ApprovalResult`
Chat 复用带独立 request_id 的待回答回填链路, 使用专用审批卡片; 命令或文件操作详情默认折叠, 不把模型审查使用的对话证据作为卡片正文。Shell 展开后显示本次命令, 解释器和工作目录, 可进一步查看固定参数。卡片只提供 `批准本次` 和 `拒绝`, 无自由输入或跳过入口; 后端拒绝非选项回答, 重连快照保留卡片类型和实际动作
普通 `y`, `all` 或后续群聊消息不能批准操作, 也不能隐式切换模式
平台或嵌入式宿主未接入专用回调时, 人工审批返回 `approval_required`, 不假装已挂起并可跨消息自动恢复

原生命令:

```text
/approve mode user
/approve mode auto
/approve mode full-access
/approve status
/plan on
/plan off
/goal <目标描述>
```

本地 Chat 允许用户通过真实原生命令切换模式
已获 permissions 插件授权的宿主管理员可以通过平台调度器前置检查; 旧 command_operators 名单不会替代最终插件授权
平台来源还必须获得 `permissions.approval_manage` 宿主管理权限; 系统管理组按 permissions 插件授权范围判定
QQ 群管理员或群主身份本身不授予机器执行权限, 模型和子代理也不能自行提高模式
模式保存于当前会话宿主审批目录, 子代理使用相同共享引擎; 不保存一次性批准
旧版无版本状态, 包括 `auto-agent`, `full` 和操作类别级宽泛规则, 原字节备份为 `permissions.legacy-v1.json` 后迁移为 `user`
旧 `rule/rules` 放行机制及通过 `all` 改模式的行为已移除, 必须明确选择新模式

`/goal` 仍负责持续目标注入, 不授予工具执行权限
`/plan` 的文件写入限制不影响 memory 插件的元信息管理

## 工作区与免审批语义

插件不再提供独立 sandbox 工具 (与 shell / 文件工具重复, 已移除)。平台运行时注入会话私有 sandbox; Chat 项目会话可以操作外部工作区, 但插件缓存和沙箱仍属于当前会话。

文件/shell 工具在调用时优先读取宿主注入的 `plugin_host.environment_snapshot().workspace_root`; 未注入时使用安装期保存在工具实例上的 `workspace_root`, 再回落项目根。其他会话的安装或空配置不会修改已有工具的工作区。文件写操作审批后会复核计划模式、工作区和目标路径, 审批期间发生变化则取消执行。

免审批范围:

- 文件和目录只读工具访问工作区内路径
- 文件写工具修改有效沙箱内的非保护路径; 沙箱按宿主环境的 `sandbox_root`、工具实例 `sandbox_root`、固定默认目录依次选取, `/plan on` 仍拒绝所有文件写入

文件保护与免审批共用实际沙箱的选择规则, 包括宿主属性、插件实例配置和固定默认值。工作区与沙箱可以相同或互相包含; 位于系统数据目录时, 只有同时属于工作区和实际沙箱的普通文件获得例外, 不因其祖先是系统 `.satrap` 而误拦截。沙箱内 `.env`、`.git`、`.satrap`、`node_modules` 和额外保护目录仍拒绝, 其余运行数据不被放行。

以下情况需要审批或直接拒绝:

- user/auto 下沙箱外文件修改和所有 Shell 命令进入对应审批
- 环境修改类命令 (pip install 等)
- 黑名单命令直接拒绝
- user/auto 的文件工具拒绝工作区外路径; full-access 可访问显式外部路径, 仍遵守文件硬保护

`ask_user` 工具建议模型提供 2-3 个推荐选项 (options 参数, 编号展示), 用户可输入序号选择。执行本机程序走 `shell`, 文件读写走文件工具; 已授权 Shell 仍具有宿主进程的实际权限。`code_sandbox` 由 base_take 提供, 继续使用独立 Python worker, 库白名单和网络能力限制, 不受这里的三档模式控制。

## 数据目录

目标命令和目标注入处理器共用 coding 数据根。宿主 session_root/cache/satrap_coding 优先, 否则使用插件的 data_root, 再按会话数据库目录推导。coding 目录只持有 goal.json, 审批模式和日志由宿主统一维护, 默认位于 session_root/cache/permissions。

旧 permissions.json 和 approval_log.jsonl 在目标不存在时复制到宿主目录, 原文件保留。显式配置旧数据根时继续识别该目录; 新配置优先。一次性操作批准不会持久化。

## 卸载与隔离

`uninstall_plugin` 回收全部工具 / 命令 / 技能 / 处理器并调用插件清理回调, 重置 coding 内存状态 (计划模式 / 注入缓存), 保留宿主审批模式, 不遗留孤儿; 目标等数据文件保留, 重装后数据仍在。插件级共享状态按会话隔离 (state.py), 同一会话的工具、命令、处理器共享同一份宿主审批引擎和插件目标状态。安装失败时回滚已注册能力与 sys.path, 并调用清理回调释放已构建的共享状态; 重复安装已安装插件只报错, 不清除其正常状态。

## 插件配置

meta.yaml 声明 `config_schema`, 支持以下配置项 (全局默认 + 按会话覆盖两级):

| 配置键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| workspace_root | path | 项目根 | 文件工具白名单根目录 |
| data_root | path | 会话缓存 | 目标数据根, 选择规则见“数据目录” |
| sandbox_root | path | 项目根/.satrap/sandbox | 文件免审批目录; 宿主会话路径优先, 内部敏感路径和计划模式仍受保护 |
| shell_timeout | number | 120 | 1-3600 的整数秒; 按工具实例保存; 模型调用可省略 timeout, 单次调用可覆盖; 工厂拒绝超出范围或非整数的生效值 |
| protected_dirs | string | - | 额外保护目录名 (逗号分隔, 忽略大小写); 按工具实例保存, 空配置仅保留内置保护 |
| allowed_env_vars | string | - | Shell 子进程显式放行的环境变量名 (逗号分隔); 其他密钥类变量默认剥离 |

安装时经 `install_plugin(path, config={...})` 传入会话级覆盖; 全局默认存于 `.satrap/config/plugins/satrap_coding.json`。配置在安装时解析, 不支持原地改绑数据根; 更改配置须卸载后重装, 防止已有工具、命令和处理器指向不同状态。卸载不搬迁或删除持久文件。
