# 原消息转发

`message_forward` 是独立插件, 通过宿主平台接口转发原消息, 不依赖 `group_chat`, `group_admin` 或 `friend_manager`
当前实现同一平台实例, 同一机器人账号下的群聊到群聊, 群聊到私聊, 私聊到群聊和私聊到私聊

| 工具 | 用途 | 参数 |
| --- | --- | --- |
| message_forward_read | 查看合并转发内容预览 | source_message_id, source 可选 |
| message_forward_send | 合并原消息或转发已有卡片 | message_ids, mode/source/target 可选 |
| message_forward_compose | 创建机器人署名的文字合集 | nodes, target 可选 |

`source` 和 `target` 使用 `{conversation_kind: "group" 或 "private", conversation_id: "对话 ID"}`, 省略时为当前对话
平台, 机器人账号和实际调用者由宿主提供, 工具不接受这些身份参数
`mode=merge` 合并 1 到 30 条原消息, `mode=existing_forward` 只接受一个含转发卡片的来源消息 ID
平台原消息引用可能按原消息时间显示, 不承诺按模型提交顺序重排

读取预览可能截断, 发送不使用预览, 不接受模型重写原文或伪造发送者
发送前回源核验账号和来源对话, 等待及入队后复查权限与连接
OneBot 优先发送原消息引用节点, 已有卡片使用原生单条转发接口; 接口缺失时明确失败, 不降级为文字复述
`compose` 属于新内容, 每段最多 2000 字符, 不能称作原文转发
`status=success` 表示平台返回了可确认的消息回执; `failed` 或 `unknown` 均不能称作已送达, `unknown` 不自动重发

发送开关 `send_enabled` 默认关闭; `write_callers` 留空不授予名单写权限
`read_callers` 留空不额外限制当前对话读取; 所有调用仍受来源账号和平台对话范围限制
插件不再提供 `allow_private` 或 `allowed_groups`, 群聊与私聊统一遵守平台自身的对话范围; 系统管理员仍受本插件排除规则与功能开关限制

跨对话允许模型直接调用, 不强制使用命令; 来源或目标不是当前对话时追加检查 cross 权限, 读取其他对话也受此限制
需开启 `cross_conversation_enabled`, 并授予 `cross_callers` 或对应系统管理员权限

```text
/forward read <卡片消息 ID>
/forward merge <group/private> <目标 ID> <来源消息 ID...>
/forward relay <group/private> <目标 ID> <卡片消息 ID>
```

命令从当前对话取来源, 转发资料不会作为命令或授权解析
旧 `group_admin_get_forward` 迁移为 `message_forward_read`, 旧 `group_admin_send_forward` 迁移为 `message_forward_compose`
迁移不启用原消息发送, 跨对话, 命令或 skill; 保留调用者名单, 清除废弃的私聊开关和插件群名单, 对话范围改由平台统一管理
系统管理员针对旧插件的授权不会隐式授予新插件, 需要在管理员设置中配置 `message_forward`
旧工具及实现已经移除, 不保留运行时别名
只对包含旧工具名的能力配置执行迁移; 未保存工具开关的旧简写配置需手动添加新插件, 不把新建群管理配置误判成旧转发配置
