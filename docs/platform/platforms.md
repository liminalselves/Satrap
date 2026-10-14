# 平台接入

OneBot 的群目录、逐群配置、审批和迁移详见 [OneBot 群管理](groups.md)

Satrap 用统一的 `PlatformAdapter` 把不同聊天平台接入后端。平台消息会被转换成统一事件, 交给 `PipelineScheduler`, 再路由到对应 Session。

## 平台配置结构

```yaml
platforms:
  - id: misskey1
    type: misskey
    session_type: assistant
    settings:
      base_url: https://misskey.example.com
      api_token: ${MISSKEY_API_TOKEN}
      chat_enabled: true
      room_enabled: false
```

字段说明:

| 字段 | 说明 |
| --- | --- |
| `id` | 适配器实例唯一 ID |
| `type` | 适配器类型, 如 `misskey`, `onebot`, `aiocqhttp` |
| `session_type` | 入站消息使用的会话类配置名称; 未设置时优先使用与适配器类型同名的会话类, 再回退到全局 `default_session_type` |
| `settings` | 适配器专属配置 |

同一种平台可以配置多个实例, 只要 `id` 唯一即可。

## Misskey

```yaml
platforms:
  - id: misskey1
    type: misskey
    session_type: assistant
    settings:
      base_url: https://misskey.example.com
      api_token: ${MISSKEY_API_TOKEN}
      chat_enabled: true
      room_enabled: false
```

每个 OneBot 实例只绑定一个机器人账号: 配置 `self_id` 时必须匹配该账号, 留空时使用首条合法来源消息的账号。后续不同账号和自身发出的回声消息不会入队。消息按账号、会话类型、群或私聊目标及平台消息 ID 去重, 每实例至多保留 4096 条标识, 120 秒后过期; 不缓存正文。转换失败、取消或队列满不会将消息记为已接收。此去重不跨进程重启持久化, 无消息 ID 的输入不参与去重。健康状态 `ingress` 提供各类拒绝计数和缓存容量。

OneBot 发送方法返回 `SendReceipt`: `success` 表示收到平台消息 ID, `partial` 表示部分块成功后明确失败, `failed` 表示明确未完成, `unknown` 表示动作结果无法确认。回执保留已确认的 `message_ids`, 分块失败位置和固定错误原因, 不回显供应商响应正文。事件的 `last_send_receipt` 保存最近一次回执 (`last_business_receipt` 只保存业务输出回执); 部分成功或未知结果阻止调度器兜底重发全文。其他平台旧式 `None` 返回继续按原契约处理, 不伪造平台确认。本契约覆盖普通发送、流式降级与按长度拆分后的多块发送; 逐段发送证据与请求结论的归并规则见"发送证据与请求结论"。

### 回复引用与 @发送者

`settings.reply_with_quote` 与 `settings.reply_with_mention` 均默认 `false`, 只接受布尔值, 可在 `wake_group_overrides` 中按群覆盖, 时段规则不能修改。开启后, 事件在调用适配器发送前为群聊回复前置 `Reply(来源消息 ID)` 与 `At(发送者)`, @ 后自动补一个空格避免与正文粘连; 消息链中已有 Reply 或指向同一发送者的 At 时不重复添加, 对其他人的 @ 不影响。私聊回复、没有来源消息 ID 的事件 (如以 prompt 发起的手动唤醒) 不添加引用; 主动发送不经过事件, 也不会被装饰。流式回复只装饰首个非空块, 长消息拆分后只有首块携带引用/@; 空块不会提交给适配器。

### 引用回源与模型输入

`settings.quote_lookup` 默认 `true`, 可按群覆盖。已唤醒且通过限流的消息若顶层含 Reply 组件, 管线调用适配器的 `fetch_quoted_message` 回源首个引用: OneBot 使用 `get_msg`, 与手动唤醒共用 4 个并发槽位, 含等待的总超时 5 秒, 响应上限 64 KiB, 核验机器人账号与会话归属 (群消息必须来自同一群, 私聊引用必须来自同一对话双方), 群白名单收紧后不再回源; 不下载附件, 不递归回源引用内的引用。未唤醒的普通消息不会触发回源。

回源结果填充已有的 `Reply.chain/message_str/sender_id/sender_nickname/time`, 输入投影把它作为 `[引用 <发送者> 的消息: <原文>]` 前置到当前正文, 被引用者是机器人时标为“机器人自己”; 引用内文本截断到 2000 字符, 引用内图片/视频最多合并 4 个进入 `img_urls/video_urls`。回源失败、超时、越界或关闭时保留当前问题并前置 `[引用了一条无法获取原文的消息]`, 不阻塞处理。引用原文不参与命令解析。事件的 `input_projection` extra 保存本次投影与状态说明。

`settings.wake_on_quote_self` 默认 `false`, 可按群覆盖。开启后, 未被 @/唤醒词/别名命中的群消息若引用了消息, 会在唤醒阶段用同一回源预算确认被引用者: 是机器人账号则视为明确唤醒 (决策 `quote_self`, 不受自动参与冷却限制), 引用他人或回源失败保持未唤醒且不消耗模型额度; 该次回源结果直接复用于后续输入投影, 不重复请求。

### 合并转发入站与出站

入站 `forward` 消息段转换为 `Forward(id)` 组件, 正文占位为 `[转发]`; 实现随消息段内联 `content` 节点列表时直接解析为 `Forward.nodes`, 不再回源。`settings.forward_lookup` 默认 `true`, 可按群覆盖。已唤醒且通过限流的消息若顶层含未解析的 Forward, 管线调用适配器的 `fetch_forward_message` 回源: OneBot 使用 `get_forward_msg`, 与引用回源共用 4 个并发槽位, 含等待的总超时 5 秒, 响应上限 256 KiB, 每事件最多回源 2 条转发, 每条至多 20 个节点; 核验机器人账号, 群白名单收紧后不再回源; 不递归展开节点内的嵌套转发, 不下载附件。兼容 `messages`/`message` 两种响应字段与 `type/data` 包装或直接字段两种节点形态。

输入投影把已解析转发渲染为 `[转发消息 N 条: - <昵称>: <摘要> …]` 前置到当前正文, 单条转发投影截断到 2000 字符; 节点内图片/视频与引用共享 4 个媒体预算进入 `img_urls/video_urls`; 未解析的转发保留 `[转发]` 占位。转发内容作为用户提供的资料, 不参与唤醒判断或命令解析。

出站消息链在 `Node/Nodes` 边界拆分为普通段与转发段并按原顺序发送: 普通段走 `send_private_msg`/`send_group_msg`, 转发段走 `send_private_forward_msg`/`send_group_forward_msg`, 不隐式把整条链包装成转发。例如 `Plain(A), Nodes(B), Plain(C)` 依次发送普通 A、转发 B、普通 C。转发接口返回动作未找到 (retcode 10002/1404) 时降级为逐块普通分段发送, 嵌套转发保留 `[转发]` 占位; 其他动作拒绝返回 `failed/action_rejected`。转发段与普通块共用同一逻辑回复的执行权与失败即停止语义, 回执聚合保留已确认消息 ID。

### 群管理动作与能力矩阵

OneBot 实例持有 `OneBotAdmin` 动作集 (`adapter.admin`), 通过同一 aiocqhttp 客户端执行管理动作, 每个动作含等待最长 10 秒。群号/QQ 号只接受纯数字字符串, 群动作执行前校验实例群白名单; 响应数据收窄为白名单字段, 不回显平台响应正文。错误归一为三类: 接口缺失 (retcode 10002/1404 或方法不存在) 报 `UnsupportedAdminAction`, 平台明确拒绝报 `AdminActionRejected`, 超时或传输异常报 `AdminActionUnconfirmed` (结果未知, 不假定成功)。适配器 `admin_capabilities()` 返回各动作 `supported`/`unavailable` 状态并随 `get_stats` 的 `capabilities` 字段暴露; 客户端未连接时全部记为不可用。

