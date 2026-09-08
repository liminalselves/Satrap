# 插件系统 (Plugin)

插件是 edictum 的**可分发能力组合包**: 一个目录即一个插件, 由 `meta.yaml` 声明身份, 可同时携带工具 / 技能 / MCP 接入 / 处理脚本 / 命令。安装后其能力全部注册进会话, 并支持**双层启停** (插件级开关 × 能力独立状态, 能力生效 = 两者皆开)。

> 本文专注插件系统本身 (结构 / 安装 / 启停 / 错误处理 / 扫描机制)。会话框架整体见 [简易 Agent 框架](simple-session.md); 官方示例插件见 [satrap_coding 插件](satrap-coding-plugin.md)。

## 目录结构

```text
插件名/
├── meta.yaml     # name(必填) / version / author / repo / description
│                 # 及可选能力组成描述: tools/skills/handlers/commands/mcp (名字 -> 描述)
├── tools.py      # 可选: Tool 子类 (同步版) / AsyncTool 子类 (异步版), 或 get_tools(session, config?, resources?) 工厂
├── skills.py     # 可选: 导出 skills: list[Skill]; 或 skills/ 子目录 (skill.md 文件夹式)
├── mcp.py        # 可选: 导出 clients: dict[str, MCPClient] 或 build_clients() (仅异步版)
├── commands.py   # 可选: 导出 commands / async_commands 字典, 或 build_commands(session) 工厂, 或 cmd_* 约定
├── handlers.py   # 可选: 导出 handlers: list[SessionHandler]; 或 build_handlers(session) 工厂, 或 4 个约定函数
├── state.py      # 可选: 导出 cleanup(session) 卸载清理回调 (兼容旧命名 reset_plugin_state)
└── ...           # 插件私有模块 (安装时插件目录加入 sys.path, 可互相 import)
```

## meta.yaml

### 身份字段

```yaml
name: my-plugin        # 必填, 插件唯一标识
version: 0.1.0
author: ...
repo: ...
description: "..."
```

### 能力声明 (可选)

`meta.yaml` 除身份字段外, 还可用五类键声明插件的**能力组成描述** (名字 -> 描述), 供前端 / 文档展示:

```yaml
name: my-plugin
version: 0.1.0
description: "..."
# 以下为可选: 能力组成描述 (名字 -> 描述)
tools:
  read_file: 读取工作区内文件
  shell: 执行本机 shell 命令
skills:
  plan: 计划模式
handlers:
  my-plugin.inject: 注入上下文
commands:
  plan: 进入/退出计划模式
mcp: {}
```

> 声明仅作**描述补充**: 能力的实际注册由自动扫描 (`collect_*`) 决定 (真相源), meta.yaml 声明不改变安装行为。声明了但扫描不到的能力仅在安装时给出 `warning` 日志; 扫描到但未声明的能力照常安装 (描述留空)。声明经 `plugin.capability_descriptions` 读取, 并在 `plugin.list_capabilities()` 的每项以 `description` 字段返回。

### 配置项声明 `config_schema` (可选)

插件可在 meta.yaml 用 `config_schema` 声明配置项 (键 -> `{type, default, description, options?}`), 供前端渲染配置表单, 解析后存于 `plugin.config_schema`:

```yaml
config_schema:
  db_scope:
    type: select            # string / path / number / bool / select
    default: session
    options: [global, session, session_global]
    description: "检索范围"
  write_db_id:
    type: knowledge_base    # 模型/资源选择器: knowledge_base / knowledge_bases / llm / embed / rerank
    default: ""
    description: "默认导入目标"
```

- 基础类型: `string` / `path` / `textarea` / `number` (可加 `integer` / `minimum` / `maximum`) / `bool` / `select` (配 `options`);
- 模型与资源选择器: `llm` / `embed` / `rerank` (引用后端模型配置名) 与 `knowledge_base` / `knowledge_bases` (引用 RAG 知识库), 前端以下拉选项渲染, 模型引用的运行时注入与校验见 [RAG 与会话覆盖](rag-and-session-overrides.md);
- 配置值按四级合并后注入工具工厂 (见下"插件配置与会话级覆盖"), schema 不提供校验之外的安装行为变化。

## 安装 / 启停 / 卸载

