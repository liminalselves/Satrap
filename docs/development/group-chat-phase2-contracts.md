# group_chat 第二阶段: 群摘要, 图片与表情, 群记忆, 提醒

状态: A1/A2 群摘要与 B1/B2 图片和表情已实现; C1/C2 记忆插件拆分与群记忆已实现, D1/D2 未完成; 回归与冒烟在全部批次结束后统一执行

实施顺序: 1. 群摘要 → 2. 图片与表情 → 3. 群记忆与成员偏好 → 4. 一次性提醒

## 0. 现状与共同契约

### 0.1 可复用部分与需要新增的部分

- 已有 `MessageScope(adapter_id, self_id, conversation_kind, chat_id)`, 原始消息档案, 管理修订, 删除/恢复与保留期
- 已有六个群聊工具, 主 Agent 回复作用域, `text/quote/mention` 组件与实际发送回执
- 已有跨平台 `Image/Face` 组件, 受控媒体下载, 临时文件清理与轻量文件 token 服务
- 文件 token 仅映射本地路径, 不能直接充当具有群归属, 生命周期和权限的图片资产 ID; 需要新增资产登记层
- `PipelineScheduler` 是入站消息处理管线; `WakeTimers` 是唤醒等待计时器, 二者都不是可重启恢复的持久提醒调度器
- 不新增专用摘要模型配置; 第一版由当前群的主 Agent 生成摘要

### 0.2 插件与平台边界

摘要, 媒体和提醒继续扩展官方 `group_chat` 插件; 长期记忆从 `base_take` 独立为 `memory` 插件
三个插件互不依赖, 记忆由平台无关的宿主服务统一管理; 不将记忆或提醒混入 `group_admin` 动作

模型工具不接受 `adapter_id/self_id/chat_id/agent_config` 参数, 这些字段始终从可信当前群来源解析
所有消息, 成员, 资产和记录 ID 都是字符串, 不假定数字 ID 或 QQ 格式
核心与插件不得按平台名称分支; 平台负责声明能力并实现统一适配接口

能力分成两层:

| 层 | 能力 | 来源 |
| --- | --- | --- |
| 宿主 | `summary_read`, `summary_write`, `memory_read`, `memory_write`, `reminder_read`, `reminder_write` | 存储, 配置和来源权限 |
| 适配器 | `image`, `sticker`, `scheduled_text_send` | 平台实际实现与连接状态 |

继续采用 `supported/unsupported/unavailable` 与稳定原因码, 支持插件功能不等于平台能发送对应组件
不支持的图片/表情选项不向模型声明为可发送; 连接临时不可用时保留能力说明并返回原因
调度提醒的能力独立于即时回复能力, 适配器必须明确实现后台目标发送契约

只读工具沿用已有有效来源校验; 所有新建/修改/删除和回复准备只允许当前主工作流
子 Agent 可以在既有只读范围内提供分析, 不能提交持久写入, 登记跨范围媒体或创建提醒

### 0.3 时间

- 模型填写 `2026-10-04T09:00:00` 即可, 宿主自动补后端本地时区; 显式时区保留
- 入库使用 UTC 时间戳, 返回同时带 UTC 与已解析的显示时间, 前端按显示时区展示
- 摘要查询沿用消息搜索的含首尾边界, 即 `start_time <= message_time <= end_time`
- 摘要必须填写两个时间边界, 不因缺省而总结全部历史
- 相对提醒使用后端接受请求时的 `accepted_at`, 不使用可能偏快/偏慢的平台消息时间
- 提醒创建时冻结实际 UTC 执行时间, 重启或系统时区改变不移动已有任务
- 夏令时重复/不存在的本地时间不得静默猜测; 无法唯一解析时返回 `ambiguous_local_time` 或 `invalid_local_time`, 给出候选供确认
- 自动补时区不校准机器时钟, 前端保留现有平台时间/接收时间来源区别

### 0.4 通用结果与写入语义

沿用 `ok` 成功标识; 每个新对象携带 `schema_version: 1`, 不改写现有六个工具的返回形状

```json
{
  "ok": false,
  "error": {
    "code": "revision_conflict",
    "message": "记录已被修改, 请重新读取后再操作",
    "retryable": false
  }
}
```

| 状态 | 含义 |
| --- | --- |
| `saved` / `created` / `updated` / `cancelled` | 数据库事务已提交, 不是平台消息已送达 |
| `pending` | 提案已保存, 等待审批, 尚未成为有效记忆 |
| `prepared` | 沿用回复工具语义, 仅本轮草稿就绪 |
| `sent` | 提醒全部分段取得平台确认 |
| `partial` / `unknown` | 提醒部分成功或无法确认, 不自动重发 |

持久工具在自身数据库事务提交后返回, 不延迟到主模型轮次结束
后续模型失败不回滚已确认写入, 失败诊断包含已提交操作 ID; 模型不应重复创建来补救输出失败
实际发送仍使用既有回复作用域或后台发送回执, 与数据库写入分别记录

