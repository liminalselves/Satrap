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
plugin.commands      # goal / plan / approve
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
| 文件 | read_file / write_file / edit_file / search_replace / list_dir / glob_files / grep_files | 工作区白名单内操作, 写类免审批/审批 |
| 询问 | ask_user | 获取缺失信息, 建议提供 2-3 个推荐选项, 未配置输入通道时返回占位说明 |
| 执行 | shell | 本机 PowerShell/cmd, 每次执行需明确批准, 计划模式禁用 |
| 任务 | todo_write | 会话级任务清单: add / done / list / clear |
| 子代理 | subagent | 独立上下文, 继承主会话工具与审批策略 |
| 命令 | /goal /plan /approve | 目标、计划模式与审批策略的用户侧接口 |
| 技能 | goal / plan | 同命令的面向 Agent 的技能指令 |
| 处理器 | satrap_coding.inject | 用户消息进入模型前注入目标块 |

> `ask_user` 需要宿主特殊适配: 宿主必须设置 `session.user_input_provider`, 展示工具提出的问题, 等待用户回答并把回答回填到当前工具调用。未适配时工具只返回占位说明, 不会自动暂停并跨消息恢复

## 审批模型

写类操作和所有 Shell 命令经过 `PermissionEngine`, 三种策略 (`/approve mode <user|auto-agent|full>`):

- `user` (默认): 需要用户批准, 通过 `session.user_input_provider` 询问; 未配置输入通道则返回"需要用户批准"
- `auto-agent`: 调用会话主模型只读判断, 写类操作的模型批准仍转人工确认, 判断失败同样转为询问
- `full`: 普通操作放行, 任意本机 Shell 仍需逐次批准

询问时用户输入 `y` 仅批准本次操作, 普通操作可用 `all` 授予本会话权限 (不跨会话持久化)。Shell 不接受 `all` 或历史规则自动授权, 审批说明绑定本次命令、解释器和工作目录。审批策略本身 (`/approve mode`) 随规则文件持久化, 跨会话生效。

持久规则 (`/approve rule <操作> <风险级 0-1>`) 按操作类型直接放行低于风险级上限的操作 (最高 1, 高危操作不开放持久放行), 规则保存在插件数据目录的 `permissions.json`。计划模式下写类操作被引擎直接拒绝, 不做询问。

命令分类仅用于风险提示和黑名单拒绝, 不能证明解释器脚本只读。包括 echo、git 在内的所有 Shell 命令均走单次审批; 无输入通道或处于计划模式时拒绝执行。同步和异步执行统一使用 UTF-8。

## 命令

```text
/goal <目标描述>            设置持续目标
/goal status                查看目标与子任务
/goal todo <子任务>         添加子任务
/goal todo-done <序号>      完成子任务
/goal done / clear          完成 / 清除目标
/plan on / off              进入 / 退出计划模式 (工作区写操作全部禁用)
/approve mode <user|auto-agent|full> 切换审批策略
/approve rules              查看持久规则
/approve rule <操作> <风险级 0-1>    添加持久规则 (上限 1)
```

`/goal` 设置的目标由注入处理器自动拼接到后续用户消息头部 (带缓存, 内容变化自动失效), 保证模型每轮都围绕目标推进。`/plan on` 与工具审批引擎共享同一状态: 进入计划模式后 write_file / edit_file 和所有 Shell 命令被拒绝。文件只读工具仍可使用; 长期记忆属元信息, 其增删改 (memory 的记忆工具与 /memory 命令) 不受计划模式拦截, 属有意设计。

## 工作区与免审批语义

插件不再提供独立 sandbox 工具 (与 shell / 文件工具重复, 已移除)。平台运行时注入会话私有 sandbox; Chat 项目会话可以操作外部工作区, 但插件缓存和沙箱仍属于当前会话。

文件/shell 工具在调用时优先读取宿主注入的 `session.coding_workspace_root`; 未注入时使用安装期保存在工具实例上的 `workspace_root`, 再回落项目根。其他会话的安装或空配置不会修改已有工具的工作区。文件写操作审批后会复核计划模式、工作区和目标路径, 审批期间发生变化则取消执行。

免审批范围:

- 文件和目录只读工具访问工作区内路径
- 文件写工具修改有效沙箱内的非保护路径; 沙箱按宿主 `coding_sandbox_root`、工具实例 `sandbox_root`、固定默认目录依次选取, `/plan on` 仍拒绝所有文件写入

文件保护与免审批共用实际沙箱的选择规则, 包括宿主属性、插件实例配置和固定默认值。工作区与沙箱可以相同或互相包含; 只允许同时位于工作区和实际沙箱内的普通文件, 不因其祖先是系统 `.satrap` 而误拦截。沙箱内 `.env`、`.git`、`.satrap`、`node_modules` 和额外保护目录仍拒绝, 其余运行数据不被放行。

以下情况需要审批或直接拒绝:

- 沙箱外文件写入、编辑和所有 Shell 命令
- 环境修改类命令 (pip install 等)
- 黑名单命令直接拒绝
- 文件工具操作工作区外路径 (越界拒绝)

`ask_user` 工具建议模型提供 2-3 个推荐选项 (options 参数, 编号展示), 用户可输入序号选择。执行统一走 `shell` (共享本机全环境, 非安全边界), 文件读写走文件工具。免审批读取仅通过文件工具并复用工作区与敏感路径保护; 任意已授权 Shell 程序仍具有当前进程的文件访问权限。`code_sandbox` 仅限制工作目录, 不构成操作系统隔离, 因此 `run` 和 `run_file` 没有明确用户授权时默认拒绝。沙箱路径越界和删除沙箱根目录始终拒绝。

## 数据目录

权限引擎、目标命令和目标注入处理器共用同一数据根。选择顺序为宿主注入的 `coding_cache_root/satrap_coding`、显式插件 `data_root`、当前会话的默认缓存目录。默认目录为会话数据库旁的 `sessions/<storage_key(session_id)>/cache/satrap_coding`; 无数据库的 embedded Session 使用项目根下 `.satrap/data/unscoped/<storage_key(session_id)>`。路径键由框架生成, 不直接拼接未经处理的 ID。

```text
<data_root>/
├── permissions.json     # 持久审批规则
├── approval_log.jsonl   # 审批记录
└── goal.json            # 目标与子任务状态
```

> 长期记忆由 memory 插件管理并写入当前平台的 `platform.db`; 沙箱, 索引和缓存全部按会话隔离。旧数据不迁移。

## 卸载与隔离

`uninstall_plugin` 回收全部工具 / 命令 / 技能 / 处理器并调用插件清理回调, 重置内存级状态 (计划模式 / 审批记忆 / 注入缓存), 不遗留孤儿; 目标等数据文件保留, 重装后数据仍在。插件级共享状态按会话隔离 (state.py), 同一会话的工具、命令、处理器共享同一份权限引擎 / 目标状态。安装失败时回滚已注册能力与 sys.path, 并调用清理回调释放已构建的共享状态; 重复安装已安装插件只报错, 不清除其正常状态。

## 插件配置

meta.yaml 声明 `config_schema`, 支持以下配置项 (全局默认 + 按会话覆盖两级):

| 配置键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| workspace_root | path | 项目根 | 文件工具白名单根目录 |
| data_root | path | 会话缓存 | 权限、目标和审批日志的数据根, 选择规则见“数据目录” |
| sandbox_root | path | 项目根/.satrap/sandbox | 文件免审批目录; 宿主会话路径优先, 内部敏感路径和计划模式仍受保护 |
| shell_timeout | number | 120 | 1-3600 的整数秒; 按工具实例保存; 模型调用可省略 timeout, 单次调用可覆盖; 工厂拒绝超出范围或非整数的生效值 |
| protected_dirs | string | - | 额外保护目录名 (逗号分隔, 忽略大小写); 按工具实例保存, 空配置仅保留内置保护 |
| allowed_env_vars | string | - | Shell 子进程显式放行的环境变量名 (逗号分隔); 其他密钥类变量默认剥离 |

安装时经 `install_plugin(path, config={...})` 传入会话级覆盖; 全局默认存于 `.satrap/config/plugins/satrap_coding.json`。配置在安装时解析, 不支持原地改绑数据根; 更改配置须卸载后重装, 防止已有工具、命令和处理器指向不同状态。卸载不搬迁或删除持久文件。