```python
from satrap import SimpleSession

plugin = session.install_plugin("./plugins/my-plugin")   # 返回 Plugin 实例
plugin.name / plugin.version / plugin.author / plugin.repo   # meta.yaml 元信息

session.disable_plugin("my-plugin")    # 聚合停用: 压制名下全部能力
session.enable_plugin("my-plugin")     # 聚合启用: 按独立状态恢复
session.uninstall_plugin("my-plugin")  # 卸载: 全量回收, 不留孤儿
session.list_plugins()
```

异步版 `install_plugin` / `uninstall_plugin` / `enable_plugin` / `disable_plugin` 均为 async (`await session.install_plugin(path)`), 并支持 `mcp.py` 自动接入。同步版安装含 `mcp.py` 的插件会跳过该文件并警告。

也可一键扫描安装全部可用插件:

```python
from satrap.edictum.plugin import install_all_plugins, install_all_plugins_async

install_all_plugins(session)              # 同步: 扫描官方预设目录 + 用户目录
await install_all_plugins_async(session)  # 异步
```

**插件目录扫描**: 官方预设目录 `satrap/expend/plugins` (只读基线) 在前, 用户插件目录 `.satrap/plugins` (用户自添加) 在后; 同名插件冲突时官方优先 (用户同名被跳过并记 info)。单个插件安装失败不影响其余 (记 error)。

## 双层启停与独立接口

插件内每项能力可**独立启停**, 聚合恢复不会误开独立停用的能力。推荐通过插件实例接口操作:

```python
plugin.disable_tool("calc")      # 独立停用插件内工具
plugin.enable_tool("calc")       # 独立启用
plugin.disable_skill("demo")     # 同步版返回 bool; 异步版需 await
plugin.disable_mcp("fs")         # 独立停用 MCP 连接的全部工具
plugin.disable_handler("log")    # 独立停用处理器
plugin.disable_command("plan")   # 独立停用命令
plugin.list_capabilities()        # 展示插件内每项能力的实效状态 (含 meta.yaml 声明的 description)
```

> 注意: 插件内能力的独立启停建议走插件实例接口, 会话全局接口 (`session.disable_tool`) 不维护插件状态; 插件停用期间对名下能力的操作以恢复时的独立状态为准。
>
> 工具与处理器采用**执行路径合成**: 插件停用后, 即使 `enable_tool` / `enable_all_tools` / `enable_handler` 更新了独立位, 执行时仍按「独立位 ∧ 插件聚合开关」过滤 (`execute_tool` 返回 disabled 错误, 处理器不执行); 工具定义列表 (`get_tools_definitions`) 按独立位展示, 与执行路径解耦。

## 插件配置与会话级覆盖

声明了 `config_schema` 的插件支持运行时配置, 按**四级合并** (后者覆盖前者):

```text
schema 默认 < 全局插件配置 (.satrap/plugin_config/<name>.json) < Edictum 命名配置 (session_class_config) < 当前会话覆盖
```

- **会话级覆盖**存平台库 `session_config_overrides` 表, 按会话与配置域隔离; 空值按 schema 校验, **删除键表示恢复继承**, 不保存合并结果, 对象和数组按字段整体替换;
- 合并后的配置作为 `config` 参数注入 `get_tools(session, config, ...)` 等工厂, 工具按当前生效配置工作;
- Chat 前端经 `GET/PUT /api/chat/session-plugin-config` 读写覆盖 (GET 返回合并后配置与各字段来源), 控制服务另有 `/config/session-plugin-config` 冷接口; 覆盖记录随会话进入删除 / 归档 / 恢复 / 分支生命周期;
- 完整语义 (字段来源、并发控制、RAG 插件示例) 见 [RAG 与会话覆盖](rag-and-session-overrides.md)。

## 能力收集约定 (collect_*)

每个能力文件支持多种导出方式, 安装时按优先级自动扫描:

| 文件 | 收集方式 (按优先级) |
| --- | --- |
| `tools.py` | ① `get_tools(...)` 工厂, 按签名自适应绑定: `(session, config, resources=...)` → `(session, config, resources)` → `(session, config)` → `(session)` → `()`; ② 模块内 `Tool`/`AsyncTool` 子类 (无参构造, 排除基类) |
| `skills.py` | ① `skills/` 子目录 (每个文件夹一个 `skill.md`); ② 导出 `skills: list[Skill]` |
| `mcp.py` | ① 导出 `clients: dict[str, MCPClient]`; ② `build_clients()` 工厂 |
| `commands.py` | ① `build_commands(session)` 工厂 (返回 (同步, 异步) 二元组或同步映射); ② 导出 `commands` / `async_commands` 字典; ③ `cmd_*` (同步) / `cmd_*_async` (异步) 约定 |
| `handlers.py` | ① `build_handlers(session)` 工厂; ② 导出 `handlers: list[SessionHandler]`; ③ 4 个约定函数 (`before_user_send` / `after_user_send` / `before_model_reply` / `after_model_reply`) |
| `state.py` / `hooks.py` | 导出 `cleanup(session)` 卸载清理回调 (兼容旧命名 `reset_plugin_state`) |