宿主从可信请求 ID 与工具调用 ID 生成内部操作 ID, 工具恢复重放复用同一 ID
同一来源消息, 同一操作类型与同一规范化参数的重复创建复用结果; 编辑/重新创建不同意图使用新操作
管理 API 由前端生成一次意图的 `idempotency_key`, 超时重试沿用该键; 同键不同参数返回 `idempotency_conflict`
数据库约束与事务负责去重, 不用进程内字典作为唯一依据
工具恢复策略默认禁止盲目重放写入; 已有操作 ID 时先查操作结果, 来源结束后只能查询, 不能新发写入
更新/删除必须提供 `expected_revision`; 冲突不覆盖, 不静默刷新重试

公共错误码增加 `invalid_argument`, `unsupported`, `unavailable`, `stale_call`, `permission_denied`,
`not_found`, `revision_conflict`, `idempotency_conflict`, `quota_exceeded`, `source_unavailable`,
`snapshot_expired`, `sources_not_read`, `asset_expired`, `asset_unavailable`, `time_in_past`
具体功能可补充错误码, 但禁止将超时/失败包装为零条消息, 空记忆或发送成功
`retryable` 只说明能否安全再查询或在尚未提交时重试, 不授权重新发送 `unknown` 的消息

预期失败记录带操作和范围标识的 warning; 非预期异常记录堆栈
入口捕获异常并归一结果, 一个群/任务失败不得中断进程或其它任务; 正常关闭时传播取消并完成有限收尾
日志不输出原始凭据, 图片数据或完整聊天正文

## 1. 群摘要

### 1.1 使用流程

用户要求总结某段讨论 → 当前主 Agent 申请消息快照 → 读取完整快照分页 → 主 Agent 生成带来源的条目 → 保存摘要 → 用现有 `group_chat_reply` 发送

第一版总结文本讨论, 图片/文件只标明存在附件, 不凭组件类型推断附件内容
创建摘要不自动向群发送; 保存和发送分别有结果
不创建持久子 Agent 上下文, 不暗中再调用另一套模型

### 1.2 模型工具

| 工具 | 参数 | 返回/作用 |
| --- | --- | --- |
| `group_chat_prepare_summary` | `start_time`, `end_time`; 可选 `keyword`, `include_bot=false` | 冻结有界消息快照, 返回第一批来源及 `snapshot_id`, 覆盖信息和游标 |
| `group_chat_read_summary_sources` | `snapshot_id`, `cursor` | 继续读同一个快照, 不重新混入新消息 |
| `group_chat_save_summary` | `snapshot_id`, `title`, `points` | 核验引用后保存结构化摘要, 返回 `summary_id`, `revision`, `status=saved` |
| `group_chat_get_summary` | `summary_id` | 读摘要, 来源状态与覆盖范围 |
| `group_chat_list_summaries` | 可选 `keyword`, `limit=20`, `cursor` | 当前群已保存摘要的有界列表 |

建议工具描述:

- prepare: “读取当前群指定时段的讨论, 供你生成摘要; 结果会注明保存范围和省略情况, 不能把不完整记录说成全部讨论”
- save: “保存你根据本次消息快照写出的摘要; 每一条结论都要列出来源消息 ID, 保存后可再回复到群里”

`points` 契约:

```json
{
  "snapshot_id": "ss_example",
  "title": "今天的项目讨论",
  "points": [
    {
      "text": "成员提出先完成接口联调, 发布日期尚未确认",
      "source_message_ids": ["m101", "m108"]
    }
  ]
}
```

- `title`: 1-120 字符; `points`: 1-20 条; 每条正文 1-2000 字符, 总正文不超过 12000 字符
- 每条引用 1-10 个消息 ID, 必须来自已读的快照; 宿主统一去重并保存来源清单
- 写入前重新检查当前群范围, 来源内容摘要和删除/撤回/过期状态, 并在同一事务内提交
- 引用核验只能证明出处存在和归属正确, 不能证明模型概括必然准确; skill 要求区分提议, 已决定事项和分歧
- 未读取全部快照分页返回 `sources_not_read`; 快照明确截断时允许保存, 返回与展示必须写“部分记录摘要”

### 1.3 覆盖与快照契约

```json
{
  "schema_version": 1,
  "snapshot_id": "ss_example",
  "resolved_range": {
    "start_time": "2026-10-04T09:00:00+08:00",
    "end_time": "2026-10-04T18:00:00+08:00"
  },
  "selection": {
    "selected_count": 240,
    "all_local_matches_selected": true,
    "truncated": false,
    "reasons": []
  },
  "archive_coverage": {
    "platform_history_complete": false,
    "archived_from": 1791031818,
    "archived_to": 1791039334,
    "retention_days": 30
  },
  "items": [],
  "next_cursor": "opaque_cursor",
  "expires_at": "2026-10-04T18:20:00+08:00"
}
```