内置 `group_admin` 插件把动作暴露为模型工具 (仅 `session_type: platform` 会话)。执行时按入站 `CallOrigin` 解析来源实例与调用者身份, 私聊上下文必须显式指定 `group_id`, 群聊默认当前群。权限门槛: 写操作要求插件配置 `write_tools_enabled: true` (默认关闭); `allowed_callers` 逐行列出允许触发写操作的 QQ, 留空不限制; `allowed_groups` 逐行收窄可用群, 平台实例群白名单始终生效。工具不缓存适配器引用, 实例重载后按来源 ID 重新解析。读工具 `recovery_policy` 为 `retry` (可安全重试), 写工具为 `manual` (结果未知时不自动重试, 避免重复踢人/审批)。

群资料, 成员和消息查询统一由独立的 `group_chat` 插件提供, 详见 [群聊插件](../plugins/group-chat-plugin.md)
两个插件的写开关和调用者范围独立, 通过宿主共用审批服务; 自身群昵称修改不需要安装管理插件

能力矩阵 (全部为 OneBot v11 标准动作):

| OneBot 动作 | 工具名 | 读写 | 主要参数 | 响应收窄 |
| --- | --- | --- | --- | --- |
| get_group_list | group_chat_list_groups | 读 | 无, 仅已配置管理者的私聊 | 至多 512 条且受预算限制, 群号/群名/人数, 仅允许群范围 |
| get_group_info | group_chat_get_group_info | 读 | 无, 固定当前群 | 群号/群名/人数 |
| get_group_member_list | group_chat_list_members | 读 | limit ≤50, cursor, 固定当前群 | 分页返回成员 ID/账号昵称/群昵称/角色 |
| get_group_member_info | group_chat_get_member | 读 | user_id, 固定当前群 | 成员 ID/账号昵称/群昵称/角色 |
| get_group_honor_info | group_admin_get_honors | 读 | group_id 可选, honor_type | 实现返回的荣誉数据 |
| delete_msg | group_admin_recall_message | 写 | group_id (默认当前群, 受实例白名单与插件 allowed_groups 限制), message_id | 无返回 |
| set_group_kick | group_admin_kick | 写 | user_id, reject_add_request, group_id 可选 | 无返回 |
| set_group_ban | group_admin_ban | 写 | user_id, duration 0-2592000 秒, group_id 可选 | 无返回 |
| set_group_whole_ban | group_admin_whole_ban | 写 | enable, group_id 可选 | 无返回 |
| set_group_anonymous_ban | group_admin_ban_anonymous | 写 | flag, duration, group_id 可选 | 无返回 |
| set_group_admin | group_admin_set_admin | 写 | user_id, enable, group_id 可选 | 无返回 |
| set_group_anonymous | group_admin_set_anonymous | 写 | enable, group_id 可选 | 无返回 |
| set_group_card | group_admin_set_group_nickname | 写 | user_id, nickname ≤60 字符, group_id 可选 | 宿主审批/执行状态 |
| set_group_card | group_chat_set_group_nickname | 写 | nickname ≤60 字符, 固定当前群及机器人自身 | 独立默认关闭, 宿主审批/执行状态 |
| set_group_name | group_admin_set_name | 写 | name 1-60 字符, group_id 可选 | 无返回 |
| set_group_special_title | group_admin_set_title | 写 | user_id, title ≤18 字符, group_id 可选 | 无返回 |
| set_group_leave | group_admin_leave | 写 | group_id 可选, dismiss | 无返回 |
| set_friend_add_request | friend_manager_handle_request | 写 | request_id, approve, remark ≤60 字符, flag 由宿主解析 | 动作记录 |
| set_group_add_request | group_admin_handle_group_request | 写 | group_id (默认当前群, 受群范围限制), flag, sub_type add/invite, approve, reason ≤120 字符 | 无返回 |
| get_msg | group_chat_get_message | 读 | message_id, 固定当前群 | 消息 ID/时间/发送者/原文, 优先档案, 回源核验当前群并遵守本地删除标记 |
| get_forward_msg | message_forward_read | 读 | source_message_id, source 可选 | 转发预览, 明确标识截断; 不用于发送 |

`message_forward_read` 从来源消息的顶层转发组件取得转发 ID, 支持群聊和私聊, 回源核验消息归属, 账号和连接代次, 不从正文或嵌套节点推断授权
`message_forward_send` 按原消息 ID 合并转发或原生转发已有卡片, 不使用截断预览; 原文模式接口缺失时明确失败, 不沿用普通自定义 Node 的文字降级
`message_forward_compose` 保留机器人创建文字合集的能力, 与原文转发分开; 权限和迁移见 [原消息转发](../plugins/message-forward.md)

好友/加群请求的 `flag` 来自通知事件 (见文末"通知与请求事件"), 工具只做显式审批, 不做任何自动同意或拒绝。布尔参数严格校验, 拒绝真值语义; 写操作被平台拒绝或结果未知时按 `manual` 策略交由用户确认, 不自动重放。

### 长消息拆分与发送顺序

`settings.message_text_limit` (默认 2000, 允许 64-32000 的整数) 限制每条消息的文本字符数。超长回复在适配器实际发送前拆分: 优先在段落 (`

`) 边界断开, 其次在换行处断开, 单段仍超限时按上限硬切; 图片、@ 等非文本组件不可切开并保持原顺序。拆分不修改原消息链, 拼接后的文本与原文一致。

拆分后的各块按顺序发送, 首个非 `success` 的块之后停止, 回执聚合为 `partial` (已有确认 ID 且明确失败) 或 `unknown` (结果不明), 并记录 `failed_index`; 不会重发已确认块。

### 发送证据与请求结论

回复类 send_message 在支持的平台上逐段记录发送证据 (见 [运行数据布局](../core/data-layout.md) 的 `.satrap/data/manual_wake_store.json`): 转发段、文件段、普通文本分块各为一段, 段状态为 `planned → submitted → sent`/`partial`/`failed`/`unknown`, 未尝试的段由收尾标 `skipped`。计划在发送 I/O 之前落盘, 每段在 I/O 前推进 `submitted`, 得到回执后立即保存该段结果, 因此崩溃或取消时不会把"已发出但未确认"记成计划中。证据记录失败时该段按 `unknown` 处理 (收尾带 `tracking_incomplete`), 不冒充已送达。

发送用途区分 `business` (业务输出) 与 `error_feedback` (错误提示): 错误提示的回执不进入请求送达证据, "错误提示发送成功"不会把业务失败改写成已送达。旧版本记录缺 `purpose` 字段时按用途未知处理: 只提高结论的保守程度, 不作为业务已送达的依据。

已受理的手动唤醒请求要求发送证据: 发送前记录不可用 (存储降级、容量拒绝或落盘失败) 时拒绝业务发送并返回 `unknown`/`tracking_unavailable`, 不发出无法确认的业务输出。发送收尾归发送子任务所有, 外层取消或超时也保留已落定的段证据, 收尾有界 (2 秒), 超时或被取消时尝试记录保持未终结, 由后续可信确认精化, 期间绝不重发。只有收尾调用正常返回未生效 (含降级期) 才记 warning 并保持记录未终结, 异常、超时与取消各按自身分支记一次, 不把"业务已发送"与"状态已持久化"混成同一个成功标志。请求状态回写按结果码分级记录: 同目标终态的重复写入与不可改写的 `unknown` 结论属明确幂等, 只留 debug; 记录不存在 (当前文件与归档都没有) 与终态改写都是异常拒绝, 记 warning 且保留原终态不覆盖。平台停止与热重载的清理按"撤销内存票据 → 推进持久状态 → 清理诊断"顺序执行, 持久化回写在线程池完成, 文件锁争用不占用事件循环, 但调用方仍等到落盘结束后才启用新实例。

