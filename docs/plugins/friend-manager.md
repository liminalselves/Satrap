# 好友管理

平台管理中的「管理好友」供后台登录用户操作当前机器人账号的好友关系, 不依赖模型插件是否安装或启用
好友列表, 申请和动作均绑定平台实例与机器人账号; 账号或连接变化后旧查询和旧审批拒绝继续执行

## 人工界面

- 好友列表: 按 ID, 昵称或备注搜索, 分页查看, 详情中复制 ID 或确认删除
- 发送好友申请: 填写目标账号和验证文字, 根据适配器能力启用; 提交不代表已成为好友
- 好友申请: 分待处理和已归档, 显示申请人, 验证信息, 平台状态与执行状态; 支持重新核验, 确认处理, 删除历史与保留期限配置
- 操作记录与审批: 查看人工和模型操作, 批准或拒绝模型删除申请, 查看实际失败原因
- 删除保护: 每行配置受保护的好友 ID, 同时约束人工和模型操作; 已配置的好友管理者自动保护

人工确认后直接执行, 不需再次批准同一操作; 模型删除须在后台批准
删除只解除好友关系, 不拉黑, 不删除历史对话或模型上下文

## 模型插件

安装独立的 friend_manager, 七个工具如下:

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| friend_manager_list_friends | limit, cursor 可选 | 好友列表 |
| friend_manager_find_friends | query 必填, limit 和 cursor 可选 | 按 ID, 昵称或备注搜索 |
| friend_manager_list_requests | limit, cursor, view 可选 | active 待处理, archived 归档, all 全部本地记录 |
| friend_manager_recheck_request | request_id 必填 | 重新核验本地资格或适配器支持的平台状态 |
| friend_manager_handle_request | request_id, approve 必填, remark/expected_revision 可选 | 近期申请授权后处理; 归档申请需当前修订号及人工批准 |
| friend_manager_delete_friend | user_id 可选, 默认当前发送者 | 申请删除本人或获授权管理员指定的好友, 等待人工批准 |
| friend_manager_send_request | user_id/message 可选, 默认当前发送者 | 主动发送好友申请, 普通用户只能添加本人 |

模型工具在群聊和私聊提供稳定声明, 不按当前发言人权限隐藏; 调用时拒绝无权限操作
完整好友列表和搜索要求 access 权限; 查询, 核验和处理申请对普通用户只返回或操作本人记录
普通用户可请求删除本人, 无需填写 managers 或 write_callers; 删除仍须人工批准并遵守保护名单
管理员查询全部申请要求 access, 处理他人申请或好友关系要求 access 和 write; 系统管理员可通过本插件授权获得对应权限
request_handling_enabled, delete_friend_enabled 和 send_request_enabled 默认关闭, 本人服务也遵守对应开关
申请查询返回 scope=self 或 all, 先按身份过滤再计算条数和分页; 游标绑定身份与范围, 无权使用其他人的游标或 request_id
protected_friend_ids 为插件额外模型目标保护, 人工操作遵守宿主账号保护名单
好友管理者在命名配置, 全局配置和当前实例中的名单均受宿主保护, 插件停用不移除保护
工具不接收账号或平台 ID, 不允许模型选择其它机器人账号
同步和异步 Agent 共用同一宿主, 写工具 recovery_policy=manual

查询 limit 为 1 到 100, 默认 20; 好友页为约束输出体积最多返回 30 条, 剩余项通过 next_cursor 读取
好友查询快照有效期 120 秒, 绑定账号, 调用者, 查询文字和连接代次
搜索按精确 ID, 精确昵称或备注, 包含匹配排序; 相同优先级候选全部保留
coverage.complete=false 表示底层目录不完整, has_more=true 表示还有查询结果, 不可据此唯一确定目标
申请列表只涵盖宿主曾收到且仍在保留期限内的记录, 不声称平台全部历史申请
remark 最长 60 字符且只在同意申请时使用, 拒绝时非空备注返回参数错误

## 动作与失败契约

