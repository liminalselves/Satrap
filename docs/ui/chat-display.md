# 聊天展示层 (Chat Display)

聊天展示层 (`satrap.display`) 是**独立于平台后端**的实时聊天服务: 面向前端聊天页提供对话录制、会话编排、WebSocket 实时推送, 以及配套的 HTTP 管理 API。它与后端核心 (core 会话/工作流, edictum 简易会话) 解耦, 以独立进程 `python -m satrap.display.server` 运行在 19872 端口。

> 定位区别: 平台后端 (BackendManager, 19870) 负责 Session/平台接入/检查点等管理; 聊天展示层 (19872) 只服务前端聊天页的实时交互, 两者互不依赖。

## 动机与设计

`ContextManager` 的 `chat_history` 是"模型视角"上下文: 不存 thinking (写入前清除), 且总结压缩会删除原始轮次 —— 不适合作为前端聊天显示的数据源。

`DisplayRecorder` 在会话外部**旁路**记录: 复用 `content_callback` / `thinking_callback` 与 `ToolsManager` 的 `tool_call_start` / `tool_call_end` 可选钩子, 把每轮对话的用户输入 / thinking / 最终输出 / 工具调用状态写入独立 db, 完全不改动会话内部。

## 组件

| 模块 | 职责 |
| --- | --- |
| `recorder.py` | `DisplayRecorder` 旁路记录器; 模块级 `list_conversations()` / `get_conversation_meta()` / `query_conversations()` (历史分页查询) 与项目管理函数 |
| `service.py` | `ChatService` 会话编排: 管理 (AsyncSimpleSession, DisplayRecorder) 对, 落库 + WS 广播, retry / fork / cancel / 上传 / 插件 / 记忆 / 预加载 / 历史管理与回收站 / 回复版本切换 / ask_user 回填 / RAG / 会话级插件配置 |
| `server.py` | `ChatHTTPServer` 零依赖 asyncio HTTP + WebSocket 服务 (端口 19872) |
| `plugins.py` | `ChatPluginRegistry` 插件扫描 + `.satrap/chat_plugins.json` 启用状态 |

## 启动

```bash
python -m satrap.display.server             # 默认 127.0.0.1:19872
python -m satrap.display.server --host 0.0.0.0 --port 19873
```

开发脚本会自动拉起所需服务: `scripts/start-dev.ps1` 和 `scripts/start-ui.bat` 均启动控制端 19871, 聊天端 19872 与前端 5173。`scripts/kill-chat-server.ps1` 用于清理残留的 `satrap.display.server` 进程。

## 数据模型 (display.db)

Chat 使用保留平台实例 `chat` 的唯一 `platform.db`, 展示层和上下文通过不同表共库存储:

| 表 | 内容 |
| --- | --- |
| `display_turns` | 一轮一条: `id` / `conversation_id` / `turn_index` / `user_input` / `thinking` / `answer` / `attachments` / `segments` (按时间顺序记录 thinking/tool/content 分段) / `active_variant` (当前回复版本号) / `context_stats` / `created_at`; `start_turn` 即插入 (answer 先空), `end_turn` 回填 |
| `display_turn_variants` | 每轮的每个回复版本一条: `(turn_id, variant_index)` 唯一, `thinking` / `answer` / `segments` / `context_messages` / `context_stats`; 回复版本 (variant) 切换的落库基础, 既有轮次按第 0 版本登记 |
| `display_tool_calls` | 一轮内每次工具调用一条: `turn_id` / `seq` / `name` / `arguments` / `success` / `call_id` / `variant_index` / `created_at`; `success` 三态 (`NULL` 进行中 / `1` 完成 / `0` 失败), 执行前插入、执行后按 `call_id` 更新 |
| `conversation_meta` | 会话元数据: `model` / `think` (会话默认思考强度) / `project_id` (所属项目, 可空) / `created_at` |
| `projects` | 项目登记: `project_id` / `name` / `root_path` (工作区文件夹绝对路径) / `created_at` |

工具参数落库前截断为前 20 字符 (`arguments` 仅记录模型填入参数, 不记录结果), 长 shell 命令执行中前端可实时显示"进行中"。