发送兜底 (分块, 转发段, 文件段) 遇到转换或未预期异常时统一返回 `failed`/`message_conversion_failed`, 并记录目标会话与异常类型, 便于定位是哪一个组件转换失败; 目标不在允许范围仍返回 `failed`/`target_unavailable` 且不升级为异常日志。

请求终态按同一 `request_id` 关联的全部业务尝试归并, 采集自段证据而不是最后一次回执:

| 事实 | 请求状态 |
| --- | --- |
| 确定尚未提交任何业务写动作 (取消/在发送前退出) | `failed`, detail 为 `cancelled_before_send` 等明确原因 |
| 有已提交未确认的段 (含重启后无法确认的尝试) | `unknown`, 保留已确认段与原因, 不自动重发 |
| 有已确认前缀, 其余明确失败或未尝试 | `partial` |
| 预定业务输出全部确认 | `sent` |

进程内回执 (`last_send_receipt`/`last_business_receipt`) 只在平台不记录尝试时作为补充证据; 段证据优先, 调用方的乐观状态不能把未确认段提升为已送达。重启时 `submitted` 段转 `unknown`, `planned` 段转 `skipped`, 已确认段保持原状后重新归并。

文件段在没有经实现验证的上传动作时的兼容回落 (先把文件消息当普通消息发出) 只有消息动作返回, 不构成文件交付证据: 该段记为 `unknown` 并在回执原因中带 `file_delivery_unconfirmed`, 沿用停止后续段、禁止兜底全文重发的策略, 已确认的前缀保留。平台明确拒绝该消息动作时才是 `failed`。

每个 OneBot 实例维护有界的逻辑回复队列: 同一平台会话的多轮回复串行, 不同会话互不阻塞; 至多 64 个等待或执行中的逻辑回复, 等待执行权最长 30 秒。满载、等待超时或适配器已关闭时返回 `failed` 且原因为 `send_queue_unavailable`, 不提交任何动作。适配器终止时取消未完成的发送任务。

常用 settings:

| 字段 | 说明 |
| --- | --- |
| `base_url` | Misskey 实例地址 |
| `api_token` | API token |
| `chat_enabled` | 是否启用 chat 消息 |
| `room_enabled` | 是否启用 room 消息 |

Misskey 适配器会处理 note, chat, room 等会话来源, 并尽量把文件和图片转换为统一消息组件。

Misskey streaming 协议要求把 token 放在 WebSocket URL 的 `i` 查询参数中。Satrap 会对 token 做 URL 编码, 并向底层 WebSocket 客户端注入脱敏日志器, 避免连接调试日志记录明文或编码后的 token; 反向代理、抓包工具和外部观测系统仍应避免采集完整查询字符串。

## OneBot / aiocqhttp

```yaml
platforms:
  - id: qq_bot
    type: onebot
    session_type: assistant
    settings:
      host: 127.0.0.1
      port: 6700
      access_token: ${ONEBOT_ACCESS_TOKEN}
      enable_private: true
      enable_group: true
      group_whitelist: []
      wake_words: ["小助手"]
```

`type` 可以使用 `onebot` 或 `aiocqhttp`。

群消息默认仅由真实 @机器人或 `wake_words` 中的文本触发; 唤醒词区分大小写, 匹配当前顶层正文中的子串, 不扫描引用、转发和附件。@全体、@其他人及单独附件不触发, 私聊保持直接处理。未唤醒群消息不会消耗模型调用限流额度或产生限流反馈。

`group_whitelist` 为群 ID 列表, 空列表允许所有群, 非空仅处理列出的群, 仍服从 `enable_group`。群 ID 推荐写为字符串。范围同时限制群消息接收和主动发送, 不限制私聊。平台页面支持逐行编辑群 ID 和唤醒词; 已启动 OneBot 的唤醒词、群范围、上下文范围和私聊/群聊开关可通过重载在线应用; 连接、执行容量及会话绑定变更通过定向替换实例应用, 启动失败时恢复旧实例。页面分别显示保存和生效版本。引用唤醒与高级参与规则尚未在本批次实现。

常用 settings:

| 字段 | 说明 |
| --- | --- |
| `host` | 反向 WebSocket 监听地址 |
| `port` | 反向 WebSocket 监听端口 |
| `access_token` | OneBot access token; 强烈建议始终配置。未配置时本机任意进程可伪造事件, 且非回环地址 (`host` 不为 `127.0.0.1`/`localhost`/`::1`) 监听会拒绝启动 |
| `enable_private` | 是否处理私聊 |
| `enable_group` | 是否处理群聊 |

## 多平台路由

后端会根据平台实例, `session_type` 和用户来源构建会话 ID。不同平台实例上的同一用户会进入不同上下文, 避免消息串线。平台管理页可直接从已注册且启用的会话类中选择 `session_type`。

### 按对话类型绑定 Agent

平台编辑页的“Agent 路由”按适配器声明生成私聊, 群聊及其他类型的选择器, 后端停止时也能配置
每类可继承平台默认或指定完整 Provider/配置名; 群详情展示平台默认 → 群聊类型 → 本群覆盖的实际继承链
缺少或停用的配置保留可见值并要求重新选择, 不默默替换; 切换适配器类型也不会自动删除旧绑定
保存与运行时应用分别展示状态, 应用失败可以重试; 并发保存冲突保留当前草稿
被平台默认, 对话类型或单群绑定引用的配置禁止删除或重命名, 控制服务与后端返回 409 `config_in_use` 并列出引用
引用扫描失败返回 503 `agent_reference_scan_failed`, 离线 CLI 同样拒绝变更; 更换绑定后再删除配置

`session_provider` 与 `session_type` 作为平台默认, 可用 `session_bindings` 分别绑定私聊和群聊:

```yaml
session_provider: edictum
session_type: general-agent
session_bindings:
  private:
    mode: value
    provider: edictum
    config_name: private-agent
  group:
    mode: value
    provider: edictum
    config_name: group-agent
```

每个显式绑定必须同时提供 Provider 和命名配置, `mode: inherit` 只继承平台默认, 不携带值
优先级为单群显式绑定 > 对话类型绑定 > 平台默认; 群内模型, 提示词和插件覆盖作用于最终绑定
对话类型由适配器声明, OneBot 支持 private/group, Misskey 另外区分 discussion 帖子讨论, 新适配器可声明自己的类型
启用平台时显式对话类型绑定必须可用, 不存在或已禁用的显式 Agent 不会静默回退

首次启用该字段后使用版本化的对话路由, 私聊和不同群各自隔离; 原 legacy_user 群范围升级为按群内成员隔离
即使选择相同 Agent, 私聊和群聊也不会共用上下文; 指定群共享范围时, 同一群的成员共享上下文
已启用新路由的对话在恢复平台继承后继续隔离, 不回到旧用户共享命名空间
更换有效绑定或范围时持久推进代次, 没有新消息的连续切换也会生效; 旧排队事件与执行中回复不能向新路由发送
已有对话保留可查看, 不自动把旧上下文拼接到新 Agent; 路由代次保存在原平台数据库, 不新增配置 JSON

手动创建 Session 时可以通过 `adapter_id` 写入会话初始化参数, 供会话类或插件使用; 它不会改变平台入站路由, 入站会话类由平台的 `session_type` 决定:

```bash
satrap session create assistant --id demo --adapter-id misskey1
```

## 平台管理命令

```bash
satrap platform list
satrap platform show misskey1
satrap platform add misskey1 --type misskey --session-type assistant --set base_url=https://misskey.example.com api_token=${MISSKEY_API_TOKEN}
satrap platform update misskey1 --type misskey --session-type assistant --set chat_enabled=true
satrap platform remove misskey1
satrap platform wake qq_bot --group 20000 --user 30000 --prompt "请总结刚才的讨论"
```

`platform wake` 向运行中的后端提交 OneBot 群手动唤醒, 与控制面板会话页的手动唤醒弹窗共用 `POST /api/platforms/wake` 契约: `--prompt` 与 `--message-id` 互斥, 均省略时处理该群与成员范围内待处理的文字和图片; `--request-id` 省略时自动生成, 重复提交同一 ID 只入队一次。操作者身份固定为服务端已认证的管理主体, 不从命令行参数读取。返回 `accepted`/`already_pending`/`no_pending`, 被拒绝时以非零退出并给出原因。

被拒绝时响应体为 `{"status": "rejected", "request_id": ..., "reason": <稳定原因码>}`: 参数类原因 (`invalid_request_id`, `invalid_prompt`, `invalid_message_id_or_conflicting_prompt`, `explicit_group_and_route_user_required`) 返回 400, 其余 (`queue_full`, `request_capacity`, `store_unavailable`, `request_id_conflict`, `adapter_changed`, `adapter_unavailable`, `backend_unavailable`, `source_unavailable`, `message_lookup_failed_or_scope_mismatch`, `message_convert_failed`, `invalid_fields_or_operator`) 返回 409; 状态查询接口沿用 404 `not_found` 与 503 `store_unavailable`/`store_degraded`。存储锁等待超时或锁文件不可用按 `store_unavailable` 上报, 不退化成通用 500。控制面板把原因码翻成可操作文案, 未知码回显原始码; CLI 的 HTTP 错误在响应只有 `reason` 时同样给出该码。

## 自定义适配器

继承 `PlatformAdapter`, 实现 `meta()`, `run()` 和发送方法, 再用装饰器注册:

```python
from satrap.core.platform import PlatformAdapter, register_platform_adapter


@register_platform_adapter("my_platform")
class MyPlatformAdapter(PlatformAdapter):
    async def run(self) -> None:
        ...

    def meta(self):
        ...

    async def send_text(self, session_id: str, text: str):
        ...
```

接收到平台消息时, 适配器应构造统一事件并提交到事件队列。发送消息时, 优先实现 `send_message()`, 最低也要实现 `send_text()`。


## 事件执行容量

平台 settings 可配置 `event_queue_capacity` (默认 256)、`event_pending_capacity` (默认 256)、`event_concurrency` (默认 8) 和 `event_queue_ttl` (默认 120 秒)。容量与并发数必须是正整数, TTL 必须是有限正数。保存后重载会定向重建该实例以应用执行容量变更。

同一来源会话按顺序处理, 不同来源会话可并发; 路由到相同最终 Session 的模型调用与回复发送保持串行。队列满时静默丢弃最新消息并记录计数, 过期待处理消息不会再调用模型。运行状态中的 `event_queue` 包含队列容量、排队/执行数量、丢弃及过期计数。关闭分发器会取消执行任务并清理等待事件的临时资产。


## OneBot 上下文范围

`settings.context_scope` 支持以下值:

| 值 | 会话范围 |
| --- | --- |
| `legacy_user` | 保留旧映射, 同一成员在同一适配器的多个群可能共享上下文 |
| `group_member` | 按适配器、机器人账号、群和成员隔离 |
| `group` | 显式按适配器、机器人账号和群共享, 群成员共享模型上下文 |

省略字段的旧配置继续使用 `legacy_user`; 前端新建 OneBot 实例默认选择 `group_member`。私聊仍使用原用户路由。切换范围使用独立映射并保留旧会话, 切回原范围可继续访问原映射, 不复制或合并历史。

群共享会话不归属于首位发言者的用户会话列表, 成员身份仍随每次请求保留。Provider 的 context_key 在成员范围使用真实用户 ID, 群共享范围使用群 ID; 独立的会话标识不作为用户 ID 注入 Provider。管理工具的逐次权限上下文仍将在后续批次接入。


## 配置保存与生效

从 YAML/JSON 启动时, 后端记录实际文件路径, 重载时重新读取该文件的平台配置。以字典构造的嵌入式后端使用内存配置, 不猜测默认配置文件。`/api/config/reload` 的 `platforms` 与健康响应的 `platform_config` 返回各实例的 `saved_revision`、`active_revision` 和 `status`。

当前支持 OneBot 策略在线应用, 事件持有接收时的策略副本; 群范围在执行/发送前仍检查当前权限。文件读取或校验失败保留旧生效值, 返回 `failed`。连接、会话绑定、容量以及新增/删除/停用实例通过定向生命周期协调应用, 不打断其他平台工作器。新实例确认就绪后才更新生效版本, 失败时恢复旧实例; 如果旧实例也恢复失败, 生效版本返回空且状态为 failed。后端未运行或没有分发器时保留 pending_restart。

会话绑定的可用性只在平台进入启用状态时强制校验。`enable: false` 的平台不建立连接也不接收消息, 因此它的 `session_type` 与 `session_provider` 允许缺失或不可用: 配置保存与重载都会成功, 实例保持惰性 (不产生分发工作器), 页面显示为已生效但未启用, 也不会为此解析会话定义。`enable: true` 的平台按绑定状态分三种处理: 绑定可执行时照常连接与收发; 绑定的**会话定义存在但被禁用**时平台照常建立连接并启动, 因此 OneBot 的 notice、群管理与群目录以及绑定到其他有效定义的其他群都不受影响, 但该绑定的入站消息在唤醒判定与窗口之前被静默丢弃 —— 不进入唤醒窗口或定时队列, 不消耗限流额度, 不做媒体解析, 也不创建会话, 所以重新启用后不会把禁用期间的消息补进模型; 定义不存在或 `session_provider` 本身不存在时按既有的失效语义拒绝应用 (`status: failed`, `old_runtime_preserved: true`), 不创建实例, 也不回退到 `default_session_type`。

判定只读会话定义注册表且逐事件重新读取, 因此重新启用定义后下一条消息即恢复, 不需要重建平台或重启后端。被拒消息不向发送者外发反馈, 但会在请求诊断的 `projection` 阶段留下 `binding_disabled` 或 `binding_invalid` 原因码: 前者属正常配置状态只记 DEBUG, 后者属配置错误记 WARNING 并带原始原因 (`未知会话 Provider` / `会话定义不可用` / `名称存在歧义`)。群级绑定与平台绑定各自独立判定, 只影响被判定的那个绑定。保存阶段的配置校验只看 `id`、`type`、`settings` 与 `enable`, 不检查绑定是否存在, 因此这类配置可以正常保存。

OneBot 就绪探针核验当前实例的独立本地 HTTP 标识, 不将端口被其他服务占用当成启动成功。该检查仅验证 Satrap 监听服务, 不表示 SnowLuma 或 QQ 已连通。
# 自动参与的群与时段覆盖

OneBot 默认使用 `wake_mode: explicit`, 只在明确唤醒后调用会话。可选 `frequency` 按窗口消息数量触发, 或 `necessity` 按本地必要性评分触发。`wake_message_threshold` 默认 3, `wake_cooldown` 默认 30 秒, `wake_score_threshold` 默认 0.65。含正文或图片的消息计数; 只有 @全体、语音或文件的消息不计数。

