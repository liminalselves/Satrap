# 平台接入

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

OneBot 发送方法返回 `SendReceipt`: `success` 表示收到平台消息 ID, `partial` 表示部分块成功后明确失败, `failed` 表示明确未完成, `unknown` 表示动作结果无法确认。回执保留已确认的 `message_ids`, 分块失败位置和固定错误原因, 不回显供应商响应正文。事件的 `last_send_receipt` 保存最近一次回执; 部分成功或未知结果阻止调度器兜底重发全文。其他平台旧式 `None` 返回继续按原契约处理, 不伪造平台确认。本契约覆盖普通发送、流式降级与按长度拆分后的多块发送。

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

能力矩阵 (全部为 OneBot v11 标准动作):

| OneBot 动作 | 工具名 | 读写 | 主要参数 | 响应收窄 |
| --- | --- | --- | --- | --- |
| get_group_list | group_admin_list_groups | 读 | 无 | 至多 512 条, 群号/群名/人数 |
| get_group_info | group_admin_get_group_info | 读 | group_id 可选 | 群号/群名/人数/创建时间/等级 |
| get_group_member_list | group_admin_list_members | 读 | group_id 可选 | 至多 2048 条, QQ/昵称/名片/角色/入群时间等 |
| get_group_member_info | group_admin_get_member | 读 | user_id, group_id 可选 | QQ/昵称/名片/角色/禁言时间等 |
| get_group_honor_info | group_admin_get_honors | 读 | group_id 可选, honor_type | 实现返回的荣誉数据 |
| delete_msg | group_admin_recall_message | 写 | group_id (默认当前群, 受实例白名单与插件 allowed_groups 限制), message_id | 无返回 |
| set_group_kick | group_admin_kick | 写 | user_id, reject_add_request, group_id 可选 | 无返回 |
| set_group_ban | group_admin_ban | 写 | user_id, duration 0-2592000 秒, group_id 可选 | 无返回 |
| set_group_whole_ban | group_admin_whole_ban | 写 | enable, group_id 可选 | 无返回 |
| set_group_anonymous_ban | group_admin_ban_anonymous | 写 | flag, duration, group_id 可选 | 无返回 |
| set_group_admin | group_admin_set_admin | 写 | user_id, enable, group_id 可选 | 无返回 |
| set_group_anonymous | group_admin_set_anonymous | 写 | enable, group_id 可选 | 无返回 |
| set_group_card | group_admin_set_card | 写 | user_id, card ≤60 字符, group_id 可选 | 无返回 |
| set_group_name | group_admin_set_name | 写 | name 1-60 字符, group_id 可选 | 无返回 |
| set_group_special_title | group_admin_set_title | 写 | user_id, title ≤18 字符, group_id 可选 | 无返回 |
| set_group_leave | group_admin_leave | 写 | group_id 可选, dismiss | 无返回 |
| set_friend_add_request | group_admin_handle_friend_request | 写 | flag, approve, remark ≤60 字符 | 无返回 |
| set_group_add_request | group_admin_handle_group_request | 写 | group_id (默认当前群, 受群范围限制), flag, sub_type add/invite, approve, reason ≤120 字符 | 无返回 |

好友/加群请求的 `flag` 来自通知事件 (见文末"通知与请求事件"), 工具只做显式审批, 不做任何自动同意或拒绝。布尔参数严格校验, 拒绝真值语义; 写操作被平台拒绝或结果未知时按 `manual` 策略交由用户确认, 不自动重放。

### 长消息拆分与发送顺序

`settings.message_text_limit` (默认 2000, 允许 64-32000 的整数) 限制每条消息的文本字符数。超长回复在适配器实际发送前拆分: 优先在段落 (`

`) 边界断开, 其次在换行处断开, 单段仍超限时按上限硬切; 图片、@ 等非文本组件不可切开并保持原顺序。拆分不修改原消息链, 拼接后的文本与原文一致。

拆分后的各块按顺序发送, 首个非 `success` 的块之后停止, 回执聚合为 `partial` (已有确认 ID 且明确失败) 或 `unknown` (结果不明), 并记录 `failed_index`; 不会重发已确认块。

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
| `access_token` | OneBot access token |
| `enable_private` | 是否处理私聊 |
| `enable_group` | 是否处理群聊 |

## 多平台路由

后端会根据平台实例, `session_type` 和用户来源构建会话 ID。不同平台实例上的同一用户会进入不同上下文, 避免消息串线。平台管理页可直接从已注册且启用的会话类中选择 `session_type`。

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

`platform wake` 向运行中的后端提交 OneBot 群手动唤醒, 与控制面板会话页的手动唤醒弹窗共用 `POST /api/platforms/wake` 契约: `--prompt` 与 `--message-id` 互斥, 均省略时处理该群与成员范围内的待处理正文; `--request-id` 省略时自动生成, 重复提交同一 ID 只入队一次。操作者身份固定为服务端已认证的管理主体, 不从命令行参数读取。返回 `accepted`/`already_pending`/`no_pending`, 被拒绝时以非零退出并给出原因。

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

OneBot 就绪探针核验当前实例的独立本地 HTTP 标识, 不将端口被其他服务占用当成启动成功。该检查仅验证 Satrap 监听服务, 不表示 SnowLuma 或 QQ 已连通。
# 自动参与的群与时段覆盖