写操作保留 action_id 做幂等登记, 同 ID 不同参数拒绝; 相同目标尚有待处理或结果未知的删除时拒绝再次提交
pending 只表示等待批准, executing 已占用执行, succeeded 表示平台明确返回成功
删除结果 verification=confirmed 表示重新读取完整目录确认目标消失, still_present 表示仍显示目标, not_verified 表示尚未核查
failed, rejected, expired, unknown 均不是成功, 超时或传输异常不自动重试
进程重启时执行中动作恢复为 unknown, 模型待审批动作因来源丢失而过期
批准后复核源 Agent 配置, 路由, 插件和工具状态, 管理者权限, 目标关系以及连接代次
后端失败在工具或 API 边界捕获并日志记录; 不打印原始申请 flag, 验证信息或协议响应正文

模型结果的形状: 成功返回 `{"ok": true, "data": ...}`; 失败返回嵌套结果 `{"ok": false, "error": {"code", "message", "retryable"}}`,
`retryable` 只说明可以安全地重新查询或在尚未提交时重试, 不授权重发结果未知的动作

## 平台扩展与存储

宿主使用通用 friend_* 适配器接口, 平台 ID 使用字符串, 不假设 QQ 数字格式
OneBot 使用 get_friend_list 和 set_friend_add_request 标准接口, delete_friend 属于实现扩展
主动好友申请通过通用 friend_send_request 接口接入; 当前 OneBot 适配器没有已确认的主动申请接口, 明确返回 unsupported, 不用入站审批替代
扩展删除能力未验证时显示 unknown, 可以经人工确认尝试, 不通过实际删除探测能力; 不支持时返回 unsupported 并被动记忆
好友搜索在宿主对真实目录执行, 未实现接口的平台显示能力原因
动作和额外保护名单保存到已有 platform.db 的 friend_actions 和 friend_policies, 不新增零散 JSON 或日志
原始申请凭据沿用宿主私有收件箱和不可重放账本

## 申请归档

本地 10 分钟期限只控制待处理列表, 到期归档不代表平台申请失效
归档保留同一个 request_id, revision 随状态或保留设置变化更新
can_handle 仅表示本地仍具备尝试资格, 不表示平台确认有效; requires_confirmation 表示需要明确确认
OneBot 当前核验返回 verification=local_only 与 platform_query_supported=false, 不假装查询了 QQ 服务器
凭据存在且未执行的归档申请可经人工确认尝试处理, 模型提交时先生成 pending 审批动作
处理带 expected_revision, 账号或修订号变化后拒绝旧操作, 并发占用只执行一次
已经处理, 凭据缺失或结果未知的申请禁止重放; 删除历史不删除防重复执行账本

每账号默认保留未处理凭据 30 天, 历史 90 天, 可配置 1 到 3650 天, 历史期限不得短于凭据期限
终态立即清除凭据; 延长期限不会恢复已清除的凭据; 重复事件及重启不会重置申请身份或有效期
旧版本已删除的申请原值不能凭摘要恢复; 平台重新上报同一未执行申请时可在保留期限内补齐缺失详情, 不重置首次接收时间或执行状态
不同申请 flag 分别登记, 不按申请人 ID 去重; 已处理或结果未知的重复事件不重新放行
部分适配器会复用同一人的处理 flag, 好友申请以 flag 与平台原始事件时间共同区分; 更晚的新事件生成新的 request_id, 同时间的重复上报不刷新状态或期限
复用 flag 时, 旧申请保留在归档中但不能再处理; 旧审批不能操作新申请, 执行和终态回写始终绑定本次 request_id
原始事件时间缺失或无效时记录告警并保守去重, 不用本地接收时间推测新申请; 已收到新申请后拒绝缺少时间的同凭据事件
unavailable_count 只统计本次身份与视图范围内缺失的详情, 不表示暂时不可读; 后端离线期间未收到的申请不能从本地归档补出

group_admin 中两个旧好友申请工具迁移后移除, 无运行时别名或重复实现
旧配置迁移保留申请工具开关, 管理者与写权限交集; 新增列表, 搜索, 删除及 skill 不自动启用
friend_manager, group_admin 和 group_chat 无插件间依赖
