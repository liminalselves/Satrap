# group_chat 群聊插件

group_chat 提供当前群的资料查询, 结构化回复, 成员查找与消息检索, 可选修改机器人自己的群昵称
group_chat 与 group_admin 独立安装和启用, 共享宿主的适配器, 授权和审批服务, 插件之间不读取配置或调用彼此工具

## 启用

1. 更新代码后重新启动后端
2. 在「Agent 配置」打开群聊使用的 Edictum 命名配置, 添加并启用 `group_chat`, 保存配置
3. 在「平台管理」确认群聊 Agent 选择该配置; 单群已指定 Agent 时以单群选择为准
4. 插件工具, `group_chat` skill 和 `group_chat.environment` 前处理默认启用, 可在插件使用位置分别停用

插件只适用于平台会话, 不在 Chat 中启用; 同一 Agent 用于私聊时, 本轮仅可能提供已授权的群列表查询, 不进入群聊回复缓冲模式
适配器通过公共能力声明决定可用动作, 不要求新增平台修改插件中的平台类型分支
平台不支持成员查找, 单条消息回源, 引用或提及时, 返回明确不可用原因, 不把缺失能力当成空成功结果

## 工具与回复

| 工具 | 用途 |
| --- | --- |
| `group_chat_list_groups` | 仅配置的管理者私聊可查机器人加入的群, 默认关闭, 返回最多 512 项并受字符预算限制 |
| `group_chat_get_group_info` | 查询当前群的 ID, 名称与人数 |
| `group_chat_list_members` | 分页列出当前群成员 ID, 昵称, 群昵称与角色 |
| `group_chat_set_group_nickname` | 只修改当前机器人自身的群昵称, nickname 为空则清空, 默认关闭 |
| `group_chat_reply` | 准备本轮唯一的最终回复, 可组合文本, 一条引用与多个提及 |
| `group_chat_find_members` | 按昵称与本群昵称返回候选, 重名时不自动选一个 |
| `group_chat_get_member` | 核验当前群成员 ID, 返回昵称, 群昵称与角色 |
| `group_chat_get_message` | 读取消息, 发送者与引用关系, 缺失时核验回源 |
| `group_chat_recent_messages` | 补取最近讨论, 返回消息 ID 和采集范围 |
| `group_chat_search_messages` | 按关键词, 发送者和时间范围查询, 未填时区时自动使用后端本地时区 |
| `group_chat_prepare_summary` | 指定开始/结束时间, 冻结有界摘要来源, 排除机器人自己的发言 |
| `group_chat_read_summary_sources` | 连续读取摘要快照后续分页, 不混入新收到的消息 |
| `group_chat_save_summary` | 保存带来源 ID 的摘要条目, saved 表示入库, 不表示已发到群 |
| `group_chat_get_summary` | 查看保存摘要与来源状态 |
| `group_chat_list_summaries` | 查找当前群已保存摘要 |
| `group_chat_get_message_assets` | 按消息 ID 取出当前群可用图片, 不下载全部历史媒体 |
| `group_chat_list_stickers` | 查询当前群已启用且平台兼容的表情 |

除管理者私聊的群列表查询外, 所有工具仅操作当前群; 所有工具均不接受模型指定平台实例, 机器人账号或其它群
成员返回值中的 `nickname` 是账号昵称, `card` 是群昵称, `role` 是平台资料; 字段名保留以兼容已有消费者, 角色不赋予管理操作权限
消息与成员 ID 使用字符串; 消息中的发言者, 被 @者和被引用消息的发送者分别处理

```json
{
  "components": [
    {"type": "quote", "message_id": "m101"},
    {"type": "mention", "source_message_id": "m101"},
    {"type": "text", "text": " 这是对你刚才问题的回复。"},
    {"type": "mention", "user_id": "u202"},
    {"type": "text", "text": " 你补充的条件也适用。"}
  ]
}
```

`source_message_id` 选择该消息的发送者, 不选择正文中被提及的成员
直接使用 `user_id` 时, 宿主通过当前群成员资料或已核验消息确认身份; 不提供 @全体
引用最多一个, 宿主将其移到首位; 文本与提及的其它顺序保持原样, 普通文本中的 @昵称仍是文本

