# satrap_coding 插件: 简易 Coding Agent

`satrap_coding` 是官方预设目录 (`satrap/expend/plugins`) 下的目录插件, 把 SimpleSession / AsyncSimpleSession 扩展成一个可用的 Coding Agent: 文件读写、shell、子代理, 以及目标 / 计划两种工作模式。安装后能力自动注册进会话, 卸载时全量回收。

> 搜索 (search/fetch_page)、长期记忆 (memory) 与代码沙箱 (code_sandbox) 已移交 `base_take` 插件; 两插件共享同一沙箱目录 (`.satrap/sandbox`)。

## 安装

`satra_coding` 位于官方预设目录, 可一键安装全部预设插件, 也可以手动安装:

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
| 执行 | shell | 本机 PowerShell/cmd, 工作区内免审批, 写/高危/环境修改命令走审批 |
| 任务 | todo_write | 会话级任务清单: add / done / list / clear |
| 子代理 | subagent | 独立上下文, 继承主会话工具与审批策略 |
| 命令 | /goal /plan /approve | 目标、计划模式与审批策略的用户侧接口 |
| 技能 | goal / plan | 同命令的面向 Agent 的技能指令 |
| 处理器 | satrap_coding.inject | 用户消息进入模型前注入目标块 |

## 审批模型

写类操作 (文件写 / shell 写命令) 经过 `PermissionEngine`, 三种策略 (`/approve mode <user|auto-agent|full>`):

- `user` (默认): 需要用户批准, 通过 `session.user_input_provider` 询问; 未配置输入通道则返回"需要用户批准"
- `auto-agent`: 调用会话主模型只读判断 (allow / deny / ask), 判断失败降级为询问
- `full`: 全部放行

询问时用户输入 `y` 仅批准本次操作, `all` 才授予本会话全部权限 (不跨会话持久化)。审批策略本身 (`/approve mode`) 随规则文件持久化, 跨会话生效。

持久规则 (`/approve rule <操作> <风险级 0-1>`) 按操作类型直接放行低于风险级上限的操作 (最高 1, 高危操作不开放持久放行), 规则保存在插件数据目录的 `permissions.json`。计划模式下写类操作被引擎直接拒绝, 不做询问。

命令执行前按分隔符 (`;` / `&&` / `||` / `|`) 切分逐段分级取最高风险, 只读首词拼接破坏性命令无法绕过; 盘根删除 (`rm -rf C:\` 等) 与重定向写文件均被拦截或升级为写操作。

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

`/goal` 设置的目标由注入处理器自动拼接到后续用户消息头部 (带缓存, 内容变化自动失效), 保证模型每轮都围绕目标推进。`/plan on` 与工具审批引擎共享同一状态: 进入计划模式后 write_file / edit_file / shell 写命令全部被拒绝, 只输出计划。计划模式仅限制工作区写操作 (文件 / shell / 沙箱); 长期记忆属元信息, 其增删改 (base_take 的记忆工具与 /memory 命令) 不受计划模式拦截, 属有意设计。

## 工作区与免审批语义

插件不再提供独立 sandbox 工具 (与 shell / 文件工具重复, 已移除), 工作区与沙箱合一: 默认沙箱根目录全局共享 (`.satrap/sandbox`, 可用 `session.coding_sandbox_root` 或插件配置 `sandbox_root` 覆盖)。免审批范围 (仅计划模式可拦截):

- 文件工具写入沙箱根内路径 (write_file / edit_file), 只读工具始终免审批
- shell 命令仅在工作区内活动 (无工作区外绝对路径, 如 `python demo.py` / 重定向写文件)

以下情况仍需要审批:

- 环境修改类命令 (pip install 等)
- 黑名单命令直接拒绝
- shell 命令引用工作区外的绝对路径 (盘符/UNC 近似检测)
- 文件工具操作工作区外路径 (越界拒绝)

`ask_user` 工具建议模型提供 2-3 个推荐选项 (options 参数, 编号展示), 用户可输入序号选择。执行统一走 `shell` (共享本机全环境, 非安全边界), 文件读写走文件工具。沙箱根目录自身不可被删除。

## 数据目录

插件数据保存在 `.satrap/coding/` (测试环境通过 monkeypatch 隔离):

```text
.satrap/coding/
├── permissions.json     # 持久审批规则
├── approval_log.jsonl   # 审批记录
└── goal.json            # 目标与子任务状态
```

> 长期记忆已迁移到公共 MemoryStore (`.satrap/satrapdata/memory.db`), 由 base_take 插件管理; 沙箱目录已统一为 `.satrap/sandbox` (全局共享)。注意: 迁移仅覆盖代码, 旧数据不迁移 (迁移时项目未推生产, 无存量用户数据); 旧库 `.satrap/coding/memory.db` 如仍存在可直接删除。

## 卸载与隔离

`uninstall_plugin` 回收全部工具 / 命令 / 技能 / 处理器并调用插件清理回调, 重置内存级状态 (计划模式 / 审批记忆 / 注入缓存), 不遗留孤儿; 目标等数据文件保留, 重装后数据仍在。插件级共享状态按会话隔离 (state.py), 同一会话的工具、命令、处理器共享同一份权限引擎 / 目标状态。安装任一步失败时自动回滚已注册能力与 sys.path。

## 插件配置

meta.yaml 声明 `config_schema`, 支持以下配置项 (全局默认 + 按会话覆盖两级):

| 配置键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| workspace_root | path | 项目根 | 文件工具白名单根目录 |
| data_root | path | .satrap/coding | 插件数据目录 |
| sandbox_root | path | .satrap/sandbox | 沙箱根目录 (全局共享) |
| shell_timeout | number | 30 | shell 命令超时 (秒) |
| protected_dirs | string | - | 额外保护目录 (逗号分隔) |

安装时经 `install_plugin(path, config={...})` 传入会话级覆盖; 全局默认存于 `.satrap/plugin_config/satrap_coding.json`。