- 既有档案 `coverage.complete=false` 保留, 不把最早/最晚时间间隔当成采集连续完整的证明
- `all_local_matches_selected` 只表示匹配的本地记录选完, 不表示平台历史完整
- 默认排除机器人自己发出的消息, 避免把上一版摘要当成群成员结论
- 默认最多选 500 条/60000 正文字符; 超限按时间正序取有界前缀, 返回实际截止位置, 其余不得冒充已总结
- 单页沿用消息条数与正文预算; 不丢弃剩余正文, 截断长消息时标记该消息不完整并列入 reasons
- 快照有效 15 分钟, 绑定群范围, 请求与主工作流; 新消息不改变快照, 删除/撤回会使关联快照失效
- 每群最多 5 个/宿主最多 20 个活动快照, 超过返回 quota_exceeded; 过期内容立即停止读取并由维护任务回收
- 在独立短事务中冻结消息内容和内容摘要, 不在模型生成期间持有 SQLite 读取锁
- 输入预算还受当前 Provider 可用上下文限制, 为系统提示词和输出预留空间; 无法确定容量时采用保守预算
- 过多结果建议缩短时间段后分段总结, 不默认抽样, 不自动产生无限模型调用
- 0 条结果明确返回 `selected_count=0`, 不保存“无人讨论”的推断摘要

### 1.4 保存, UI 与删除语义

摘要按群范围保存, 与 Agent 配置无关; 切换群 Agent 后仍可查询
在「对话记录 → 平台对话 → 群摘要」提供列表, 范围, 条目, 来源查看和删除
摘要由模型生成, 第一版不提供修改原摘要结论的入口; 用户可删除后重新生成

默认保留 30 天, 且不晚于被引用正文可访问的期限
来源删除/撤回/过期后整个摘要标为 `source_unavailable`, 清除派生正文与来源快照, UI 只保留必要状态与时间信息
恢复原始档案不自动恢复或发布旧摘要, 可重新生成
删除派生摘要不修改原始档案或模型上下文, 不撤回已经发到平台的消息

## 2. 图片与表情发送

### 2.1 组件契约

扩展同一个 `group_chat_reply.components`, 保留已有字段和单次准备/提交机制

```json
{
  "components": [
    {"type": "quote", "message_id": "m101"},
    {"type": "text", "text": "这是刚才那张图片"},
    {"type": "image", "asset_id": "ga_example"},
    {"type": "sticker", "sticker_id": "st_example"}
  ]
}
```

- `image` 只接受 `asset_id`; `sticker` 只接受表情库返回的 `sticker_id`
- 不接受模型填写服务器路径, Base64, 原生表情数字编号或任意下载 URL
- 一条回复最多 4 张图片和 4 个表情, 总组件数仍不超过 64
- 引用仍最多一个且移到首位, 其它组件顺序不变; 适配器拆分必须保持顺序与回执归属, 平台附件布局可能将图片置于正文之后
- 准备时核验所有组件; 任一媒体不可用时整个准备失败, 尚未发送的文字也不发送
- 模型可根据失败原因重新准备纯文本, 不由宿主静默丢图
- 提交前再次核验并持有文件租约至发送收尾; 已开始发送后的失败返回实际 partial/unknown, 不自动补发整条
- `prepared` 仍不表示送达, 原有六个工具的范围与回复去重保持

### 2.2 资产来源与工具

| 入口 | 契约 |
| --- | --- |
| `group_chat_get_message_assets(message_id)` | 从当前群已核验且可访问的消息取得媒体目录; 返回类型, 顺序, `asset_id`, 可用状态和期限 |
| `group_chat_list_stickers(keyword?, limit=20, cursor?)` | 返回当前群配置可用且适配器支持的表情 ID, 名称, 标签和预览摘要 |
| 宿主内部 `register_group_chat_asset` | 插件/工具将真实图片字节或受控产物登记, 成功后在工具结果中返回统一资产对象 |

图片资产对象:

```json
{
  "schema_version": 1,
  "asset_id": "ga_example",
  "kind": "image",
  "origin": "message",
  "source_message_id": "m101",
  "mime_type": "image/png",
  "width": 800,
  "height": 600,
  "available": true,
  "expires_at": 1791162000
}
```

- 消息资产绑定完整群范围和来源消息, 不凭裸 URL 去重后跨群授权
- 工具产物绑定群范围, 产物工具和有效请求; 默认只允许当前轮发送, 后续轮要由可信工具重新登记/授权
- 登记前把需要跨异步发送的临时文件复制到受管理缓存, 不能依赖事件结束就被删除的入站临时文件
- 受控文件登记检查实际文件类型, 字节数与像素数, 不能只相信扩展名或 MIME
- 默认单图 10 MiB, 解码像素 2000 万, 每回复媒体合计 20 MiB; 平台限制更小时取更小值
- 支持格式取宿主可解码与适配器可发送的交集, 第一版覆盖 PNG/JPEG/WebP/GIF; GIF 不保证所有平台保留动画
- Misskey 房间只支持单个 fileId, 图片和图片表情合计至多一个; 不支持的引用/@/原生表情不提供给模型
- 下载复用既有出站 URL 校验与大小/超时限制; 平台地址刷新只使用已核验来源, 不允许模型扩大可信主机配置
- 消息媒体默认按需读取/缓存, 不后台下载所有历史图片; URL 失效且平台无法回源时明确 `asset_unavailable`
- 现有档案没有通用 native media ID, 增量接入可供刷新使用的适配器原生引用; 原有档案不做伪造补全
- 缓存默认 24 小时, 来源删除/撤回立即撤销授权; 定期清理与引用租约防止发送途中删除
- `expires_at` 使用 Unix 秒时间戳; 发送文件租约通过操作系统锁跨后端和控制进程生效, 每个媒体目录固定 64 个锁槽