## HTTP API (前缀 `/api/chat/`)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 (含内存会话数) |
| GET | `/models` | LLM 配置名列表 |
| GET | `/models/detail` | LLM 配置详情 (api_key 脱敏) |
| POST | `/models` | 新增 LLM 配置 |
| PUT | `/models/{name}` | 更新 LLM 配置 |
| DELETE | `/models/{name}` | 删除 LLM 配置 |
| POST | `/conversations` | 新建会话 `{model, think?, system_prompt?, project_id?}` → `{conversation_id}`; `think` 存入 `conversation_meta` 作为会话默认思考强度; `project_id` 绑定项目工作区 |
| POST | `/conversations/preload` | 预分配 ID 并加载 Session 与插件, 不持久化空会话 (前端"预加载空会话"机制, 未发送的预加载会话有 TTL 过期) |
| GET | `/conversations` | 会话列表 (display.db, 按最近活跃倒序, 含 `project_id` 归属; 新建未发言的空会话也在列表中) |
| POST | `/conversations/{id}/project` | 会话改绑项目 `{project_id}` (`null` = 移出项目归入"最近"); 仅影响之后的工具调用 |
| DELETE | `/conversations/{id}` | 删除会话: 清运行时后把会话文件与数据库记录**归档为可恢复回收包** (进回收站, 返回 `archive_id`); 正在生成的会话需 `force` 先取消再回收; 未持久化的预加载会话直接物理删除不进回收站 |
| GET | `/history` | 历史会话分页查询 / 过滤 (搜索、项目、模型、轮数、天数) / 统计 |
| POST | `/history/delete` | 批量回收历史 `{mode: selected/empty/single/filtered, ...}`, 逐会话返回结果 |
| GET | `/history/trash` | 回收站列表 (回收包元数据, 按删除时间倒序) |
| POST | `/history/trash/restore` | 恢复回收包 `{archive_id}` (同 ID 会话仍在运行则拒绝) |
| POST | `/history/trash/purge` | 永久删除回收包 `{archive_id}` (不可恢复) |
| GET/POST | `/history/storage` | 会话存储大小统计 (缓存快照, POST 强制刷新) |
| GET | `/turns?conversation=xxx` | 对话轮次 (含工具调用明细) |
| POST | `/turns/variant` | 切换最后一轮的回复版本 `{conversation, variant_index}` |
| POST | `/send` | 发送 `{conversation, text, think?, attachments?}`, 立即返回, WS 推流; `think` 缺省时用会话默认; 预加载会话首次发送时接受 `model/temperature/system_prompt/project_id` 作为 `preload_settings` 校验并应用最新设置 |
| POST | `/ask-user/answer` | 回填 ask_user 工具等待的用户回答 `{conversation, answer}` |
| POST | `/upload` | 文件上传 `{conversation, file_name, file_data(base64)}`, 10MB 上限 |
| POST | `/retry` | 重试最后一轮 `{conversation, think?}` (删除记录后按原输入重发; `think` 缺省用会话默认) |
| POST | `/fork` | 从指定轮次 fork 新会话 `{conversation, turn_index}` |
| POST | `/cancel` | 取消当前正在执行的生成 |
| GET | `/plugins` | 插件清单 (扫描 + 启用状态) |
| POST | `/plugins/{name}/enable` / `/disable` | 插件聚合启停 (对活动会话即时 install / uninstall) |
| POST | `/plugins/{name}/capability` | 能力独立启停 `{kind, cap, enabled}` |
| GET | `/plugins/{name}/config` | 插件配置 (schema + 当前全局值) |
| PUT | `/plugins/{name}/config` | 保存插件全局配置 `{config}` |
| GET | `/plugin-model-options` | 插件配置表单的模型选项 (llm/embed/rerank 字段的候选配置名) |
| GET/PUT | `/session-plugin-config` | 会话级插件配置覆盖: GET 返回合并后配置与各字段来源, PUT 按字段整体覆盖, 删除键表示恢复继承 |
| GET/POST | `/rag` | RAG 知识库管理 (列表 / 新建 / 配置 / 删除 / 文档与重建 / 检索测试), 经有界线程池执行 |
| POST | `/rag/upload` | 知识库文档上传 (独立 32MiB 请求体上限) |
| GET | `/memories?scope=session:<conversation_id>` | 列出指定会话的记忆 |
| POST | `/memories` | 添加记忆 `{title, content, tags, importance, scope}` |
| PUT | `/memories/{id}` | 更新记忆 |
| DELETE | `/memories/{id}?scope=xxx` | 删除记忆 |