### 未唤醒消息的短期上下文

三种唤醒模式都收集通过来源、权限和会话绑定检查的群消息, 唤醒策略只决定何时调用会话。窗口记录顶层文字、图片的原始 `file`/`url` 和接收时的昵称, 收集时不下载图片。`context_scope: group` 共享本群成员的窗口; `group_member` 与 `legacy_user` 只读取当前成员的窗口。平台实例、机器人账号、群、Provider、会话类型和群路由代次均隔离。

- 每个窗口最多保留 32 条消息、8192 个文字字符、32 个图片引用, 单条最多 8 个图片引用; 最多 512 个窗口, 消息在 120 秒后过期。超过窗口容量时淘汰最早消息, 单条超额图片保留省略说明。图片引用字段超过 4096 字符时不保留其来源
- 例如先发送未带 @ 的图片, 随后 `@机器人 这张图是什么`, 会将仍在窗口里的图片与当前提问一并处理。单独 `@机器人` 也可处理已有窗口; 尚未提交的窗口可由管理端手动唤醒, 自动参与模式还可由最长等待触发
- 当前消息的文字及补全内容优先占用 `input_text_limit`; 余量给最近的窗口消息, 最终窗口按接收顺序呈现。来源标记、图片占位和分隔符均计入预算, 没有文字额度显示来源的历史图片不会下载
- 图片与当前消息、引用、转发共用 `input_media_limit`: 当前媒体优先, 其次引用与转发, 最后最近的窗口图片。同来源图片只解析一次; 窗口图片以 `[图片 N]` 对应本轮图片输入顺序, 超出预算或无来源时标记 `[图片未纳入本次输入]`, 下载失败时标记 `[图片读取失败]`
- 消息来源显示为 `[用户 小明 (ID 123), 消息 789]`, 昵称缺失时依次使用本群昵称、用户 ID。显示字段移除控制字符并折叠换行, 当前消息的昵称标记仅使用正文和补全内容之外的剩余额度; 权限和路由仍使用真实 ID。窗口保留接收时昵称, 后续改名不修改旧消息的来源
- 限流前已收集的内容仍留在窗口, 成功认领后只提交一次。认领后失败不会自动重放; 图片临时文件归属于实际处理事件并在该事件结束时清理。该窗口用于尚未提交的短期消息, 不作为长期媒体缓存

### 机器人自身昵称与本群昵称

OneBot 普通消息通过唤醒、权限和限流检查后, 通过 `get_login_info` 获取机器人账号昵称; 群聊另通过 `get_group_member_info` 查询机器人自己的本群昵称。模型输入以 `[你当前的平台机器人身份: 账号 ID 10, 账号昵称 机器人乙, 本群昵称 本群助手]` 标明当前账号身份, 与发言者昵称、会话人设名称和配置中的唤醒别名区分。私聊只带账号昵称, 普通、定时和手动唤醒共用此流程; 命令不查询或附加身份资料。

资料按平台实例、账号、连接和群隔离, 成功及失败结果最多缓存 60 秒, 重连后立即失效。每次动作最多等待 1 秒, 含并发等待的身份补全总上限为 3 秒; 查询失败或返回的账号、群不匹配时不使用该结果, 仍继续处理原请求。账号昵称查询失败时可使用已核验的群成员昵称; 本群昵称查询失败时仍保留已确认的账号昵称。

身份标记计入 `input_text_limit`, 优先保留当前问题, 剩余额度先预留身份资料再分配历史窗口。没有足够额度时省略身份标记, 不截掉当前问题; 资料字段移除控制字符、折叠换行且最多保留 128 字符。纯媒体读取失败仍走原有失败反馈, 身份资料不会单独触发模型调用。

以下示例在本机时间 23:00 至次日 07:00 停止自动参与, 但群 123 使用独立的评分策略。显式 @机器人不受自动参与时段和冷却阻挡。

```yaml
wake_mode: frequency
wake_time_rules:
  - start: "23:00"
    end: "07:00"
    settings:
      wake_mode: explicit
wake_group_overrides:
  "123":
    wake_mode: necessity
    wake_score_threshold: 0.8
```

优先级为平台默认值、时段设置、群级设置; 重叠时段按列表顺序覆盖。群级规则只改变唤醒参数, 不改变白名单、权限和会话范围。

### 频率模式的有效阈值与 talk_value

频率模式的条数阈值只有一个有效来源, 按顺序解析: 显式 `wake_message_threshold` (来自平台/时段/群的合并结果) → `wake_talk_value` 映射 → 内置默认 3。解析结果带来源说明, 试算与配置预览据此展示 (`sources` 与 `automatic.threshold`), 不写回持久化配置。

- `wake_talk_value` 为 0 时映射为"任何窗口长度都达不到": 频率模式的正常判断与**到期最长等待**都不触发, `wake_max_wait` 不补偿这一关闭; 窗口正文保留, 仍可由显式 @、唤醒词或手动唤醒处理
- 合并结果里存在显式 `wake_message_threshold` 时, `wake_talk_value=0` **不是**有效关闭: 阈值取显式值, 界面显示"被显式阈值覆盖"而不是"自动参与已关闭"; 需要停用全部自动参与时设置 `wake_mode: explicit`
- 正值 `wake_talk_value` 不受影响: 到达最长等待仍按 `max_wait` 触发; `necessity` 模式的评分与到期补偿不受 `wake_talk_value` 影响
- 输入预算与频率偏好都可在平台表单编辑 (`input_text_limit` 1–200000, 默认 20000; `input_media_limit` 1–32, 默认 8; `wake_talk_value` 0–1)。三者是仅平台级的逐事件配置, 不进入群/时段覆盖, 保存后对下一事件生效, 已冻结事件保留其原策略快照, 不需要重连平台; 表单里留空表示未设置, 0 按数字保存
- 表单里的 talk_value 提示是静态语义说明 ("留空表示未设置; 生效阈值与覆盖情况以试算结论为准")。生效阈值与是否被覆盖只以后端试算响应的 `automatic.threshold.hint`/`overridden` 为准, 前端不自行推导这两个结论

### 策略字段契约与两侧校验

策略字段的类型, 覆盖范围, 取值范围, 热更新能力, 试算展示口径, 显式关闭值与运行时默认值集中在一张声明式契约表 `satrap/core/config/platform_policy.py` 的 `POLICY_FIELD_CONTRACT`。每个字段声明 `kind` (int/number/bool/enum/text/list/notice_types/words/group_ids/scope/group_map/time_rules), `scope` (`platform` 仅平台级 / `group` 平台与群覆盖 / `time` 平台, 时段与群覆盖), `hot_reload`, `display_in_preview`, `off_value`, `min`, `max`, `max_exclusive`, `integer`, `max_length`, `max_items`, `enum`, `default` 与 `nullable`。

以下集合都由这张表派生, 不再各自维护: 群覆盖键 `GROUP_KEYS` 与时段规则键 `AUTOMATIC_KEYS` (按 `scope`), 热更新键集合 (按 `hot_reload`), 运行时默认值 `POLICY_DEFAULTS` (按 `default`), 试算面板展示的字段 (按 `display_in_preview`), 以及前端编辑器字段与表单数值约束 (由生成的契约 JSON 构建)。

取值口径:

| 字段 | 口径 |
| --- | --- |
| `wake_cooldown` | 有限非负, 没有上限; 存量大于一天的配置仍可保存并启动 |
| `wake_max_wait` | `0 ≤ x < 120` (排他上界), `0` 表示关闭; 前端不以 119 或任意 epsilon 代替。HTML `max` 表达不了排他上界, 由校验器检查 |
| `wake_message_threshold` | 整数 1–32 |
| `wake_score_threshold` 与权重字段 | 0–1 |
| `wake_talk_value` | 0–1, 接受显式 null (未设置) |
| `message_text_limit` / `input_text_limit` / `input_media_limit` | 分别为 64–32000 / 1–200000 / 1–32 的整数 |
| 文本长度 (`asr_model` ≤ 128, `media_trusted_hosts` 每项 ≤ 253, `command_operators` 每项 ≤ 64) | 按 Unicode 码点计, 与后端 `len()` 同口径; 非 BMP 字符 (emoji, 扩展区汉字) 按 1 个码点计, 前端不以 UTF-16 码元判断 |

缺失与默认值的区别: 字段缺失表示继承或未设置, 校验与前端编辑都不会向配置注入默认值; `0`, `false` 与 `[]` 都是显式取值。显式 `null` 只在可空字段上合法 (如 `wake_talk_value` 与 `notice_types`)。

前端按同一张契约做即时校验: 保存与试算前, 平台设置先经 `normalizePlatformSettings` 归一化 (数字字符串转数字, 留空删键), 再逐字段校验; 群/时段覆盖的每个字段在行转换 `fromGroupRows`/`fromTimeRows` 时校验, 非法取值保留在草稿里并同时阻止保存与试算。文本与列表项长度前端按 Unicode 码点判断 (`textLength`), 与后端 `len()` 同口径, 含 emoji 等非 BMP 字符的合法名称不会被误拒。后端在保存与适配器构造时按同一张表做权威校验, 两侧结论一致但都保留 (前端不复制后端的归一化逻辑)。

分工例外: 群白名单, 上下文范围与覆盖结构 (群号/时段/条数上限) 仍由后端专用校验器负责, 前端不复制其归一化; 平台级未知扩展字段继续透传, 群/时段覆盖里的未知字段被拒绝。

契约 JSON 的生成与同步:

```bash
python scripts/sync_wake_policy_contract.py          # 重新生成 satrap-ui/src/generated/wake-policy-contract.json
python scripts/sync_wake_policy_contract.py --check  # 只校验生成物是否与契约表一致
```

生成物按 UTF-8, LF, 字段排序与末尾换行固定并已入库; `.gitattributes` 对该文件与共享样例固定 `eol=lf`, 避免换行差异污染逐字节比对。pytest 断言"契约表序列化结果与库内文件逐字节一致", 漂移即失败。前端构建只读取该 JSON, 不要求后端在线。共享样例 `tests/fixtures/wake_policy_cases.json` 按平台/群/时段上下文列出合法, 非法与边界取值, 由 pytest 跑后端真实校验入口, 由 vitest 跑前端严格校验与实际行转换入口。

## 平台命令入口

命令仍由会话层识别与执行, 但输入必须在进入唤醒窗口与输入投影之前冻结成纯命令正文: 平台渲染的 `@机器人` 形如 `@3588795965`, 按原样送去会让 `strip()` 后的首字符不是 `/`, 命令名与参数也会被窗口上下文追尾污染。调度器因此在唤醒评估之后立即调用 `satrap/core/pipeline/command_entry.py` 的 `extract_command_candidate(event)`, 命中时以冻结正文作为唯一输入, 并跳过引用回源, 转发补全, 附件转写, 媒体下载与输入投影 (这些产物会被冻结正文取代)。

候选判定只读顶层结构化组件, 不做字符串搜索:

- 从组件头部跳过寻址段: 指向机器人自身的 `At` (不含 `@全体成员`), 全空白文本, 以及位于组件列表最前的一个引用标记
- 群聊要求寻址段里至少有一个指向机器人自身的 `At`; 私聊不要求, 但寻址段里的提及同样被剥离
- 正文由剩余组件渲染 (`Plain` 取文本, `At` 取 `@昵称或 ID`, `AtAll` 取 `@全体成员`); 出现图片, 语音, 表情等非文本组件时按普通消息处理
- 正文 `strip()` 后以默认命令前缀 (`satrap/core/framework/command/base.py` 的 `DEFAULT_COMMAND_PREFIX`) 开头才算候选

剥离范围仅限正文之前。`@bot /plan x @bot` 的尾随提及不被剥离: 群聊下它不构成"前置于正文的显式 @bot", 因此不是命令; 私聊下它留在命令参数里。平台侧只用默认前缀与默认参数分隔符做语法级判定, 会话若用自定义 `cmd_prefix` 构造, 平台层不跟随, 该会话的命令不会被平台入口识别; 命令是否注册, 是否被 `disable_command` 停用仍由会话层唯一决定, 未知 `/foo` 照旧落到模型。由此未知命令会失去窗口上下文 (它的正文已被冻结为候选), 这是本轮接受的可见行为变化。

命令不读取也不写入唤醒窗口: 所有唤醒模式下命令都不发生 `observe`, 既不把自身存进窗口, 也不消费既有待处理文字或图片。命令也不引入唤醒旁路 —— 群内 `@bot` 本来就命中提及规则, 而由唤醒词, 引用机器人或必要性阈值唤醒的 `/xxx` 只是普通消息。群内不带 `@bot` 的裸 `/help` 不会执行也不会有提示 (提示等于绕开唤醒门主动外发), 可发现性由文档与 `/help` 自身承担。

### 高权限命令的操作员名单

`/approve` 与 `/plan` 修改的是同一个工具权限引擎的安全状态 (`/plan off` 会放宽写操作限制), 因此两者都要求发起者在平台设置 `command_operators` (kind `list`, 每项为平台用户 ID) 内。名单缺失, 为空或取值非法时一律拒绝, 不存在"空名单表示不限制"的语义 (与 `group_admin.allowed_callers` 相反)。判定只认这份名单, 不认平台管理员身份或 QQ 群管理员角色: 管理面板手动唤醒等管理面来源已过管理面认证, 不进入该判定。

拒绝发生在限流之后, 因此拒绝本身受同一限流约束; 拒绝时回复固定文案 `该命令仅允许已授权操作员执行。`, 不调用模型也不进入会话, 并记入诊断 (`projection` 阶段, 原因码 `operator_required`)。名集当前只含 `/approve` 与 `/plan`, 属过渡手段: 后续会迁移到命令注册 metadata 的权限级别, `/goal` 与 `/memory` 本轮不受该名单限制。


## 通知与请求事件

OneBot 的 notice/request 不进入消息管线, 由适配器归一为 `PlatformEvent` 并经 `emit_event` 交给后端的 `PlatformEventHub`。事件类型为 `notice.<notice_type>` 或 `request.<request_type>`, `extras["payload"]` 是类型化的 `NoticePayload` (category, kind, sub_type, self_id, group_id, user_id, operator_id, target_id, message_id, flag, comment, duration, time, 受限的 file 字段), `raw_event` 保留原始载荷。账号与已绑定 self_id 不一致的事件被丢弃并计入 ingress.account。

`settings.notice_types` 省略时派发全部类型, 否则只派发列出的类别 (`notice`/`request`) 或具体类型 (如 `notice.group_increase`), 最多 64 项。带 group_id 的通知在群白名单之外时静默丢弃; 好友请求等无群事件不受白名单影响。

处理中心用平台实例、账号、类别、类型及稳定载荷字段构造去重键 (容量 4096, TTL 120 秒), 每个事件只派发一次; 处理器在独立小任务中执行 (同时至多 64 个, 超出丢弃并计数), 不排在模型调用之后, 异常互相隔离。没有订阅者的事件只计数。健康响应的 `platform_events` 提供 received/duplicate/dropped/dispatched/failed/unsubscribed 计数。

