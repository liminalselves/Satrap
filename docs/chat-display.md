# 聊天展示层 (Chat Display)

聊天展示层 (`satrap.display`) 是**独立于平台后端**的实时聊天服务: 面向前端聊天页提供对话录制、会话编排、WebSocket 实时推送, 以及配套的 HTTP 管理 API。它与后端核心 (core 会话/工作流, edictum 简易会话) 解耦, 以独立进程 `python -m satrap.display.server` 运行在 19872 端口。

> 定位区别: 平台后端 (BackendManager, 19870) 负责 Session/平台接入/检查点等管理; 聊天展示层 (19872) 只服务前端聊天页的实时交互, 两者互不依赖。

## 动机与设计

`ContextManager` 的 `chat_history` 是"模型视角"上下文: 不存 thinking (写入前清除), 且总结压缩会删除原始轮次 —— 不适合作为前端聊天显示的数据源。

`DisplayRecorder` 在会话外部**旁路**记录: 复用 `content_callback` / `thinking_callback` 与 `ToolsManager` 的 `tool_call_start` / `tool_call_end` 可选钩子, 把每轮对话的用户输入 / thinking / 最终输出 / 工具调用状态写入独立 db, 完全不改动会话内部。

## 组件

| 模块 | 职责 |
| --- | --- |
| `recorder.py` | `DisplayRecorder` 旁路记录器; 模块级 `list_conversations()` / `get_conversation_meta()` |
| `service.py` | `ChatService` 会话编排: 管理 (AsyncSimpleSession, DisplayRecorder) 对, 落库 + WS 广播, retry / fork / cancel / 上传 / 插件 / 记忆 |
| `server.py` | `ChatHTTPServer` 零依赖 asyncio HTTP + WebSocket 服务 (端口 19872) |
| `plugins.py` | `ChatPluginRegistry` 插件扫描 + `.satrap/chat_plugins.json` 启用状态 |

## 启动

```bash
python -m satrap.display.server             # 默认 127.0.0.1:19872
python -m satrap.display.server --host 0.0.0.0 --port 19873
```

开发脚本会自动拉起聊天服务: `scripts/start-dev.ps1` (控制端 19871 + 聊天端 19872 + 前端 5173), `scripts/start-ui.bat` (聊天端 + 前端)。`scripts/kill-chat-server.ps1` 用于清理残留的 `satrap.display.server` 进程。

## 数据模型 (display.db)

独立 SQLite 库, 默认路径 `.satrap/satrapdata/display.db` (与 `chat_history.db` 分离):

| 表 | 内容 |
| --- | --- |
| `display_turns` | 一轮一条: `conversation_id` / `turn_index` / `user_input` / `thinking` / `answer` / `attachments` / `created_at`; `start_turn` 即插入 (answer 先空), `end_turn` 回填 |
| `display_tool_calls` | 一轮内每次工具调用一条: `turn_id` / `seq` / `name` / `arguments` / `success` / `call_id`; `success` 三态 (`NULL` 进行中 / `1` 完成 / `0` 失败), 执行前插入、执行后按 `call_id` 更新 |
| `conversation_meta` | 会话元数据: `model` / `think` (会话默认思考强度) / `created_at` |

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
| POST | `/conversations` | 新建会话 `{model, think?, system_prompt?}` → `{conversation_id}`; `think` 存入 `conversation_meta` 作为会话默认思考强度 |
| GET | `/conversations` | 会话列表 (display.db, 按最近活跃倒序) |
| DELETE | `/conversations/{id}` | 删除会话 (内存 + db) |
| GET | `/turns?conversation=xxx` | 对话轮次 (含工具调用明细) |
| POST | `/send` | 发送 `{conversation, text, think?, attachments?}`, 立即返回, WS 推流; `think` 缺省时用会话默认 |
| POST | `/upload` | 文件上传 `{conversation, file_name, file_data(base64)}`, 10MB 上限 |
| POST | `/retry` | 重试最后一轮 `{conversation, think?}` (删除记录后按原输入重发; `think` 缺省用会话默认) |
| POST | `/fork` | 从指定轮次 fork 新会话 `{conversation, turn_index}` |
| POST | `/cancel` | 取消当前正在执行的生成 |
| GET | `/plugins` | 插件清单 (扫描 + 启用状态) |
| POST | `/plugins/{name}/enable` / `/disable` | 插件聚合启停 (对活动会话即时 install / uninstall) |
| POST | `/plugins/{name}/capability` | 能力独立启停 `{kind, cap, enabled}` |
| GET | `/plugins/{name}/config` | 插件配置 (schema + 当前全局值) |
| PUT | `/plugins/{name}/config` | 保存插件全局配置 `{config}` |
| GET | `/memories?scope=xxx` | 列出记忆 (默认 scope `web_chat`) |
| POST | `/memories` | 添加记忆 `{title, content, tags, importance, scope}` |
| PUT | `/memories/{id}` | 更新记忆 |
| DELETE | `/memories/{id}?scope=xxx` | 删除记忆 |

## WebSocket (`/ws/chat?conversation=xxx`)

订阅会话实时事件, 连接即推送 `subscribed`, 之后按需推送:

| 事件 | 内容 |
| --- | --- |
| `turn_start` | 新轮次开始 `{user_input, attachments}` |
| `thinking_delta` | 思考增量 `{delta}` |
| `content_delta` | 回答增量 `{delta}` |
| `tool_start` | 工具调用开始 `{name, arguments, call_id}` |
| `tool_end` | 工具调用结束 `{name, call_id, success}` |
| `turn_done` | 本轮结束 `{answer}` |
| `error` | 出错信息 |

所有事件均附带 `conversation_id` 与 `ts`。会话不在内存 (如重启后) 时, 订阅会先登记为孤儿队列, 会话懒加载恢复后自动挂入。

## 会话操作语义

- **send**: 立即返回; run 在后台 task 执行, 流式经 WS 推送。同一会话不支持并发 send (上一轮未完成时拒绝)。`think` 未显式传入时使用会话默认 (来自 `conversation_meta`)。
- **retry**: 删除最后一轮记录, 用相同输入重新发送。可传入 `think` 指定本轮思考强度, 缺省用会话默认。
- **fork**: 复制指定轮次之前的上下文到新会话 (同一 model), 返回新 `conversation_id`。
- **cancel**: 取消后台 task, 本轮 answer 置空并广播 `turn_done`。
- **会话恢复**: 重启后首次访问会话时, 经 `conversation_meta` / `display_turns` 懒加载重建运行时状态, 模型、默认 think 与插件按原样恢复。

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

插件配置两级机制 (schema 默认 < 全局 json < 会话覆盖) 见 [扩展模块](extensions.md#插件配置机制)。

## 模型与记忆

- 模型配置与平台后端共用同一份 `.satrap/model_config.json` (ModelConfigManager), 新建会话时指定 `model` 名即可。
- 记忆管理走公共 `MemoryStore` (`.satrap/satrapdata/memory.db`), 默认 scope `web_chat`, 见 [扩展模块](extensions.md#长期记忆存储-memorystore)。

## 前端聊天页

前端聊天页位于 `satrap-ui/src/pages/Chat/`, 为独立整页 (不渲染管理面板布局), 通过 `satrap-ui/src/api/chat.ts` 访问上述 API。服务地址由 `src/utils/constants.ts` 的 `CHAT_API_URL` 控制 (默认 `http://127.0.0.1:19872`, 可用环境变量 `VITE_CHAT_API_URL` 覆盖)。开发时经 Vite 代理转发, 见 [前端迁移指南](frontend-migration.md) 与 `satrap-ui/DEVELOPMENT.md`。