宿主内部登记接口拟定为 `register_group_chat_asset(payload, mime_type, origin_ref)`
scope 和有效工作流由宿主取得, payload 只来自受信任工具产物, 不直接映射模型参数
接口返回上述资产对象, 登记失败不返回可使用的 ID; byte/path 入口都必须经过同样内容校验
普通文件回调 token 可在发送时由宿主内部生成, 不作为群工具授权凭据

### 2.3 表情库与前端

表情库有统一条目 `id/name/tags/kind/content_revision`; kind 是 `image` 或 `native`
图片表情由用户上传并登记; native 表情由适配器提供目录和原生映射, 只有适配器层处理原生编码
上传和编辑复用专用控件, 不让用户在插件表单里填写原始 JSON 或服务器路径

在「插件 → group_chat」增加表情库入口: 上传, 命名, 标签, 预览, 停用, 删除
群可选择启用的表情集合, 默认不启用任何用户表情; 未授权集合不会出现在模型目录里
共享表情文件可以物理去重, 授权和引用按群分别核验
删除正在发送的条目会撤销未提交草稿; 已提交发送用原版本租约完成, 不能换成同 ID 新图片

## 3. 独立 memory 插件与群记忆

### 3.0 拆分与旧配置迁移

- `base_take` 仅保留搜索, 网页抓取, 沙箱和文档工具; 删除原记忆工具核心, state, handlers, commands 和配置声明
- 独立 `memory` 提供工具, skill, `memory.memory_inject` 前处理和 `/memory` 命令, 不要求安装 `base_take` 或 `group_chat`
- 公共存储与业务层放在 `satrap/core/memory`, 插件和管理 API 调用同一业务服务
- 保留已有 `memories` 数据, ID 和 `session:<session_id>` 归属, 不自动将旧会话记录变为成员偏好或已批准群约定
- 迁移旧全局配置, Agent 配置与会话覆盖中的 memory_scope/memory_mode 和能力开关; 关闭状态保留, 新增 get_memory 和 skill 不因迁移自动启用
- 新插件已有显式字段优先, 冲突记录明确提示; 全局新配置成功落盘后才清除旧字段, 会话覆盖在同一事务中迁移
- 新增群聊写入开关默认关闭, 旧 full 不自动授予群聊写入权限
- 普通 Chat 与私聊旧记忆保留会话范围; 新群约定与偏好使用稳定 MessageScope, Agent 切换不改变数据归属
- `/memory` 的写入, clear 和 mode 同样经过权限入口, 不能绕过群约定审批或其他成员所有权
- 记忆注入仅由 memory 插件执行一次, group_chat 不新增记忆工具或第二个注入器

### 3.1 类型, 所有权与注入

| 类型 | 所有者 | 示例 | 生效方式 |
| --- | --- | --- | --- |
| `member_preference` | 当前群+当前发言成员 | “叫我小明”, “回答请简短” | 当前成员的下一轮读取/注入 |
| `group_rule` | 当前群 | 群内约定, 项目固定信息 | 审批通过后下一轮读取/注入 |

成员 ID 始终由消息身份确认, 不根据昵称合并; 同一成员在不同群的偏好分开保存
注入只含当前群有效规则和当前发言者有效偏好, 不把全群所有人的资料塞入每轮上下文
查询其他成员偏好只允许已核验当前群成员, 不跨群读同一账号的数据
注入作为有出处的数据块, 不能覆盖系统权限或解释成新的系统指令

### 3.2 模型工具

| 工具 | 参数 | 语义 |
| --- | --- | --- |
| `list_memories` | 可选 `kind`, `user_id`, `keyword`, `limit=20`, `cursor` | 只查当前群有效记忆; user_id 仅用于读已核验成员偏好 |
| `get_memory` | `memory_id` | 读正文, 所有者, 来源状态, revision 与生效状态 |
| `add_memory` | `title`, `content`; 群聊另需 `kind`, `key`, `source_message_ids` | 新增本人偏好或提交群规则提案; 成员偏好 owner 自动取当前发言者 |
| `update_memory` | `memory_id`, `content`, `source_message_ids`, `expected_revision` | 更新本人偏好; 群规则更新仍走审批 |
| `delete_memory` | `memory_id`, `request_message_id`, `expected_revision` | 删除本人偏好; 群规则删除提交审批提案 |

`key` 是稳定用途键, 例如 `preferred_name`, `response_style`, `project_meeting_time`, 长度 1-64
`content` 长度 1-2000, 来源 1-10 条; 不让模型填写所有者, 权限或任意数据库路径
当前群+类型+所有者+key 唯一, 已存在时返回现有 ID 和 revision, 不用新增覆盖旧值
格式规范化只做有限 trim/Unicode 规范化, 不自行合并不同成员或不同含义的 key