群文件上传 (`notice.group_upload`) 额外归一为附件事件: `extras["attachment"]` 携带 `File` 组件 (name 为文件名, file 为远端文件 ID, url 为实现返回的下载地址, 可能为空), 文件大小与 busid 保留在 `payload.file`; 缺少文件 ID 和 URL 时不生成附件。归一只携带远端元信息, 不触发下载; 下载与模型处理仍须遵守目标会话的触发策略。

插件可在 `build_tools`/`build_handlers` 等工厂中调用 `satrap.core.platform.notices.current_hub()` 获取处理中心并 `subscribe(event_type, handler)`, 返回的注销函数应在插件 `cleanup` 中调用; 后端未运行时返回 None。默认不把任何入退群、撤回或请求转成模型调用, 也不自动审批请求; 审批与群管理动作由 `group_admin` 插件的工具按来源身份显式执行 (见"群管理动作与能力矩阵"), 请求审批所需的 `flag` 即来自这里的 request 事件载荷。

请求审批的身份由跨重启的审批账本 `.satrap/data/request_ledger.json` 决定 (见 [运行数据布局](../core/data-layout.md)):

- 入站 request 事件按平台实例、已绑定账号与类别 (`group`/`friend`) 隔离, 群申请沿用 flag 摘要; 好友申请额外使用平台原始事件时间区分可复用的处理 flag, 缺少时间时保守沿用旧身份规则
- 同一次申请的归属与首次接收时间固定, 重复入站不改写状态或期限 (默认 600 秒, 到期移入归档); 同一好友凭据的更晚事件生成新的 request_id, 旧可处理申请记为 `superseded` 并禁止再次执行
- 审批动作在文件锁内原子占用 `available → executing → completed/unknown`, 落盘成功后才发出网络动作; 动作超时、取消或传输异常记为 `unknown` 并保持不可重放, 同一次申请不会恢复可审批; 好友处理在协议发送前复核是否已出现更新申请, 终态回写仅作用于实际占用的记录
- 重启后无法确认的占用保守记为 `unknown`; 台账容量达限 (每"实例+账号"4096, 全表 16384) 拒绝新登记, 不淘汰旧身份
- 进程内的近期 flag 缓存仅用于诊断, 不作为审批资格或跳过登记的依据; 缓存淘汰不影响账本身份
- 审批动作的审计日志 (`[OneBotAdmin] 写动作已执行`) 只记 `adapter`, `self_id`, 群或成员 ID 与 flag 摘要 (与账本同域, 前 8 位), 不回显原始 flag, 备注, 拒绝理由或消息正文: 原始 flag 是可重放标识, 摘要可与账本条目对照而不泄露
- 账本损坏或降级时审批直接拒绝执行, 需显式恢复并校验通过后才继续, 不提供"清空账本后继续"
- 声明"处理了某个 flag"的调用必须与登记事件的账号和群一致, 归属不符按拒绝处理, 不落任何网络动作


## 入站图片与视频

已唤醒且通过限流的消息中, 最终会进入模型的图片与视频由 `pipeline/media_resolve.py` 与引用/转发补全、附件处理在同一阶段解析, 只处理按 `input_media_limit` 实际选中并会提交给模型的那部分媒体; 本轮只对 OneBot 适配器启用, Misskey 等其他适配器继续由模型层直连获取。

- 解析顺序: ① 直连上报的 `url`; ② 图片在直连失败时经实现动作 `get_image` 取回重新解析后的地址再下载一次, 请求键优先用入站 `image` 段保留下来的原始 `file` 标识, 该标识取不回地址时才退回上报的 `url`; ③ 两者都不可用时该媒体标记失败并降级。视频没有对应的实现动作, 只做直连, 不假设实现提供视频回源
- 解析成功的媒体落地为临时文件 (登记到事件, 事件结束自动删除), 路径写入组件的 `resolved_path`; 该字段只由解析阶段写入, 不参与组件序列化, 模型输入与出站发送都优先使用它。上报的 `file` 与 `url` 一律保留原值, 不再互相覆盖, 因此出站转发时不会把不可发送的原始标识当作来源
- 预算: 单条媒体下载 20 秒超时, 每事件 60 秒总预算, 单条不超过 32 MiB; 超出预算的媒体直接降级, 不为最终会被裁掉的媒体做无用下载
- `get_image` 只能取回实现仍在其缓存中的图片: 图片已被实现淘汰时该动作失败, 此时无法恢复。这项限制无法由 Satrap 侧绕过; SnowLuma 的图片缓存对 `file`, `fileName` 与 `url` 建别名, 但按 `url` 别名查找属实现特定行为, 因此只作为原始标识失效后的兜底
- 失败占位: 解析失败的图片把正文中的 `[图片]` 覆盖为 `[图片读取失败]` (视频为 `[视频读取失败]`), 占位不并存; 混合正文继续正常调用模型, 模型因此不会误以为自己已拿到该媒体。若消息除失败媒体外没有任何可读正文, 不调用模型, 直接回复 `图片读取失败，暂时无法处理该图片。`
- 失败原因码进入补全阶段诊断, 形如 `image:failed:image_url_refreshed`; 平台页与手动唤醒弹窗按图片/视频给出中文标签
- 未唤醒窗口中的图片在本轮认领后通过同一解析流程处理, 包括原始 `file` 刷新, 失败降级和临时文件清理; 窗口过期或平台缓存已淘汰的图片无法恢复

## 语音转写与文件正文

已唤醒且通过限流的消息中, 顶层 `Record` 与 `File` 组件由 `pipeline/attachments.py` 在引用/转发补全之后处理, 每事件至多 4 个附件, 其余标记为“超出附件处理数量”。未唤醒的普通消息不会下载任何附件。