OneBot 默认使用 `wake_mode: explicit`, 只处理明确唤醒。可选 `frequency` 按正文数量触发, 或 `necessity` 按本地必要性评分触发。`wake_message_threshold` 默认 3, `wake_cooldown` 默认 30 秒, `wake_score_threshold` 默认 0.65。单独附件和 @全体不计数。

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


## 通知与请求事件

OneBot 的 notice/request 不进入消息管线, 由适配器归一为 `PlatformEvent` 并经 `emit_event` 交给后端的 `PlatformEventHub`。事件类型为 `notice.<notice_type>` 或 `request.<request_type>`, `extras["payload"]` 是类型化的 `NoticePayload` (category, kind, sub_type, self_id, group_id, user_id, operator_id, target_id, message_id, flag, comment, duration, time, 受限的 file 字段), `raw_event` 保留原始载荷。账号与已绑定 self_id 不一致的事件被丢弃并计入 ingress.account。

`settings.notice_types` 省略时派发全部类型, 否则只派发列出的类别 (`notice`/`request`) 或具体类型 (如 `notice.group_increase`), 最多 64 项。带 group_id 的通知在群白名单之外时静默丢弃; 好友请求等无群事件不受白名单影响。

处理中心用平台实例、账号、类别、类型及稳定载荷字段构造去重键 (容量 4096, TTL 120 秒), 每个事件只派发一次; 处理器在独立小任务中执行 (同时至多 64 个, 超出丢弃并计数), 不排在模型调用之后, 异常互相隔离。没有订阅者的事件只计数。健康响应的 `platform_events` 提供 received/duplicate/dropped/dispatched/failed/unsubscribed 计数。

群文件上传 (`notice.group_upload`) 额外归一为附件事件: `extras["attachment"]` 携带 `File` 组件 (name 为文件名, file 为远端文件 ID, url 为实现返回的下载地址, 可能为空), 文件大小与 busid 保留在 `payload.file`; 缺少文件 ID 和 URL 时不生成附件。归一只携带远端元信息, 不触发下载; 下载与模型处理仍须遵守目标会话的触发策略。

插件可在 `build_tools`/`build_handlers` 等工厂中调用 `satrap.core.platform.notices.current_hub()` 获取处理中心并 `subscribe(event_type, handler)`, 返回的注销函数应在插件 `cleanup` 中调用; 后端未运行时返回 None。默认不把任何入退群、撤回或请求转成模型调用, 也不自动审批请求; 审批与群管理动作由 `group_admin` 插件的工具按来源身份显式执行 (见"群管理动作与能力矩阵"), 请求审批所需的 `flag` 即来自这里的 request 事件载荷。


## 语音转写与文件正文

已唤醒且通过限流的消息中, 顶层 `Record` 与 `File` 组件由 `pipeline/attachments.py` 在引用/转发补全之后处理, 每事件至多 4 个附件, 其余标记为“超出附件处理数量”。未唤醒的普通消息不会下载任何附件。

- 语音: `settings.voice_transcribe` 选择转写来源, 默认 `asr`; `settings.asr_model` 指向一个已保存的 ASR 模型配置, 后端按名称解析并用 `AsyncASR` 转写 (16 MiB / 60 秒超时)。`asr` 路径按三级获取 ASR 可接受的音频: ① 请求实现服务端转码 (`get_record out_format=wav`, SnowLuma/NapCat 支持, 覆盖 QQ 原生 SILK 语音); ② 实现不提供时直接下载并按魔数探测, wav/ogg/flac/mp3/webm/m4a 原样送 ASR; ③ amr 等 ffmpeg 可解码格式在线程池中经 PyAV 本地转 16 kHz 单声道 wav (需 `pip install -e .[audio]`, 最长 300 秒), 面向不提供 `get_record` 的实现。SILK 裸流无法本地转码, 标记 `unsupported/silk_needs_platform_transcode`; 缺 av 包标记 `av_missing`。`platform` 只调用实现的原生转写 `fetch_ptt_text` (不需要 `asr_model`); `asr_then_platform` 在 ASR 路径失败后回退到它; `off` 关闭。转写结果冻结到 `Record.text`, 同一事件不重复调用; 投影为 `[语音 转写内容: …]` (至多 4000 字符), 不支持时给出具体原因文案
- 文件: `settings.attachment_extract` 默认开启, 只处理 `documents.SUPPORTED_EXTENSIONS` 内的类型; 经受限下载写入临时文件 (登记到事件, 事件结束自动删除), 在线程池中调用 `extract_text` (32 MiB / 20000 字符上限), 投影为 `[文件 <名> 内容:
…]`。群文件上传 notice 生成的 File 附件同样只在其被明确转为会话消息时才会下载
- 下载使用出站防护 `safe_async_get`: 只接受 http/https, 私网地址默认拒绝, 需要访问 SnowLuma 内网下载地址时在 `settings.media_trusted_hosts` 登记主机名; 默认校验 TLS 证书且重定向限制在同源, 自签证书的内网 https 需显式开启 `settings.media_insecure_tls: true`; 不把平台上报的本地路径当作 Satrap 主机上的可信文件
- 失败降级: 未配置 ASR、格式不支持、下载/转写/提取失败均保留可识别标记 (`[语音: 未启用转写]`、`[文件 x: 不支持的格式]`、`[…: 获取或处理失败]`) 并继续处理当前问题, 不把未知二进制送入文本模型; `input_projection` extra 的 `attachment_status` 为 resolved/partial/failed, notes 记录各项原因

上述内容作为用户提供的资料进入模型输入, 不提升为系统指令, 不参与唤醒判定或命令解析。