```json
{
  "kind": "member_preference",
  "key": "preferred_name",
  "content": "称呼我为小明",
  "source_message_ids": ["m101"]
}
```

返回字段:

```json
{
  "ok": true,
  "status": "saved",
  "memory": {
    "schema_version": 1,
    "memory_id": "gm_example",
    "kind": "member_preference",
    "owner_user_id": "u101",
    "key": "preferred_name",
    "content": "称呼我为小明",
    "revision": 1,
    "state": "active",
    "source_message_ids": ["m101"],
    "source_status": "available"
  }
}
```

### 3.3 写权限与审批

- 模型记忆写入默认关闭, 用户在插件配置中主动开启; 只读和管理界面仍可使用
- 本人偏好仅允许修改当前可信发言者所有的记录, 证据必须包含本轮来源消息, 且全部证据由该成员发出
- 删除本人偏好时 `request_message_id` 必须是本轮来源, 防止仅凭历史“忘掉”文本执行当前删除
- skill 仅在成员明确要求记住/更正/忘记时写入; 普通发言不自动变成长期事实
- 来源与所有权由后端机械核验; “明确表达偏好”的语义仍由模型判断, 不声称能完全消除误判
- 本人偏好默认在受控写入开关启用后直接生效, UI 可回看与更正
- 群规则新增/更新/删除默认全部 `pending`, 经已认证管理界面批准后生效
- 群规则提案不改变当前有效值; 审批时重新校验基准 revision 与来源, 避免批准覆盖较新的决定
- 不复用 OneBot 专属群管理动作类型保存记忆; 新增平台无关的内容提案表, 复用既有审批交互样式
- 不从消息里“我是管理员”授予权限; 管理 API 授权取现有认证, 若以后开放平台成员审批则由适配器提供可信权限证明
- 管理界面可以管理所有范围内记忆, 人工新增记录写 `origin=operator` 与操作者审计, 不伪造平台消息来源

群规则写入返回 `status=pending`, `proposal_id`, `operation=create/update/delete`, `base_revision` 与拟修改内容
提案默认 24 小时过期, 状态为 pending/approved/rejected/expired/conflicted; 每群最多 50 个待审提案
批准通过事务应用真实记忆变化后才返回 approved; 应用存储失败保持 pending 并记录失败, revision 冲突变 conflicted
批准新增后返回 memory_id, 批准更新/删除返回对应新 revision; reject/expired 不改变原记忆

### 3.4 生命周期与 UI

记忆是用户主动保存的长期数据, 默认不随原始档案 30 天保留期删除
来源删除/撤回/过期后保留记忆, 标注 `source_status=unavailable`, 不保留原文副本或尝试绕过删除重新取回
这与派生摘要策略不同, UI 必须明确提示; 同一来源删除操作可选择一并删除关联长期记录
重置模型上下文不删长期记忆; 平台对话数据清理提供独立勾选项, 不模糊“清空聊天”和“删除记忆”
记忆修改/删除在下一轮生效, 正在运行的模型请求保持其已读版本; 提交新写入必须重新核验 revision

「对话记录 → 平台对话 → 群记忆」分群约定和成员偏好, 支持查找, 新增, 修改, 删除和待审批列表
查看来源时使用已有档案查看器, 来源不可用显示原因
默认每群最多 200 条规则, 每成员 50 条偏好; 每轮自动注入最多 20 条/6000 字符
候选按显式用途匹配与最近更新确定性选择, 预算不足声明有未注入项, 可用搜索工具补查
停用插件停止注入和模型访问, 不静默删除用户保存的记录

## 4. 一次性提醒

### 4.1 第一版范围

支持一次性固定文本提醒, 可带已核验成员提及; 创建/查询/取消, 本人记录管理和本地管理 UI
到期不再调用模型, 不执行任意工具, 不持有原入站事件或已结束的 `CallOrigin`
重复日程, 定时摘要, 图片提醒和后台 Agent 工作流后续单独扩展

### 4.2 模型工具

| 工具 | 参数 | 语义 |
| --- | --- | --- |
| `group_chat_create_reminder` | `text`; `due_at` 或 `after_seconds` 二选一; 可选 `mention_user_ids=[]` | 当前群创建一次性任务, 发起者和来源自动取可信当前消息 |
| `group_chat_list_reminders` | 可选 `state`, `limit=20`, `cursor` | 默认只返回当前群由当前发言者创建的任务 |
| `group_chat_get_reminder` | `reminder_id` | 读本人任务, 明确期限, 状态, 修订与发送结果 |
| `group_chat_cancel_reminder` | `reminder_id`, `expected_revision` | 取消尚未取得发送权的本人任务 |

`text`: 1-2000 字符; `mention_user_ids`: 最多 10 个, 禁止 @全体, 创建与发送前均核验当前群成员
`after_seconds`: 整数 10-31536000; `due_at` 必须在接受请求后至少 10 秒, 不超过一年
schema 使用 `oneOf` 声明时间二选一, 宿主仍执行相同校验

```json
{
  "text": "该检查联调结果了",
  "after_seconds": 1800,
  "mention_user_ids": ["u101"]
}
```