- 语音: `settings.voice_transcribe` 选择转写来源, 默认 `asr`; `settings.asr_model` 指向一个已保存的 ASR 模型配置, 后端按名称解析并用 `AsyncASR` 转写 (16 MiB / 60 秒超时)。`asr` 路径按三级获取 ASR 可接受的音频: ① 请求实现服务端转码 (`get_record out_format=wav`, SnowLuma/NapCat 支持, 覆盖 QQ 原生 SILK 语音); ② 实现不提供时直接下载并按魔数探测, wav/ogg/flac/mp3/webm/m4a 原样送 ASR; ③ amr 等 ffmpeg 可解码格式在线程池中经 PyAV 本地转 16 kHz 单声道 wav (需 `pip install -e .[audio]`, 最长 300 秒), 面向不提供 `get_record` 的实现。SILK 裸流无法本地转码, 标记 `unsupported/silk_needs_platform_transcode`; 缺 av 包标记 `av_missing`。`platform` 只调用实现的原生转写 `fetch_ptt_text` (不需要 `asr_model`); `asr_then_platform` 在 ASR 路径失败后回退到它; `off` 关闭。转写结果冻结到 `Record.text`, 同一事件不重复调用; 投影为 `[语音 转写内容: …]` (至多 4000 字符), 不支持时给出具体原因文案
- ASR 命名配置的引用保护: 删除或重命名一个 ASR 配置前, 扫描平台 `asr_model` 绑定、插件全局配置的 asr 字段与各平台库中的会话覆盖; 有引用时拒绝 (控制端 409 `config_in_use` 并列出引用清单)。扫描必须完整才允许放行: 已存在的插件元数据或全局配置读不出/解析失败、平台库锁定或查询失败、覆盖 JSON 非法、平台库已声明覆盖表版本但缺表时, 一律按"无法确认无引用"处理, 返回 503 `asr_reference_scan_failed` 并带脱敏原因 (`override_db`/`override_json`/`override_schema`/`plugin_meta`/`plugin_config`), CLI 返回非零状态; 只有明确不存在且在契约上允许不存在的来源 (没有插件声明 asr 字段、旧版库 `PRAGMA user_version` 早于覆盖表版本) 才当作空集合。扫描与配置写入共用一把锁, 扫描通过到实际删除之间不会有新增引用被漏检
- 文件: `settings.attachment_extract` 默认开启, 只处理 `documents.SUPPORTED_EXTENSIONS` 内的类型; 经受限下载写入临时文件 (登记到事件, 事件结束自动删除), 在线程池中调用 `extract_text` (32 MiB / 20000 字符上限), 投影为 `[文件 <名> 内容:
…]`。群文件上传 notice 生成的 File 附件同样只在其被明确转为会话消息时才会下载
- 下载使用出站防护 `safe_async_get`: 只接受 http/https, 私网地址默认拒绝, 需要访问 SnowLuma 内网下载地址时在 `settings.media_trusted_hosts` 登记主机名; 默认校验 TLS 证书且重定向限制在同源, 自签证书的内网 https 需显式开启 `settings.media_insecure_tls: true` (仅对 `media_trusted_hosts` 登记的主机生效, 公网下载始终校验证书); 公网地址默认要求 https, 明文 http 公网下载需显式开启 `settings.media_plaintext_http: true` (平台设置界面有对应开关, 明文传输可被窃听篡改, `media_trusted_hosts` 登记主机不受此限); 不把平台上报的本地路径当作 Satrap 主机上的可信文件
- 失败降级: 未配置 ASR、格式不支持、下载/转写/提取失败均保留可识别标记 (`[语音: 未启用转写]`、`[文件 x: 不支持的格式]`、`[…: 获取或处理失败]`) 并继续处理当前问题, 不把未知二进制送入文本模型; `input_projection` extra 的 `attachment_status` 为 resolved/partial/failed, notes 记录各项原因
- 已知限制 (兼容性债务): 入站 `record` 段的 `file` 与 `url` 现在都保留原值, 但 `_resolve_record` 仍可能把 `url` 交给 `get_record`。SnowLuma/NapCat 的媒体缓存支持 URL 别名, 因此当前可用; 这不等于语音的原始标识问题已彻底解决

上述内容作为用户提供的资料进入模型输入, 不提升为系统指令, 不参与唤醒判定或命令解析。

## 请求诊断

按请求关联的诊断在阶段位置就地采集, 进程内按平台实例有界保存 (每实例最近 256 个请求, 每请求最多 16 条): 阶段为 `wake_decision`/`rate_limit`/`projection`/`model`/`send`, 每条含状态、脱敏原因码、展示原因、附件失败类型、截断说明与关联 `turn_id`。诊断只保存原因码与计数, 不保存正文、音频、密钥或供应商原始响应; 普通事件与手动唤醒共用同一套阶段记录, 手动请求的幂等与发送证据仍以持久账本为准, 诊断环形淘汰不影响它。

- 未唤醒/被限流的消息在投影之前就被拒绝, 其阶段记录同样可查 (`wake_decision`/`rate_limit`)
- 补全阶段的附件失败与最终发送结果分开成条: 附件失败后模型仍可执行, 不把阶段失败压成整条请求失败
- 发送阶段只读发送证据: 有已确认前缀且其余未尝试为 `partial`, 有未确认段为 `unknown`, 全段确认为 `sent`, 没有业务尝试也没有业务回执为 `skipped`
- 未被唤醒的事件不产生发送阶段记录, 决策阶段的记录即为完整结论
- 平台命令入口在 `projection` 阶段记录冻结命中 (`command_candidate`); 命令未调用模型, 因此不为它写 `model` 阶段记录, 也不出现"模型输出 N 字符"; 非操作员执行受保护命令记 `operator_required` (同样落在 `projection` 阶段, 故"仅看拒绝"预设不会列出它, 在近期请求里按该原因码查看)

查询接口 (沿用既有管理认证通路):

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/platforms/wake/diagnostics?adapter_id=&stage=&request_id=&limit=` | 按请求汇总的近期诊断, 最新在前; `stage` 支持逗号分隔多值 (命中任一阶段即保留该请求, 命中后仍返回该请求全部阶段); `limit` 上限 256, 非法值 400 `invalid_limit`, 空项或未知阶段 400 `invalid_stage` |
| GET | `/api/platforms/wake/diagnostics/{request_id}[?adapter_id=]` | 单请求的阶段明细; 未采集到 404 `not_found` |
| GET | `/api/platforms/wake/rejections?adapter_id=&limit=` | 遗留的记录级拒绝查询, 只返回 `wake_decision`/`rate_limit` 两个拒绝阶段; 新界面统一走 `stage=wake_decision,rate_limit` 的请求级诊断, 本接口仅为兼容保留 |

调度器未装配时列表返回 `available: false`/`reason: scheduler_unavailable`, 明细返回 503; 诊断入口全部为内存操作, 采集异常只记日志, 不影响消息处理。

## 管理界面: 阶段诊断与草稿行保护

平台页与手动唤醒弹窗共用上节的诊断接口: 阶段名与状态以中文展示, 附件与引用/转发失败码逐项翻译 (语音/文件/引用/转发 + 状态 + 原因码), 发送结果按"已确认/未确认/失败"分段列出, 不把 `unknown` 显示成已送达或确定失败。发送结果为 `unknown` 时界面明确标注"不确定, 不自动重发"并由用户按阶段记录决定后续动作; 手动请求在受理与执行中显示进行中, 已确认完成停止跟踪, `accepted`/`executing` 期间按有界间隔轮询。

- 只含 `wake_decision`/`rate_limit` 的请求不会被标成进行中
- 平台筛选会作为 `adapter_id` 查询参数发给服务端 (不是仅在本地过滤), 可选项来自运行中适配器与已配置平台
- 面板提供"仅看拒绝"预设 (即 `stage=wake_decision,rate_limit`), 用于浏览近期被拒绝的请求; 平台页默认关闭, 手动唤醒弹窗在未跟踪请求时默认开启
- 跟踪某个具体请求时拒绝筛选被自动取消并禁用, 正常请求不会因残留过滤而显示为空; 弹窗提供"返回近期请求"清除聚焦后重新筛选, 切换平台也会重置该预设
- 手动唤醒弹窗只有这一处诊断面板 (不再单独请求遗留的记录级拒绝接口), 状态跟踪与阶段诊断指向同一 `request_id`
- 自动刷新有界: 每 4 秒一轮, 最多 45 轮; 没有进行中的请求或页面隐藏时停止, 之后只能手动刷新
- 空列表、调度器不可用、查询失败各有独立状态, 查询失败时提供"实际重试"按钮; 刷新页面后可从来列表重新定位普通与手动请求
- 近期诊断按容量淘汰, 详情被淘汰时说明"已被容量淘汰", 持久账本中的手动请求状态与发送证据仍可查询

平台编辑表单中的群级覆盖与时段规则以草稿行形式由表单持有: 每行有稳定的本地行标识, 群号为空/非正整数/重复、起止时间非法或相同都保留该行并给出行内错误, 有错误时保存与试算同时被阻止 (服务端仍会再校验)。行内其余显式值不因同一行的校验失败被清掉, 恢复继承与删除行只由显式操作触发; 完整且全部继承的行在保存时规范化为不写覆盖, 但不会在输入过程中消失。草稿只在打开平台、保存成功时从持久化配置重建, 不写入浏览器存储。

未保存的修改在弹窗关闭、站内导航与浏览器前进/后退三处统一确认: 拒绝离开时停留在原页面且草稿完整, 确认放弃才执行一次导航; 保存成功后不再拦截。浏览器关闭/刷新仍走 `beforeunload` 原生确认。