`prepared` 表示全部组件已经核验并暂存, 不表示送达
启用回复工具的群聊轮次暂存正文回调, 轮次成功结束后统一交给原发送队列
存在草稿时不再发送模型最终文本, 不自动追加平台的引用/提及策略; 未调用工具时发送最终文本并沿用原策略
超过平台文本预算时按原通道拆分, 汇总实际分段回执; 只将已确认的平台消息写入档案

主 Agent 才能准备最终回复, 子 Agent 即使共享同一工具实例也不能取得提交权
作用域结束后复制的执行上下文也会失效; 异常, 超时, 取消, 切换路由, 停用插件或替换连接时不提交旧草稿
提交前再次核验目标, 本地删除或平台撤回后的出处不再用于回复
已开始发送后取消时保留已确认, 部分完成或结果未知的回执依据, 不自动重发全文

## 图片与表情

图片只接受查询或可信工具返回的 `asset_id`, 表情只接受目录中的 `sticker_id`
将 `{"type":"image","asset_id":"ga_..."}` 或 `{"type":"sticker","sticker_id":"st_..."}` 放入同一次 `group_chat_reply.components`
模型不能填写服务器路径, URL, Base64 或原生表情编号; 任一组件失败时整个草稿不准备, 不静默丢图

「插件 → group_chat → 表情库」支持图片上传, 名称/标签/集合编辑, 预览, 停用和删除
「对话记录 → 平台对话 → 表情设置」为完整平台/账号/对话范围选择可用集合, 默认不启用任何集合
平台原生表情从适配器已确认目录中选择; OneBot 第一版目录来自已采集消息里实际出现的表情, 不是完整平台目录
表情数据保存到用户数据目录 `group-chat/catalog.db` 和 `group-chat/stickers`, 页面修改带修订校验和幂等键

`media_reply_enabled` 默认 true, `max_reply_images` 与 `max_reply_stickers` 默认各 4, 可设为 0 到 8
单图最多 10 MiB/2000 万像素, 单次回复图片合计 20 MiB, 支持 PNG/JPEG/WebP/GIF; 动画最多 200 帧/累计 4000 万解码像素
OneBot 支持组合图片与原生表情; Misskey 房间只支持一个图片附件, 图片和图片表情共同计数, 不提供引用/@/原生表情组件
图片显示位置服从平台附件布局, GIF 动画效果由平台决定; Misskey 必需图片上传失败时不发送剩下的文字

消息图片缓存最多 24 小时, 来源删除/撤回即撤销; 工具产物只允许登记时的主工作流轮次发送
缓存每平台最多 256 MiB, 整个数据根的消息图片缓存最多 1 GiB, 每平台最多 4096 个登记; 用户表情库单独限 2000 个活动条目/256 MiB
准备和提交均核验来源, 权限和连接; 发送任务实际结束前持有原文件, 不受入站临时文件清理或调用方提前取消影响
表情图片版本不可原位替换, 需删除后新建; 删除或停用阻止未提交草稿, 在途发送继续持有已经固定的版本

可信插件可以调用 `await satrap.core.group_chat.sdk.register_group_chat_asset(payload, mime_type, origin_ref)`
`payload` 是插件实际生成的字节或受控产物 `Path`, `origin_ref` 是当前主工作流启用的产物工具名
不得直接透传模型提供的文件路径; 登记检查真实格式并复制到平台缓存, 返回 `{"ok":true,"asset":...}` 或明确失败
后端自动维护会清理过期图片和无活动引用的表情文件, 租约仍在使用的文件等待发送结束后再回收

## 一次性提醒

| 工具 | 用途 |
| --- | --- |
| `group_chat_create_reminder` | 在当前群创建一次性固定文本提醒, `due_at` 与 `after_seconds` 二选一, 可选 `mention_user_ids` |
| `group_chat_list_reminders` | 默认列出当前群由当前发言者创建的提醒, 可按 `state` 过滤, `limit` 1-50 |
| `group_chat_get_reminder` | 查看本人提醒的期限, 状态, 修订与发送结果 |
| `group_chat_cancel_reminder` | 携带 `expected_revision` 取消尚未开始发送的本人提醒 |

