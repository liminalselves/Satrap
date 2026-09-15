# edictum 体系总览

edictum 是建立在核心框架 `Session` 之上的高可扩展单 workflow Agent 体系, 包路径为 `satrap.edictum`。它把一个会话的能力拆成六类可注入元素 —— 命令, 工具, MCP, skill, 处理器 (Handler) 和目录插件 (Plugin) —— 并统一支持注入, 删除, 启停与查看。

与直接使用核心框架的区别: 核心框架 (`satrap.core.framework`) 提供 workflow / Session 基类和运行后端; edictum 在其上提供"开箱即用 + 运行时可编排"的会话实现和插件机制。聊天展示层 (Chat) 与平台适配会话都复用这套插件机制。

## 模块地图

| 模块 | 职责 |
| --- | --- |
| `simple_session/` | 内置会话实现: `SimpleSession` (同步) 与 `AsyncSimpleSession` (异步), 处理器注册表, 能力启停, 恢复接入 |
| `plugin.py` | 插件本体: meta.yaml 加载, 五类能力收集 (`collect_tools` / `collect_skills` / `collect_handlers` / `collect_commands` / `collect_mcp_clients`), `Plugin` 句柄与命名空间启停 |
| `plugin_catalog.py` | 插件目录: 扫描官方目录 (`satrap/expend/plugins/`) 与用户目录 (`.satrap/plugins/`), 产出稳定的元数据 / 配置结构 / 能力声明条目; 官方插件优先于同名用户插件 |
| `plugin_spec.py` | 运行规格: 把字符串或对象形式的插件配置解析为统一的 `PluginSpec`, 并计算稳定指纹 |
| `plugin_compatibility.py` | 兼容性判定: 在导入插件代码前校验 `compatibility` (Satrap 版本范围) 与 `applicability` (会话类型 / 平台适配器) |
| `plugin_settings.py` | 配置分层: 合并 default / global / named / 会话覆盖四层配置, 逐字段来源追踪与保存校验 |
| `plugin_runtime.py` | 运行协调器: 比较目标规格与已应用规格, 执行安装 / 启停 / 重装 / 能力差量应用, 失败回滚 |
| `plugin_resources.py` | 插件模型依赖: 命名模型配置校验, LLM / Embedding / ReRank 客户端懒构造, 每次安装独享生命周期 |
| `plugin_config.py` | 配置 schema: `ConfigField` 类型定义, 声明解析与值校验 |
| `registry.py` | 会话类型注册表: `EdictumTypeRegistry` 用稳定类型名 + 工厂契约描述会话实现 |
| `config.py` | 命名会话冷配置: `EdictumConfigManager` 持久化 `edictum_session_config.json`, 后端未启动也可增删改查 |

## 会话类型与注册表

`EdictumTypeRegistry` 用 `EdictumTypeDefinition` 描述一种会话实现: 稳定类型名, 工厂, 同步 / 异步标记, 能力支持位 (plugins / mcp / stream) 以及插件安装适配器。`create_default_edictum_type_registry()` 注册两个内置类型:

- `simple` → `SimpleSession` (同步)
- `async_simple` → `AsyncSimpleSession` (异步)

新增会话实现只需注册类型定义, 不需要改动 Provider 分发或冷配置结构。用法细节见 [SimpleSession 用法](simple-session.md)。

## 处理器链

`SessionHandler` 提供四个处理点, 是"直接注入流程"而非 hook:

- `before_user_send`: 用户输入进入前, 可改写文本或用 `HandlerResult` 短路整个流程
- `after_user_send`: 用户输入确认后 (只读)
- `before_model_reply`: 模型回复生成前 (只读)
- `after_model_reply`: 模型回复生成后, 可改写最终文本

处理器按 `priority` 升序执行, 同值按注册序号; 一次 run 使用一致性快照, 运行中的增删 / 启停 / 优先级变更在下一次 run 才生效。同步会话拒绝异步回调 (协议级校验)。

## 双层启停

所有能力 (包括处理器) 都有两层开关:

- 独立位: 能力自身的启用状态
- 插件聚合位: 所属插件的 `enabled`

实际生效 = 独立位 AND 插件聚合位。停用插件不会丢失各能力的独立状态, 重新启用后原样恢复。

## 插件生命周期

一个目录插件从磁盘到生效经过以下环节, 全部环节对 Chat 会话与平台会话一致:

1. 目录扫描 (`PluginCatalog.scan`): 读取 `meta.yaml`, 解析出版本, 作者, 配置结构 (`config_schema`), 五类能力声明和兼容性声明, 生成目录条目; 元数据不合法的目录被跳过。
2. 规格解析 (`parse_plugin_specs`): 把配置里的插件列表 (字符串或对象) 解析为 `PluginSpec`, 校验配置项必须已在 `config_schema` 声明, 能力启停必须对应已声明能力; 未显式配置的已声明能力默认启用。
3. 兼容性判定 (`check_plugin_compatibility`): 在导入任何插件代码之前, 按 `compatibility.satrap` 版本范围和 `applicability.session_types` / `applicability.platforms` 判定, 产出机器可读的 `reason_code`; 未完整声明的插件给出警告但放行。
4. 配置合成 (`PluginSettingsService` / `resolve_runtime_specs`): 按 default → global → named → 会话覆盖合并, 得到生效配置和逐字段来源; 引用的命名模型配置会被展开成 `resources_revision` 指纹, 模型配置变更因此能被察觉。
5. 运行协调 (`reconcile_plugin_states_async`): 预览目标规格与已应用规格的差量 (add / enable / disable / remove / reinstall / capabilities), 然后执行: 新装走安装器, 纯能力差量在插件句柄上直接调用 `enable_*` / `disable_*`, 配置或版本变化走卸载重装; 安装或能力应用失败时自动回滚到旧规格。会话不支持热卸载时标记 `restart_required`, 等会话重新激活。协调结果汇总 `ok` / `failed` / `drift`, 漂移状态可通过重试收敛。
6. 资源管理 (`PluginResources`): 插件声明的模型类配置项在首次使用时才构造客户端, 每次安装独享一批客户端, 卸载时统一关闭; 配置本体保持纯 JSON, 凭据不进入插件配置或对外响应。

插件目录结构, meta.yaml 字段, 安装 / 启停 / 卸载接口和错误处理的细节见 [插件系统](plugin-system.md)。

## 冷配置与 Provider 衔接

`EdictumConfigManager` 管理按名称组织的会话冷配置 (`edictum_type` + `params` + `model_name` + `plugins`), 只做持久化和校验, 不创建运行时会话, 因此控制服务可以在后端未启动时维护配置。运行时由 `SessionManager` 通过 Provider 机制按 `provider = "edictum"` 和定义名分发到类型注册表创建实例, 契约见 [会话 Provider](session-providers.md)。

## 与执行引擎和恢复的关系

内置会话的 Agent 循环由执行引擎 (`satrap.core.framework.Base.execution`, 见 [执行引擎架构](../execution/engine.md)) 驱动。安装插件后, `prepare_session_recovery` 会把插件代码摘要, 有效配置和能力状态纳入会话的恢复指纹; 指纹不匹配的历史运行记录不会被错误续跑, 语义见 [任务执行记录与恢复](../execution/task-recovery.md)。

## 深入阅读

- [SimpleSession 用法](simple-session.md): 快速上手, 处理器示例, 工具 / 命令 / skill / MCP 接入, 流式与 checkpoint
- [插件系统](plugin-system.md): 目录插件结构, meta.yaml 能力声明, 能力收集约定, 双层启停与错误处理
- [会话 Provider](session-providers.md): SessionProvider 契约, 命名会话定义与 Edictum 冷配置
- 内置插件实例: [satrap_coding 插件](../plugins/satrap-coding-plugin.md), [base_take 插件](../plugins/base-take-plugin.md), [RAG 与会话覆盖](../plugins/rag-and-session-overrides.md)