项目管理路由 (前缀 `/api`, 非 `/api/chat`):

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/projects` | 项目列表 |
| POST | `/api/projects` | 新建项目 `{name, root_path}` (校验路径存在且是目录) |
| DELETE | `/api/projects/{id}` | 删除项目 (仅解绑其下会话, 不动会话数据与磁盘文件) |
| GET | `/api/fs/browse?path=xxx` | 目录浏览 (前端新建项目选择工作区): 只列子目录, `path` 为空时 Windows 返回盘符视图 / POSIX 落到主目录; 根层级 `parent` 为 `null` (Windows 盘符根为 `''` 回盘符视图)。只读, 不读文件内容 |

## WebSocket (`/ws/chat?conversation=xxx`)

订阅会话实时事件, 连接即推送一次 `snapshot` (全量运行时快照: `{conversation_id, stream_id, seq, state, active_turn_id, turns, pending_user_inputs}`, 用于断线重连恢复), 之后按需推送增量事件:

| 事件 | 内容 |
| --- | --- |
| `turn_start` | 新轮次开始 `{user_input, attachments}` |
| `thinking_delta` | 思考增量 `{delta}` |
| `content_delta` | 回答增量 `{delta}` |
| `tool_start` | 工具调用开始 `{name, arguments, call_id}` |
| `tool_end` | 工具调用结束 `{name, call_id, success}` |
| `ask_user` | ask_user 工具等待用户回答 (前端弹出问答面板) |
| `ask_user_end` | 问答结束 (回答已回填或会话取消) |
| `variant_selected` | 回复版本已切换 `{turn_index, variant_index, variant_count}` |
| `turn_done` | 本轮结束 `{answer, turn_id, turn_index, variant_index, variant_count, context_stats}` |
| `error` | 出错信息 |

所有事件均附带 `conversation_id` 与 `ts`; 增量事件带递增 `seq`, 前端按序号校验去重, 溢出或断线后以 `snapshot` 全量恢复。会话不在内存 (如重启后) 时, 订阅会先登记为孤儿队列, 会话懒加载恢复后自动挂入。

## 会话操作语义

- **send**: 立即返回; run 在后台 task 执行, 流式经 WS 推送。同一会话不支持并发 send (上一轮未完成时拒绝)。`think` 未显式传入时使用会话默认 (来自 `conversation_meta`)。
- **preload (预加载)**: 前端在打开聊天页 / 新建会话时预分配 ID 并后台加载 Session 与插件, 空会话不持久化; 未发送的预加载会话有 TTL (默认 300s) 过期回收。首次 send 时随 `preload_settings` 校验并应用最新设置。
- **retry**: 删除最后一轮记录, 用相同输入重新发送。可传入 `think` 指定本轮思考强度, 缺省用会话默认。重试产生新的回复版本 (variant)。
- **fork**: 复制指定轮次之前的上下文到新会话 (同一 model), 返回新 `conversation_id`。
- **cancel**: 取消后台 task, 本轮 answer 置空并广播 `turn_done`。
- **回复版本 (variant)**: 每轮可保留多个回复版本 (`display_turn_variants`), `POST /turns/variant` 切换最后一轮当前版本, 前端左右箭头翻页浏览。
- **ask_user**: 会话内 ask_user 工具挂起等待时, WS 推送 `ask_user`, 前端问答面板收集答案经 `/ask-user/answer` 回填后继续生成。
- **历史与回收站**: 删除会话 = 把会话目录与 13 张会话域表记录归档为回收包 (`trash/sessions/<archive_id>/`), 可经 `/history/trash` 系列恢复或永久删除; 详见 [运行数据布局](../core/data-layout.md) 与回收站生命周期。Chat 服务停止时, 前端历史管理自动回落到控制服务的冷管理接口 (同一存储布局)。
- **会话恢复**: 重启后首次访问会话时, 经 `conversation_meta` / `display_turns` 懒加载重建运行时状态, 模型、默认 think、项目绑定与插件按原样恢复。

## 项目 (工作区文件夹绑定)

**项目** = 登记的工作区文件夹 (任意绝对路径, 创建时校验存在且是目录) + 名称。项目下所有会话共享该工作区, 可见范围 = 项目工作区 + 本会话上传附件。语义要点:

- **无项目会话使用私有工作区**: 当前会话的 `sandbox/` 同时是默认工作区, 不读写 Satrap 项目根目录。
- **外部项目只共享源码工作区**: 建会话/改绑时 `ChatService` 注入 `coding_workspace_root`, 但 `coding_sandbox_root` / `uploads` / `artifacts` / `indexes` / `cache` / 记忆始终属于当前会话。
- **上传附件会话隔离**: 所有上传写入当前会话的 `uploads/`; 项目绑定不会把附件写入外部项目, `read_document` 只访问当前工作区或当前会话上传目录。
- **删除项目仅解绑**: 其下会话 `project_id` 置空归入"最近", 工作区立即切换为各自的私有沙箱, 外部项目文件不动。
- **会话允许改绑** (`POST /conversations/{id}/project`): 仅影响之后的工具调用, 历史消息不变; 记忆、沙箱及其他运行时数据不会因改绑而共享。
- **工具状态按会话隔离**: 审批、缓存、索引和插件运行状态保存在当前会话目录中。
- **前端目录选择**: 新建项目对话框的路径输入旁提供"浏览"按钮, 经 `/api/fs/browse` 在网页内浏览服务器目录 (无需手敲绝对路径), 选择后自动回填路径与项目名。

## 插件管理

插件清单扫描官方目录 `satrap/expend/plugins` + 用户目录 `.satrap/plugins` (官方优先, 同名冲突官方覆盖); 清单来自 meta.yaml 的能力声明, **默认不安装**。启用状态独立记录于 `.satrap/chat_plugins.json` (不碰 `session_class_config.json`):

```json
{
  "satrap_coding": { "enabled": true, "capabilities": {"tools": {"shell": false}} }
}
```

- `enabled`: 插件聚合开关 (默认 false = 扫描到但不装)
- `capabilities`: 能力独立启用状态 (默认 true); 能力生效 = 插件启用 ∧ 独立启用

前端勾选后由 `ChatService` 对**所有活动会话**即时执行 `install_plugin` / `uninstall_plugin`。沙箱协调: `satrap_coding` 与 `base_take` 同时启用时, 自动停用 base_take 的 `code_sandbox` 工具 (coding 的 shell 能力更强), 避免模型困惑。

插件配置采用四级合并 (schema 默认 < 全局 json < Edictum 命名配置 < 当前会话覆盖), 会话级覆盖经 `GET/PUT /session-plugin-config` 读写, 详见 [RAG 与会话覆盖](../plugins/rag-and-session-overrides.md) 与 [扩展模块](../plugins/extensions.md#插件配置机制)。

## 模型与记忆

- 模型配置与平台后端共用同一份 `.satrap/model_config.json` (ModelConfigManager), 新建会话时指定 `model` 名即可。
- 记忆管理走 `chat/platform.db` 中的公共 `MemoryStore` 表, 默认 scope 为 `session:<conversation_id>`, 见 [运行数据布局](../core/data-layout.md)。
- **记忆隔离**: 每个会话只读写 `session:<conversation_id>` 作用域。项目绑定不会自动共享记忆; 未来增加共享领域时需要单独的数据模型和授权。

## 前端聊天页

前端聊天页位于 `satrap-ui/src/pages/Chat/`, 为独立整页 (不渲染管理面板布局), 通过 `satrap-ui/src/api/chat.ts` 访问上述 API。侧边栏分两段: **项目区** (可折叠分组, 项目行内可直接新建对话/删除项目) 与 **最近区** (无项目会话平铺); 会话 hover 提供移入/移出项目入口。

**对话设置弹窗**为左右分栏 (`Modal size="3xl"`): 左侧分类导航 (复用侧边栏 `glass-nav-item` 样式, `nav-fill` 变体撑满整列), 右侧内容区固定高度独立滚动, 每次打开重置到"对话"分类:

| 分类 | 内容 |
| --- | --- |
| 对话 | 当前会话模型选择、思考强度、温度、系统提示词 |
| 模型管理 | 模型配置的新增 / 编辑 / 删除 |
| 插件 | 插件清单与启停, 能力清单弹窗 (`PluginCapabilitiesModal`, 双层启停), 配置弹窗 (`PluginConfigModal`, 按 schema 渲染表单, 可跳转 `SessionPluginSettingsModal` 配置当前会话参数) |
| 数据管理 | RAG 知识库 (`RagManager`)、长期记忆 (`MemoryPanel`)、会话历史 (`ChatHistoryManager`) 三个入口, 均叠层打开不关闭设置弹窗 |

其他要点: `ChatHistoryManager` 分**历史 / 回收站**两栏, 支持搜索过滤、批量回收、恢复与永久删除, Chat 服务停止时自动回落控制服务冷管理; 记忆面板按 `session:<conversation_id>` 作用域读写, 添加表单的作用域固定为"当前会话"。服务地址由 `src/utils/constants.ts` 的 `CHAT_API_URL` 控制 (默认 `http://127.0.0.1:19872`, 可用环境变量 `VITE_CHAT_API_URL` 覆盖)。开发时经 Vite 代理转发, 见 [前端迁移指南](../archive/frontend-migration.md) 与 `satrap-ui/DEVELOPMENT.md`。