正文 1-2000 字; 提及最多 10 个不重复成员, 不支持 @全体, 创建与发送前均核验当前群成员
`after_seconds` 为 10-31536000 的整数, `due_at` 至少 10 秒后且不超过一年
到期直接发送固定正文, 不调用模型, 不执行工具, 不触发新的 Agent 轮次; 模型只能读取和取消本人创建的提醒

```text
scheduled -> waiting_delivery -> sending -> sent / partial / failed / unknown
scheduled / waiting_delivery -> cancelled / paused / missed
paused -> scheduled / missed / cancelled
```

- `created` / `cancelled` 表示数据库已提交, 不表示已送达; 送达以任务的 `sent` 状态和消息 ID 为准
- 取消晚于发送开始时返回 `too_late_to_cancel` 与任务详情, 不改状态
- `unknown` 表示请求已提交但无法确认 (含崩溃时正在发送), 不自动重发; `partial` 已确认的分段不重发, 需要时人工新建任务
- 到期离线或重启错过时在 `reminder_catchup_seconds` 内等待, 短暂失败按 30/120/300 秒退避, 超出变 `missed`, 不批量补发旧提醒
- 发送前重新核验平台实例, 账号, 群, 插件开关与发起者权限; 停用平台, 群, 插件或提醒开关会暂停未发送任务, 再启用不自动恢复, 需在界面明确恢复
- 调度器随后端启停, 默认 5 秒兜底扫描; 终态提醒保留 30 天后清理, 活动提醒不清理
- 删除或清空消息档案时可勾选 `cancel_reminders` 取消关联的未发送提醒, 默认不取消

| 配置 | 默认值 | 范围 |
| --- | --- | --- |
| `reminders_enabled` | false | 同时控制创建和后台发送 |
| `reminder_catchup_seconds` | 600 | 0-3600, 0 表示错过即记为 missed |
| `active_reminders_per_member` | 20 | 1-100, 暂停的任务也占额度 |
| `active_reminders_per_group` | 200 | 1-1000 |

在「对话记录 → 平台对话 → 提醒」查看, 创建, 取消和恢复
管理 API 基础路径为 `/api/platforms/{adapter_id}/group-chat/reminders`, 范围由 `self_id` / `chat_id` 查询字段指定; 后端运行时支持列表, 详情, 创建, `/{id}/cancel` 与 `/{id}/resume`, 后端未运行时控制服务仍可列表, 查看和取消
取消与恢复需要最近读到的 `expected_revision`, 冲突返回 409

## 结果与错误

成功结果为 `{"ok": true, ...}`, 新对象带 `schema_version: 1`; 失败为嵌套形状 `{"ok": false, "error": {"code", "message", "retryable"}}`
常见 `code`: `invalid_argument`, `unsupported`, `unavailable`, `stale_call`, `permission_denied`, `not_found`, `revision_conflict`, `idempotency_conflict`, `quota_exceeded`, `invalid_time`, `invalid_cursor`, `source_unavailable`, `snapshot_expired`, `sources_not_read`, `asset_unavailable`, `archive_unavailable`, `unverified_target`, `already_prepared`
`retryable` 只说明能否安全地再次查询, 或在尚未提交时重试; 它不授权重新发送结果为 `unknown` 的消息
超时或失败不会包装成零条消息, 空结果或发送成功

## 配置与上下文

摘要默认启用, 最多选取 500 条/60000 正文字符, 还受当前模型剩余上下文的保守输入预算限制
模型需先读完全部快照分页, 再保存带来源 ID 的结论; 部分记录必须明确标记, 不承诺平台历史完整
快照有效 15 分钟, 每群最多 5 个, 活动平台合计最多 20 个; 来源删除或撤回立即撤销快照并清除关联摘要正文
已保存摘要默认最多保留 30 天, 不晚于来源过期; 查询旧摘要不能自动恢复已删除出处
在「对话记录 → 平台消息档案 → 选中对话 → 群摘要」查看, 查询, 查看来源和删除
摘要管理使用认证控制 API `/api/platforms/{adapter_id}/group-chat/summaries`, 支持冷热平台; 不调用平台撤回或修改模型上下文
摘要开关和三个预算/保留期字段均使用已有插件配置表单

