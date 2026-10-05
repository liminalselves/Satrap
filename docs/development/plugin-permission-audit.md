# 本轮插件权限与开关核查

日期: 2026-10-06

状态: 静态代码核查完成; 本轮仅记录计划与现状, 未修改权限或执行平台操作

本表保留 aca8329 的权限基线. 后续七项调用者名单已接入系统管理组的独立授权路径, 具体以 [统一权限契约](plugin-management-permission-contracts.md) 为准; 空管理组时仍沿用本表行为. 系统管理员不会跳过业务开关, 范围和审批, 有效管理员还纳入对应平台的好友删除保护

## 1. 核查范围

[PR #16](https://github.com/liminalselves/Satrap/pull/16) 的远端 base 为 `31604d6`, head 为 `3244000`. 比较这两个版本的插件目录, 真正新增的插件是 group_chat, friend_manager 和 memory. group_admin 在 base 中已存在, 本轮扩展了申请管理并迁移了查询工具, 因此一起核查

下表以本地 `fix/issue1` 分支 `aca8329` 的代码为准, 包含此前合并 main 后的权限收紧; 本地尚未提交的术语修改不改变这些权限判断. 例如 group_admin 的 `allowed_read_callers` 与 `high_risk_approval` 已在本地生效代码中存在, 不能把本表当作远端 PR head 的原样配置

base_take, satrap_coding, rag 和 session_commands 均不是本 PR 新增插件, 不纳入本次业务权限清单. sandbox 与编程权限另行处理

对应实施计划: [插件聊天命令计划与契约](plugin-command-plan-contracts.md)

## 2. 所有插件共同的限制

| 层次 | 当前机制 | 对新增聊天命令的影响 |
| --- | --- | --- |
| 安装与启用 | 当前 Agent 必须安装并启用插件; 工具, 处理器, 命令, skill 等子能力可分别关闭 | 必须检查命令子能力, 不因为后台入口存在就给平台成员开放 |
| 子能力默认值 | 已声明但没有显式配置的子能力默认开启; 敏感功能另受插件配置控制 | 不能把缺失的授权名单按子能力默认值解释为授权 |
| 身份与范围 | 宿主提供真实平台实例, 机器人账号, 对话, 发言者及来源消息; 部分功能核验成员关系和消息归属 | 命令参数不能自报审批者身份或平台管理员权限 |
| 平台能力 | 插件适用范围和适配器实际能力共同决定能否执行; 配置开启不代表协议支持或机器人具备平台权限 | 权限判断使用能力接口, 新的共享命令框架不写死平台名称 |
| 模型写操作来源 | 群管理, 自身群昵称与好友写操作在提交及审批执行时复核原会话, 工具启用状态, 路由和当前配置 | 人工批准不会重新授予已撤销的模型权限 |
| 人工管理入口 | 后台 API 使用服务鉴权; 没有直接映射到平台成员的审批身份和分角色授权 | 不能把群主/管理员身份自动当成后台管理权限, 也不能伪装为 panel 调用 |
| 配额与版本 | 条数, 字符预算, 保留时间, revision 和幂等键限制资源及并发 | 配额不是调用者授权, revision 也不是审批权限 |

依据: `satrap/edictum/plugin_spec.py:62`, `satrap/core/config/model_tool_authorization.py:46`, `satrap/core/backend/BackendManager.py:1392`, `satrap/core/backend/control_server.py:3121`

## 3. group_chat

以下各行均需插件及对应工具子能力启用. 除群列表工具外, 模型工具限定当前群, 并遵守平台可见范围及 `allowed_groups`; 该名单默认空, 表示不追加群限制

| 功能与工具 | 开关 / 授予条件 | 默认与实际范围 | 逐次审批 |
| --- | --- | --- | --- |
| 群资料与成员查询: `group_chat_get_group_info`, `group_chat_list_members`, `group_chat_find_members`, `group_chat_get_member` | 无额外成员名单; 需要对应平台读取能力 | 当前群可查询; 不能任填其他群号 | 无 |
| 消息读取与检索: `group_chat_get_message`, `group_chat_recent_messages`, `group_chat_search_messages` | 无额外成员名单; 需要档案或已核验的平台来源 | 限当前群记录, 包括未触发回复的已归档消息 | 无 |
| 私聊群列表: `group_chat_list_groups` | `cross_group_query_callers` 显式包含真实发言者; 必须在私聊 | 名单默认空, 禁用; 只列符合当前平台及插件范围的群, 不授予跨群读取正文的权限 | 无 |
| 修改机器人自身群昵称: `group_chat_set_group_nickname` | `self_nickname_enabled=true`; 若 `nickname_allowed_callers` 非空, 发言者须在名单中 | 开关默认 false; 名单默认空, 开启后所有当前群成员都可请求; 目标只能是机器人自己 | 遵守宿主 `set_group_card` 审批策略; 无额外强制审批开关 |
| 提取图片: `group_chat_get_message_assets` | `media_reply_enabled=true` 与平台图片能力 | 默认 true; 仅当前群已核验来源图片 | 无 |
| 查找表情: `group_chat_list_stickers` | `media_reply_enabled=true`, 平台能力, 表情条目 enabled, 当前群授权的集合 | 媒体开关默认 true; 群授权集合默认空, 不会因上传就自动给所有群开放 | 无 |
| 组合回复: `group_chat_reply` | 对应文本 / 引用 / @ / 图片 / 表情能力; 图片和表情还需 `media_reply_enabled` | 文本, 引用, @ 没有额外成员名单; 图片和表情默认允许, 但必须使用合法来源与已授权资产 | 普通回复无管理审批 |
| 摘要来源读取: `group_chat_prepare_summary`, `group_chat_read_summary_sources` | `summary_enabled=true`; 当前有效主工作流和其消息快照 | 默认 true; 限当前群指定时段; 快照只属于当前工作流 | 无 |
| 摘要保存: `group_chat_save_summary` | `summary_enabled=true`; 当前有效主工作流, 合法快照及出处 | 默认 true; 无专门的摘要写入者名单; 保存不等于已发送 | 无 |
| 摘要查询: `group_chat_get_summary`, `group_chat_list_summaries` | `summary_enabled=true` | 默认 true; 当前群内共享查询 | 无 |
| 创建提醒: `group_chat_create_reminder` | `reminders_enabled=true`, 当前主工作流, 本轮真实消息, 可用后台发送能力; 创建者及 @ 对象均需成员核验 | 默认 false; 开启后没有额外创建者名单; 只能在当前群创建, 禁止 @ 全体 | 无; 用户明确要求提醒目前由工具描述约束, 不是自然语言意图的强制权限判定 |
| 查询 / 取消本人提醒: `group_chat_list_reminders`, `group_chat_get_reminder`, `group_chat_cancel_reminder` | 对应工具启用; 取消需要当前主工作流和 expected_revision | 不额外要求 `reminders_enabled=true`, 允许停用创建后查询 / 取消已有本人任务; 不显示其他成员提醒正文 | 无 |

提醒到期发送另有持续授权校验: 当前 Agent 路由, 插件, `group_chat_create_reminder` 子能力, `reminders_enabled`, 平台与群范围仍须允许. 这些校验也影响后台人工创建的提醒, 不能简单套用“人工管理均与模型开关无关”. 权限停用会暂停; 单纯离线会等待恢复, 超出补发期限会记为错过; 暂停后恢复需明确操作

处理器 `group_chat.environment` 仅提供环境说明, skill 仅指导工具用法; 它们不代替以上实际授权

依据: `satrap/expend/plugins/group_chat/meta.yaml`, `satrap/expend/plugins/group_chat/tools.py:134`, `satrap/expend/plugins/group_chat/tools.py:339`, `satrap/core/group_chat/service.py:291`, `satrap/core/group_chat/service.py:542`, `satrap/core/group_chat/reminder_service.py:45`, `satrap/core/group_chat/reminder_host.py:96`, `satrap/core/group_chat/stickers.py:262`

## 4. memory

| 功能与工具 | 开关 / 授予条件 | 默认与实际范围 | 逐次审批 |
| --- | --- | --- | --- |
| 查询: `list_memories`, `get_memory` | 对应工具启用, `memory_mode` 不为 disabled | 默认 full; 群内默认查询共享群记忆与本人偏好; 显式查询其他成员偏好时核验该成员属于当前群 | 无 |
| 自动注入: `memory.memory_inject` | 处理器启用, `memory_mode` 不为 disabled | 默认开启; 群内只注入已生效的共享群记忆与真实发言者本人偏好; 不要求同时启用 get/list 工具 | 无 |
| 群内本人偏好增删改: `add_memory`, `update_memory`, `delete_memory` | `memory_mode=full` 且 `group_write_enabled=true`; 对应工具启用; 主工作流, 本人身份和当前来源消息 | 群写默认 false; 开启后没有额外成员名单; 不允许替其他成员修改偏好 | 无 |
| 共享群记忆增删改: 同上三个工具, kind 为 `group_rule` | 同样需要 full 与群写开关; 合法当前来源消息 | 只能提交提案; 群主或平台管理员也不能通过模型直接生效; 查询 / 注入不包含待审批内容 | 固定人工审批, 无关闭审批的配置 |
| Chat / 私聊等普通范围的记忆增删改: 同上三个工具 | `memory_mode=full` 与工具启用; 存储范围由宿主 / 普通调用配置绑定 | 默认 full; 不受 `group_write_enabled` 控制; 无独立写入者名单 | 无 |
| 既有 `/memory` 群命令 | 命令子能力开启; 读操作受 memory_mode 控制, 添加 / 删除也受 full 与 group_write_enabled 控制 | 目前仅列表, 添加本人偏好, 删除; 群内不提供 mode / clear 或审批 | 无审批子命令 |
| 后台人工编辑 / 审批 | 服务鉴权, 明确范围与 revision | 不依赖模型插件安装, memory_mode 或群写开关; 可以人工管理共享记忆及成员偏好 | 人工编辑直接生效; 决定模型提案时核验来源和基准版本 |

`group_rule` 是保留的存储类型名, 对人和模型使用“群记忆”. 这里的成员偏好属于群内数据, 并非其他成员私聊记忆. 当前读工具没有针对群内成员偏好的单独可见性开关

依据: `satrap/expend/plugins/memory/meta.yaml`, `satrap/expend/plugins/memory/runtime.py:15`, `satrap/expend/plugins/memory/tools/base.py:102`, `satrap/core/memory/service.py:86`, `satrap/core/memory/service.py:169`, `satrap/core/memory/scoped.py:173`, `satrap/core/memory/management.py:13`, `satrap/expend/plugins/memory/commands.py`

## 5. group_admin

| 功能与工具 | 开关 / 授予条件 | 默认与实际范围 | 逐次审批 |
| --- | --- | --- | --- |
| 荣誉与转发内容查询: `group_admin_get_honors`, `group_admin_get_forward` | `allowed_read_callers` 非空时须在名单中; 群范围遵守 allowed_groups 与平台管理范围 | 读取名单默认空, 不追加成员限制; 转发内容必须证明来源消息归属 | 无 |
| 查询入群申请: `group_admin_list_group_requests` | 读名单条件 + `request_managers` 显式包含发言者, 账号和申请范围有效 | request_managers 默认空, 禁用 | 无 |
| 撤回, 禁言, 全员禁言与匿名管理: `group_admin_recall_message`, `group_admin_ban`, `group_admin_whole_ban`, `group_admin_ban_anonymous`, `group_admin_set_anonymous` | `write_tools_enabled=true` 且发言者在非空 `allowed_callers` 中; 群 / 消息目标有效及平台有能力 | 写开关默认 false; 写名单空拒绝全部模型写操作 | 宿主按动作配置; ban / whole_ban / ban_anonymous 还受 high_risk_approval 强制审批 |
| 移出成员与管理员设置: `group_admin_kick`, `group_admin_set_admin` | 同上写授权 | 默认不可调用 | 宿主新配置默认需审批, 可显式改自动执行; high_risk_approval=true 时仍强制审批 |
| 群昵称, 群名与头衔: `group_admin_set_group_nickname`, `group_admin_set_name`, `group_admin_set_title` | 同上写授权; 本群昵称工具可选择其他成员, 不同于 group_chat 自身修改 | 默认不可调用 | 宿主按动作配置; set_name 还受 high_risk_approval 强制审批 |
| 退群 / 解散: `group_admin_leave` | 同上写授权, 平台实际权限 | 默认不可调用 | 宿主新配置默认需审批; high_risk_approval=true 时强制审批 |
| 同意 / 拒绝入群申请: `group_admin_handle_group_request` | 写授权 + request_managers; 原申请身份与目标群匹配 | 默认不可调用; 参数 approve 指平台申请决定, 不是 Satrap 审批者授权 | 宿主按动作配置; high_risk_approval=true 时强制审批 |
| 发送合并转发: `group_admin_send_forward` | 同上写授权, 合法内容及公共发送路径 | 默认不可调用 | 不属于宿主群管理动作审批账本, high_risk_approval 也不涵盖它 |

`high_risk_approval` 默认 false, 只表示不追加插件层强制审批, 不关闭宿主审批. 它覆盖 kick, ban, whole_ban, ban_anonymous, set_admin, set_name, leave, handle_group_request. 宿主在没有账号 / 群覆盖时, 默认需要审批的四类动作是移出成员, 设置管理员, 全员禁言和退群; 其他逐群管理动作默认自动执行. 账号默认与本群设置可以按动作覆盖

`allowed_groups` 默认空, 不额外限制. 与 group_chat 当前群读取不同, 该插件的工具可显式填写目标 group_id, 限制来自插件名单和机器人可管理范围; 普通读取名单空时不能推断为“只允许读发起消息所在群”. 入群申请从群内查询 / 处理时另有当前群归属校验

大部分 group_admin 工具即使未获写授权也可能出现在模型工具声明中, 在执行时返回拒绝; 目前动态隐藏主要针对申请工具. group_chat 与 friend_manager 的隐藏条件更细. 这是声明层体验差异, 本次未把它认定为权限绕过

依据: `satrap/expend/plugins/group_admin/meta.yaml`, `satrap/expend/plugins/group_admin/tools.py:112`, `satrap/expend/plugins/group_admin/tools.py:129`, `satrap/expend/plugins/group_admin/tools.py:150`, `satrap/expend/plugins/group_admin/tools.py:271`, `satrap/expend/plugins/group_admin/tools.py:344`, `satrap/core/config/group_approval.py:9`, `satrap/core/config/group_store.py:28`

## 6. friend_manager

| 功能与工具 | 开关 / 授予条件 | 默认与实际范围 | 逐次审批 |
| --- | --- | --- | --- |
| 好友列表与搜索: `friend_manager_list_friends`, `friend_manager_find_friends` | 真实发言者在 managers; 仅私聊, 同一机器人账号及可用好友能力 | managers 默认空, 全部工具禁用; 普通成员和群聊不注入 | 无 |
| 查询好友申请: `friend_manager_list_requests` | 同上 managers 与私聊限制 | 不要求 request_handling_enabled, 可以只授予读取 | 无 |
| 同意 / 拒绝好友申请: `friend_manager_handle_request` | `request_handling_enabled=true` 且发言者同时属于 managers 与 write_callers | 开关默认 false; write_callers 默认空, 拒绝写入 | 无额外人工审批, 授权后直接执行 |
| 删除好友: `friend_manager_delete_friend` | `delete_friend_enabled=true` 且同时属于两个名单; 目标确认且未受保护 | 开关默认 false; 机器人自己和宿主管理账号 / 账号保护名单不可删除; 插件 managers 与 protected_friend_ids 额外禁止模型删除 | 固定人工审批, 配置不能关闭 |
| 后台人工查询 / 处理 / 删除 / 审批 | 服务鉴权与同一账号范围; 删除仍受账号级保护 | 不依赖插件开关与模型调用者名单; 人工删除走直接执行路径 | 决定模型删除申请时仍复核原工具权限与来源存活 |

`protected_friend_ids` 默认空, 只增加模型删除保护; 后台人工遵守的是账号级保护名单及宿主提供的受保护管理账号. 配置名 managers / write_callers 表示模型工具调用者, 不是已经存在的聊天审批者名单

依据: `satrap/expend/plugins/friend_manager/meta.yaml`, `satrap/expend/plugins/friend_manager/tools.py:40`, `satrap/expend/plugins/friend_manager/tools.py:119`, `satrap/core/friends/service.py:286`, `satrap/core/friends/service.py:351`, `satrap/core/friends/service.py:372`, `satrap/core/friends/store.py:101`

## 7. 命令实现前需要明确的授权

以下是计划要求与设计取舍, 不代表当前已实现的授权字段或已证实的漏洞

| 命令行为 | 应用的授权契约 | 优先级 |
| --- | --- | --- |
| 决定群管理动作, 群记忆提案, 删除好友申请 | 显式的人工审批者名单与范围; 独立于模型 allowed_callers / managers; 复用现有状态机, 保留真实审批者身份 | P1, 三类审批都需要 |
| 查询待审批详情 / 动作结果 | 申请本人按范围查看自己的结果; 审批者才能查看其他人的待审批内容; 好友申请只在授权私聊中显示 | P1 |
| 人工执行好友申请处理 | 独立命令写授权; 仅私聊, 核验真实申请, 不伪装模型或后台入口 | P1 |
| 修改 / 删除共享群记忆或其他成员偏好 | 显式的人工管理权限; 普通成员只能修改本人偏好, 共享内容按提案流程 | P1 |
| 管理提醒 | 本人可查 / 取消本人任务; 管理全部任务和恢复暂停任务需授权; 创建 / 恢复仍需当前后台发送开关, 不能借命令绕过停用 | P1 |
| 删除共享群摘要, 修改表情集合授权 | 显式的群内人工管理权限; 普通查询无须逐次批准 | P2 |
| 机器人自身群昵称 | 当前为空即不限制; 是否收紧为显式名单属于另一个行为变更, 本轮不自动改默认值 | 另行决定 |
| 提醒创建 / 摘要保存 | 当前是开关 + 对应工具子能力, 无专门调用者名单; 新增命令不意味着要给所有普通功能追加审批 | 保持已有模型契约 |

权限实现放在宿主命令分发与业务服务入口; 插件独立声明自己的命令要求, 两个插件之间不读对方配置或调用对方工具. 平台群主 / 管理员角色只能作为经过适配器核验的可选授权依据, 不能直接当作 Satrap 管理权限

三类已有审批需要聊天入口, 但恢复提醒不是审批一条模型提案, 修改表情授权也不是平台写动作审批. 不应把所有后台操作塞进同一个 approve 命令

## 8. 本次验证边界

逐项对照四个插件的 metadata, 工具声明与实际服务校验, 覆盖 47 个工具, 两个处理器及现有 memory 命令. 对比 PR base / head 的插件目录确认新增范围. 本次没有修改实现或权限默认值, 没有进行在线平台验收; 未据此宣称不存在其他权限漏洞