```json
{
  "ok": true,
  "status": "created",
  "reminder": {
    "schema_version": 1,
    "reminder_id": "gr_example",
    "revision": 1,
    "state": "scheduled",
    "due_at": "2026-10-04T09:30:00+08:00",
    "due_at_utc": "2026-10-04T01:30:00Z",
    "creator_user_id": "u101",
    "source_message_id": "m101",
    "text": "该检查联调结果了",
    "mention_user_ids": ["u101"],
    "delivery": null
  }
}
```

create 描述: “在当前群创建一次性提醒, 可填具体日期时间或等待秒数; 返回 created 表示已安排, 到期发送结果要通过任务记录确认”
cancel 描述: “取消你创建且还没开始发送的提醒; 已经开始发送的任务可能无法撤回, 工具会明确返回当前状态”

### 4.3 状态机与取消竞争

```text
scheduled -> waiting_delivery -> sending -> sent / partial / failed / unknown
scheduled / waiting_delivery -> cancelled / paused / missed
paused -> scheduled / missed / cancelled
```

- `scheduled`: 已持久化, 等待到期
- `waiting_delivery`: 已到期, 尚未提交网络发送; 短暂等待平台恢复或来源权限核验
- `sending`: 发送计划已落盘并取得不可重入发送权
- `sent`: 全部分段有平台确认; `partial`: 至少一段已确认且后续明确失败
- `failed`: 明确未完成且有失败证据; `unknown`: 请求提交后无法确认, 即使零消息 ID 也不自动重发
- `paused`: 配置或权限关闭, 不处于自动发送队列; 原因与 paused_at 可见
- `missed`: 已超过补发宽限, 不自动补发旧提醒
- `cancelled`: 在尚未 sending 时取消成功; 与抢占在同一数据库状态比较事务中竞争

如果取消晚于发送权取得, 返回 `too_late_to_cancel` 与任务详情, 不把状态改成 cancelled
发送回执的 `success` 映射为任务 `sent`, 其余与现有 `SendReceipt` 原样对应, 已确认消息 ID 可查看
对 unknown/partial 的人工重发需明确创建新任务并展示可能重复的原结果, 第一版没有自动重发按钮

### 4.4 调度与恢复

- 新增后台 `ReminderScheduler`, 由 BackendManager 启停, 不通过启动脚本拉起第二个进程
- 活动任务存 SQLite, 近期限任务进短期堆/计时器; 默认 5 秒扫描兜底, 相对等待使用单调时钟, 定期对齐 UTC 截止时间
- 领取任务使用事务条件更新, 单任务同一时刻只有一个 worker 可进入发送
- SQLite 的 sending 标记和发送计划必须在网络 I/O 前成功持久化, 记录失败就不发送
- 崩溃恢复时已进入 sending 而无确认的任务变 unknown, 不因 worker 租约过期自动再次发送
- 保证不盲目重发, 不承诺跨平台 exactly-once; 平台没有幂等发送键时, 网络与本地事务之间无法消除未知窗口
- 到期离线或重启错过时间时默认宽限 10 分钟; 仅未提交网络 I/O 的任务在此期间等待恢复
- 短暂等待采用 30/120/300 秒退避, 受宽限截止约束; 明确发送失败默认终止, 提交后的超时不进入该重试路径
- 超宽限变 missed, UI 可选择基于原内容创建新提醒; 不默认补发一批历史任务
- 系统时钟明显向后/前跳时暂停领取并记录 `clock_unstable`, 对齐恢复后再按截止时间与宽限判断
- 正常停止暂停新领取并有限等待已在发送的回执, 没有确认的尝试保留 unknown 依据
- 每个任务异常独立捕获, 存储不可用停止提醒发送并告警, 普通群聊继续运行

### 4.5 到期时的配置与权限

任务保存完整群范围, 发起者, 来源消息, 固定正文与提及目标, 不保存会话对象或原始上下文
发送前解析当前适配器实例和连接, 确认同一账号/对话仍可用, 群仍启用, 当前路由的 group_chat 提醒功能仍启用
重新核验发起者权限及提及目标, 不沿用创建时的权限结论
切换 Agent 不改任务归属; 新 Agent 不启用提醒时暂停旧任务, 显示原因
停用平台/群/插件或提醒开关时暂停相关未发送任务; 再启用不静默恢复, 由 UI 或本人明确恢复并重新校验
恢复时若已过宽限返回 missed, 不改变原 deadline 后悄悄发送
适配器无后台发送能力时不允许创建任务; 实例删除后任务可查看, 不自动绑定同名新实例或另一账号
消息档案到期不取消用户已主动创建的长期提醒, 仅标注来源不可用; 对话数据清理可勾选取消提醒

模型仅能读/取消当前发言者创建的任务, 不公开其他成员提醒正文; 管理 API 可以管理授权范围内全部任务
提醒写入默认关闭, 插件配置显式启用后开放; 默认每成员 20 个/每群 200 个活动任务
发送使用原队列与实际回执, 只将已确认发送的消息入档; 提醒不触发新的 Agent 对话轮次