插件页面和 Agent 插件配置都使用既有配置表单, 不需要手写 JSON

| 配置 | 默认值 | 范围 |
| --- | --- | --- |
| `allowed_groups` | 空 | 每行一个群 ID, 收窄查询和修改范围; 留空仍遵守平台权限 |
| `cross_group_query_callers` | 空 | 每行一个管理者账号 ID, 仅这些人的私聊可查询群列表; 留空禁用 |
| `self_nickname_enabled` | false | 独立开启机器人自身群昵称修改, 不要求安装管理插件 |
| `nickname_allowed_callers` | 空 | 每行一个允许请求修改机器人群昵称的成员 ID; 留空不增加调用者限制 |
| `message_limit` | 100 | 单次请求上限 1-100, 未指定条数时默认 20 |
| `member_limit` | 50 | 单次请求上限 1-50, 未指定条数时默认 10 |
| `text_budget` | 12000 | 查询正文字符预算 128-100000, 超出时标记截断 |
| `member_cache_ttl` | 60 | 成员快照复用秒数 0-300, 0 不复用首次查询缓存 |

成员分页快照有短期有效期; 过期游标要求重新查询, 不混用新旧列表
插件配置不改变平台消息档案保留期; 档案仍按平台设置保留, 默认 30 天
删除档案只影响检索和回复来源, 不修改模型上下文或撤回平台原消息

安装和首次激活时立即注入 skill, 保留 Agent 基础 system prompt
每轮前处理补充机器人身份, 当前群, 发言者 ID, 消息 ID, 引用, 提及及实际工具/平台能力, 消息窗口继续保留逐条身份
这些资料和工具结果均作为数据处理; 群成员自称管理员不会改变工具范围
停用插件后下一轮恢复原输出方式, 重启重新装配只保留一个 skill 指令块

## 群昵称与旧配置迁移

`group_chat_set_group_nickname` 只接受 `nickname` (最多 60 字符), 宿主固定当前群和机器人自身 ID; 空字符串用于清空
普通群成员身份的机器人也可提交自身修改, 平台最终权限和宿主审批仍生效; 无可用审批入口时拒绝写入
`group_admin_set_group_nickname` 另可接受目标 `user_id`, 由管理插件独立的写开关和调用者范围授权
两个修改工具都采用 `manual` 恢复策略, 不自动重试结果未知的写动作; `pending` 表示待审批, `succeeded` 才表示执行成功
审批前复核来源会话, 插件/工具开关, 最新持久配置, 调用者, 机器人账号和目标群; 撤销权限后旧申请不能执行

旧管理插件的五个重复查询入口已经删除, 查询统一由群聊插件提供; 旧群昵称工具名也不再注册或执行
配置解析时将显式保存的旧工具开关迁移到新名称, 再保存即写入规范化配置; 显式禁用状态不会变成启用
为了迁移查询而自动添加群聊插件时, 只启用原来开启的查询能力, 不额外启用回复, 修改, skill 或前处理
旧管理插件与现有群聊插件的允许群范围取交集, 无交集时明确拒绝迁移并要求调整配置; 群列表查询仍须单独设置管理者账号
平台协议的 `set_group_card` 与审批/审计动作 ID 保持不变, 新的模型工具参数 `nickname` 只在适配器边界转换为协议字段

## 开发验证

```powershell
python -m pytest tests/unit/test_group_chat_reply.py tests/unit/test_group_chat_plugin.py tests/unit/test_group_chat_service.py tests/unit/test_group_tool_migration.py -q
```

```text
cd satrap-ui
npm run test:e2e:group-chat
```

测试包含真实 SessionManager/Edictum 配置装配, 同步和异步主工作流, 流式与非流式调用, 原 OneBot 发送队列和平台消息档案
模型和平台网络响应使用隔离替身, 不向真实群发送消息; 实际模型调用策略和 QQ 客户端显示仍可在指定测试群现场核对
浏览器验证直接读取官方插件元数据, 检查能力展示, 查询范围和管理者名单, 自身群昵称修改开关, 零值保存, 刷新保持, 恢复默认与窄屏布局
