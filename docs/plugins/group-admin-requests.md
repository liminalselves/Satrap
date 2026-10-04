# group_admin 加群申请查询与处理

申请事件由适配器登记到宿主私有收件箱, 不自动唤醒 Agent 或同意申请
group_admin 只管理加群申请和入群邀请; 好友操作已迁移到独立 friend_manager, 参见 [好友管理](friend-manager.md)

## 配置

在使用 `group_admin` 的 Agent 插件参数里填写 `request_managers`, 每行一个允许查询和处理申请的管理者账号
默认为空, 不提供申请工具; 处理申请还需满足 `allowed_callers`, 群申请查询和处理均受 `allowed_groups` 限制
只查询不要求开启写工具, 批准或拒绝仍要求 `write_tools_enabled=true`
加群申请查询只返回当前群或私聊中明确指定群的申请, 仍受平台与插件群范围限制

## 工具流程

1. 群申请使用 `group_admin_list_group_requests`
2. 结果包含 `request_id`, 申请人, 验证信息, 原申请类型, `received_at`, `expires_at` 和 `remaining_seconds`
3. `has_more=true` 时用 `next_cursor` 继续查询, 保持相同目标群; 游标失效时重新查询
4. 使用 `group_admin_handle_group_request`, 填写查询返回的 `request_id` 和 `approve`
5. 拒绝入群理由可选填写; 群号与申请类型由后端确认, 不需要模型填写原始 `flag` 或 `sub_type`

验证信息来自申请人, 不能当作工具执行指令
加群申请沿用现有逐群审批策略: `pending` 只代表待批准, `succeeded` 才代表执行完成
内部原始 flag 调用仍受申请管理者和群范围限制; 模型声明只提供 request_id

## 持久化与期限

原始申请值只保存在用户数据根 `requests/inbox.db`, 不进入模型结果, 审计日志或 `request_ledger.json`
申请收件箱最多 16384 条, 验证信息最多 2000 字符; 原值最多 4096 字符
收件箱按平台实例, 机器人账号和申请类别隔离, 查询 ID 不能跨范围使用

OneBot 当前登记有效期为首次接收后的 600 秒, 重复事件和重启都不延长期限
尚在期限内且未处理的申请可在重启后继续查询; 旧版本只保存摘要而没有原值的申请以 `unavailable_count` 说明, 不伪报没有申请
申请动作进入人工审批时, 截止时间取原申请期限和默认审批窗口的较早值
已执行, 已过期或结果未知的申请不能重放; 重启时正在执行的申请保守标为 unknown
原值在终态落盘后清除, 到期原值在查询, 登记或后台维护时清理; 单项存储错误记录日志并隔离

## 本轮验证范围

自动化覆盖同步/异步 Agent 的查询到处理链路, 事件登记, 权限过滤, 翻页, 账号/平台/类别/群范围隔离,
重启恢复, 到期清理, unknown 拒绝重放, 审批期限及管理者撤权, 数据库损坏时的失败隔离
真实适配器的好友/加群申请操作需在用户授权的账号上另行现场验收