新增适配器内部接口 `group_chat_send_scheduled(target, chain, recorder) -> SendReceipt`
target 是宿主到期重建的可信目标对象, 含完整 scope, 当前连接代次, 当前策略修订和 reminder/attempt ID
接口取得发送队列后再次检查目标代次, 任一核验失败且尚未 I/O 时明确拒绝
适配器按 recorder 在每个实际分段 I/O 前后记录证据; 宿主不通过伪造入站事件绕过这一接口
确认失败前未发出的分段标记 skipped, 原已确认分段不重发, 分段 ID 与档案归属保持一致
任一额外适配器只需实现这份接口并声明能力, 不要求修改 ReminderScheduler 的平台分支

### 4.6 前端

「对话记录 → 平台对话 → 提醒」展示待执行, 暂停, 已发送, 失败/未知/错过等状态
提供创建, 查看, 取消和明确恢复; 创建表单用日期时间选择器或“多久后”, 自动显示解析后的执行时间
详细页展示发起者, 来源, 计划时间, 暂停原因, 实际发送时间与消息 ID
未知结果用“无法确认是否送达”, 不显示“发送失败”诱导直接重发
UI 保存状态与任务执行状态分开, 不沿用“配置已生效”表示提醒已发送

## 5. 存储与管理 API

### 5.1 存储

优先扩展现有每平台 `platform.db`, 使用统一 `ensure_platform_tables` 版本迁移, 不另开零散 JSON
当前平台 schema 是 v9; 实施时为每批统一分配下一版本, 不让新服务各自写 user_version

拟新增表:

| 表 | 主要内容 |
| --- | --- |
| `group_chat_operations` | 内部操作 ID, 幂等指纹, 创建者, 状态和结果索引 |
| `group_chat_summary_snapshots` / `group_chat_summary_sources` | 有效期有界快照与出处, 阅读进度 |
| `group_chat_summaries` / `group_chat_summary_refs` | 摘要结构, 来源 ID/内容摘要, 状态与修订 |
| `group_chat_assets` | 资产归属, 类型, 来源, hash, 缓存相对位置, 到期与授权版本 |
| 扩展已有 `memories` / `memory_refs` | 当前记忆, 所有权, 来源和修订 |
| `memory_proposals` | 群规则增改删提案, 基准 revision, 审批与执行状态 |
| `group_chat_reminders` / `group_chat_reminder_attempts` | 提醒状态, 固定内容与发送证据 |
| `group_chat_preferences` | 每群功能开关/表情集合/配额覆盖与修订 |

活动表都包含 scope_key, 不能只凭 ID 查询后再补范围检查
短期图片在 StorageLayout 的平台 cache/群聊媒体目录, 用户表情放专用 data/资产目录
全局表情库登记保存在 `.satrap/data/group-chat/catalog.db`, 文件统一放 `.satrap/data/group-chat/stickers/`; 不与平台授权混在一起
配置继续使用 `.satrap/config` 下既有插件配置文档; 不为每条摘要/记忆/提醒创建独立文件
缓存启动时及周期维护, 用户表情按显式删除与引用计数回收; UI 预览复用有认证与有效期的资源接口
群聊媒体缓存默认每平台 256 MiB, 宿主合计 1 GiB; 优先回收过期/未租用缓存, 仍不足时拒绝新登记, 不删除用户表情
只持久化必要内容, 记忆/提醒来源不保存完整原文副本
终态提醒正文与发送尝试默认保留 30 天; 幂等最小记录可额外保留但不携带正文, 活动提醒不得被清理
记忆硬删除清除正文与来源关系, 审计只保留操作者/类型/时间; 删除群规则同步清理含旧内容的过期提案

### 5.2 拟定管理接口

复用现有认证控制入口与后端代理, URL 路径不传 opaque chat ID, 范围通过 `self_id/chat_id` 查询字段解析

摘要与提醒基础路径: `/api/platforms/{adapter_id}/group-chat`
记忆与记忆审批基础路径: `/api/platforms/{adapter_id}/memory`, 不依赖 group_chat 的安装状态

| 相对路径 | 方法 | 作用 |
| --- | --- | --- |
| `/summaries` | GET | 列表 |
| `/summaries/{summary_id}` | GET, DELETE | 详情与删除 |
| `/memories` | GET, POST | 列表与人工新增 |
| `/memories/{memory_id}` | GET, PATCH, DELETE | 查看/修改/删除 |
| `/proposals` | GET | 待审批和已处理提案 |
| `/proposals/{proposal_id}/decision` | POST | approve/reject, 校验基准修订 |
| `/reminders` | GET, POST | 列表与创建 |
| `/reminders/{reminder_id}` | GET | 任务详情 |
| `/reminders/{reminder_id}/cancel` | POST | 取消 |
| `/reminders/{reminder_id}/resume` | POST | 显式恢复 |
| `/preferences` | GET, PATCH | 本群功能覆盖, 配额和表情集合 |

