# 好友管理

平台管理中的「管理好友」供后台登录用户操作当前机器人账号的好友关系, 不依赖模型插件是否安装或启用
好友列表, 申请和动作均绑定平台实例与机器人账号; 账号或连接变化后旧查询和旧审批拒绝继续执行

## 人工界面

- 好友列表: 按 ID, 昵称或备注搜索, 分页查看, 详情中复制 ID 或确认删除
- 好友申请: 显示申请人, 验证信息和期限, 同意时可填写备注, 拒绝需确认
- 操作记录与审批: 查看人工和模型操作, 批准或拒绝模型删除申请, 查看实际失败原因
- 删除保护: 每行配置受保护的好友 ID, 同时约束人工和模型操作; 已配置的好友管理者自动保护

人工确认后直接执行, 不需再次批准同一操作; 模型删除须在后台批准
删除只解除好友关系, 不拉黑, 不删除历史对话或模型上下文

## 模型插件

安装独立的 friend_manager, 五个工具如下:

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| friend_manager_list_friends | limit, cursor 可选 | 好友列表 |
| friend_manager_find_friends | query 必填, limit 和 cursor 可选 | 按 ID, 昵称或备注搜索 |
| friend_manager_list_requests | limit, cursor 可选 | 当前账号仍可处理的好友申请 |
| friend_manager_handle_request | request_id, approve 必填, remark 可选 | 授权后直接处理申请 |
| friend_manager_delete_friend | user_id 必填 | 申请删除单个好友, 等待人工批准 |

所有模型工具仅向 managers 名单中的管理者私聊提供, 留空禁用全部工具
写调用者必须同时属于 managers 和 write_callers; request_handling_enabled 与 delete_friend_enabled 默认关闭
protected_friend_ids 为插件额外模型目标保护, 人工操作遵守宿主账号保护名单
好友管理者在命名配置, 全局配置和当前实例中的名单均受宿主保护, 插件停用不移除保护
工具不接收账号或平台 ID, 不允许模型选择其它机器人账号
同步和异步 Agent 共用同一宿主, 写工具 recovery_policy=manual

查询 limit 为 1 到 100, 默认 20; 好友页为约束输出体积最多返回 30 条, 剩余项通过 next_cursor 读取
好友查询快照有效期 120 秒, 绑定账号, 调用者, 查询文字和连接代次
搜索按精确 ID, 精确昵称或备注, 包含匹配排序; 相同优先级候选全部保留
coverage.complete=false 表示底层目录不完整, has_more=true 表示还有查询结果, 不可据此唯一确定目标
申请列表只涵盖宿主收到且仍有执行资格的申请, 不声称平台全部历史申请
remark 最长 60 字符且只在同意申请时使用, 拒绝时非空备注返回参数错误

## 动作与失败契约

写操作保留 action_id 做幂等登记, 同 ID 不同参数拒绝; 相同目标尚有待处理或结果未知的删除时拒绝再次提交
pending 只表示等待批准, executing 已占用执行, succeeded 表示平台明确返回成功
删除结果 verification=confirmed 表示重新读取完整目录确认目标消失, still_present 表示仍显示目标, not_verified 表示尚未核查
failed, rejected, expired, unknown 均不是成功, 超时或传输异常不自动重试
进程重启时执行中动作恢复为 unknown, 模型待审批动作因来源丢失而过期
批准后复核源 Agent 配置, 路由, 插件和工具状态, 管理者权限, 目标关系以及连接代次
后端失败在工具或 API 边界捕获并日志记录; 不打印原始申请 flag, 验证信息或协议响应正文

## 平台扩展与存储

宿主使用通用 friend_* 适配器接口, 平台 ID 使用字符串, 不假设 QQ 数字格式
OneBot 使用 get_friend_list 和 set_friend_add_request 标准接口, delete_friend 属于实现扩展
扩展删除能力未验证时显示 unknown, 可以经人工确认尝试, 不通过实际删除探测能力; 不支持时返回 unsupported 并被动记忆
好友搜索在宿主对真实目录执行, 未实现接口的平台显示能力原因
动作和额外保护名单保存到已有 platform.db 的 friend_actions 和 friend_policies, 不新增零散 JSON 或日志
原始申请凭据沿用宿主私有收件箱和不可重放账本

group_admin 中两个旧好友申请工具迁移后移除, 无运行时别名或重复实现
旧配置迁移保留申请工具开关, 管理者与写权限交集; 新增列表, 搜索, 删除及 skill 不自动启用
friend_manager, group_admin 和 group_chat 无插件间依赖