**冲突即失败**: 安装过程中任一能力与已注册能力同名冲突 (工具 / 技能 / 处理器 / 命令 / MCP 连接), 或插件名重复, 立即抛 `ValueError` 并**回滚**已注册能力与 `sys.path`, 不留孤儿。

## 错误处理

### 安装期

- `meta.yaml` 缺失 / 格式非法 / 缺 `name` → 抛 `ValueError`
- 能力冲突 / 插件名重复 → 抛 `ValueError`, 全量回滚
- `mcp.py` 客户端连接失败 → 关闭已建连接后 re-raise, 触发回滚
- 能力声明 (meta.yaml 五类键) 与扫描结果不匹配 → 仅 `warning`, 不改变安装行为
- 异步回调混入 (handlers 含 `async def`) → 全同步协议统一抛 `TypeError`

### 运行期 (handler 异常)

插件 handler 与会话内 handler 走相同的 `_invoke_handler` 隔离路径, 按 `SessionHandler.error_policy` 处理:

| 策略 | 行为 | 适用 |
| --- | --- | --- |
| `"continue"` (默认) | 异常记 `error` 日志后返回 `None`, 该 handler 本次调用视为透传, **后续 handler 与模型调用照常** | 通知型 / 注入型 handler, 不应因自身故障拖垮会话 |
| `"abort"` | 异常被包装为 `HandlerAbortError` 抛出, **中断整轮 run** | 关键前置校验, 失败必须终止 |

**主动短路** (仅 `before_user_send`): `HandlerResult.respond(text)` / `reject(reason)` 短路跳过模型 (text 作为 run 返回, `after_model_reply` 仍执行); `abort(reason)` 抛 `HandlerAbortError` 故障终止 (此异常不被隔离, 直接透传)。

**各阶段异常路径**:
- `before_user_send` / `after_user_send` / `before_model_reply`: 异常走隔离, 默认不影响主流程
- `after_model_reply`: 在模型调用的 `finally` 中执行; 模型异常时 `result=None` 且 `ctx.error` 置位, 此时 handler 仍被调用, 但其返回的改写被忽略 (`ctx.error is None` 才接受改写)
- 模型调用本身异常 (`except BaseException`): 记录到 `ctx.error` 后**原样 re-raise** (不隔离), 传播给 `run()` 调用方

**异步版超时**: `_invoke_handler_async` 用 `asyncio.wait_for(to_thread(fn), timeout)` (默认 `HANDLER_TIMEOUT=30s`, 可被 `handler.timeout` 覆盖), 超时抛 `TimeoutError` 按 `error_policy` 处理。关键语义: **超时只放弃等待结果, 底层工作线程仍跑完**, 故约定 handler 网络调用需自设超时。

### 卸载期

`close()` 异常不阻断: 卸载 / 移除 handler 时 `close()` 抛错仅记 `warning`, 不影响其余清理。`close()` 契约: 同步函数、幂等、容忍回调曾被超时取消的中间状态。

## 安全提示

安装插件 = 执行其代码 (tools.py / skills.py / mcp.py / handlers.py 均会被 import)。meta.yaml 的 `author` / `repo` 仅用于溯源, 不提供安全保证, 请仅安装可信来源的插件。

## 同步 / 异步差异

| 能力 | SimpleSession (同步) | AsyncSimpleSession (异步) |
|---|---|---|
| 插件接口 | 同步 (`install_plugin` 等) | async (`await install_plugin` / `uninstall_plugin` / `enable_plugin` / `disable_plugin`) |
| 工具基类 | `Tool` | `AsyncTool` |
| MCP 接入 | 不支持 (插件 `mcp.py` 跳过并警告) | 支持 (`mcp.py` 自动接入, 工具注册进主工作流) |
| 命令 | 仅同步命令 (`commands` / `cmd_*`) | 仅异步命令 (`async_commands` / `cmd_*_async`) |
| handler 执行 | 同步直调, 无超时保护 | `to_thread` + `wait_for` 超时 (默认 30s) |