全局表情库路径: `/api/group-chat/stickers`, 上传用 multipart, 内容按实际文件校验
PATCH/DELETE/decision/cancel/resume 使用 expected_revision; 写请求使用幂等键, 前端冲突保留草稿
列表统一 `items/has_more/next_cursor`, 排序和查询条件固定, 游标绑定范围/条件/读取修订, 无效时要求重查
不同功能的正文读取均经过群范围认证, 不能凭随机 ID 获得其它群记录或资产

第一版前端功能入口在既有平台对话详情下扩展, 不新增混淆历史记录与 Agent 配置的入口
如果某适配器没有专门群管理页, 仍可通过通用平台对话详情访问这些功能

## 6. 配置默认值

以下是待实现默认值, 安装升级不自动开启持久写入或后台发送

| 配置 | 默认 | 上限/说明 |
| --- | --- | --- |
| `summary_enabled` | true | 主动请求才生成, 无自动定时摘要 |
| `summary_message_limit` | 500 | 1-2000, 仍受模型上下文预算 |
| `summary_text_budget` | 60000 | 1000-200000, 不盲目推断 token 数 |
| `summary_retention_days` | 30 | 1-3650, 实际不超过来源可访问期限 |
| `media_reply_enabled` | true | 仍受适配器能力与来源约束 |
| `max_reply_images` | 4 | 0-8 |
| `max_reply_stickers` | 4 | 0-8 |
| `memory_mode` | full | memory 插件的 disabled/base/full, 不覆盖能力与身份限制 |
| `group_write_enabled` | false | memory 插件的群聊模型写入显式开启 |
| `injection_limit` | 20 | 1-50 |
| `injection_budget` | 6000 | 1000-20000, 受模型上下文约束 |
| `reminders_enabled` | false | 创建与后台发送一起受开关控制 |
| `reminder_catchup_seconds` | 600 | 0-3600, 0 表示不补发 |
| `active_reminders_per_member` | 20 | 1-100 |
| `active_reminders_per_group` | 200 | 1-1000 |

资源硬限制和存储维护参数由宿主统一限制, 不将所有内部细节塞进 Agent 表单
有效上限取宿主上限, 插件安装配置和本群覆盖的最小值; 群覆盖可缩小, 不能突破宿主/安装上限
权限与能力关闭始终优先于模型参数或旧任务配置
前处理注入本轮实际可用工具/额度/解析后的当前时间, 不注入未实现能力

## 7. 实施批次与验收

每个功能都先落契约和宿主, 再适配器/插件/UI, 最后现场验收, 分批提交

| 批次 | 内容 | 最小验收 |
| --- | --- | --- |
| A1 | 摘要快照/出处/覆盖与预算, 存储迁移 | 跨群隔离, 冻结后新消息不混入, 删除使来源失效 |
| A2 | 五个摘要工具, skill 与摘要页 | 可追溯保存/查询, 无额外模型调用, 超限与 0 条行为 |
| B1 | 资产登记, 文件租约与两类新组件 | 临时文件结束后仍可发送, 禁止跨群/路径/过期引用 |
| B2 | 适配器能力/表情库/插件工具与 UI | QQ 图片和配置表情, 额外虚拟平台不依赖原生编号 |
| C1 | 独立 memory 插件, 公共存储与业务, 全局/Agent/会话覆盖迁移, 清除旧实现 | 原记录与 ID 保留, 能力关闭状态不改变, 重复迁移无副作用 |
| C2 | 群约定/成员偏好, 来源/所有权/审批/revision, 统一工具/命令/注入和记忆页 | 他人修改拒绝, pending 不生效, 切换 Agent/重启仍可读, 下一轮生效 |
| D1 | 提醒状态机/发送账本/后台发送接口 | 重启恢复, 取消竞争, 崩溃 sending 变 unknown 且不重发 |
| D2 | 四个提醒工具, UI 与生命周期协调 | 离线宽限, 停用暂停, 路由切换, QQ 实际单次到期发送 |

共同自动化验收:

- 同步/异步工具一致, 主/子工作流身份, 首轮 skill, 启停插件, 跨群/跨账号/跨平台隔离
- ID 使用含特殊字符的虚拟平台, 能力缺失返回明确原因, 无核心平台名称分支
- 不带时区/显式时区/跨日时间, UTC 归一, 系统时区变化, 提醒时钟跳变
- SQLite 写失败, 幂等重放, revision 冲突, 超时取消和批量维护异常不阻断其它群
- UI 草稿保留, 无 JSON 必填流程, 窄屏, 空结果/来源缺失/未知发送状态
- 原有 text/quote/mention, 私聊/群聊路由, 原档案删除/恢复和普通回复继续通过回归

现场验收只在已授权测试群逐步进行:

1. 同一时段多成员讨论生成摘要, 核对每条来源和覆盖信息
2. 引用当前消息图片, 回复文字+图片+表情, 确认顺序与无重复发送
3. 本人昵称偏好跨重启保留, 修改/忘记生效; 群约定审批前不生效, 审批后下一轮读取
4. 短提醒, 重启等待提醒, 取消提醒, 离线错过, 配置停用暂停; 对 unknown 使用隔离替身验证, 不故意造成真实群重复消息

本轮不处理既定暂缓的 PR bug #7, 不修改 SnowLuma 清空名片实现
