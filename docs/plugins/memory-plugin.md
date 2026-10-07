# memory 插件: 独立长期记忆

`memory` 提供 `add_memory`, `update_memory`, `delete_memory`, `list_memories`, `get_memory`, `/memory` 命令与每轮记忆注入
可以单独安装, 不依赖 `base_take`, `group_chat` 或 `satrap_coding`

```python
session.install_plugin("./satrap/expend/plugins/memory")
```

## 数据与配置

数据继续存储在当前平台的 `platform.db`, 原 `memories` 记录的 ID 和会话归属保持不变
宿主通过 `memory_db` 与 `memory_scope` 绑定运行时范围, 平台运行时不能用插件配置覆盖该范围

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `memory_mode` | full | disabled 关闭模型读取和注入; base 只读; full 允许权限范围内写入 |
| `memory_scope` | 空 | 独立调用的作用域覆盖, 平台运行时由宿主决定 |
| `group_write_enabled` | false | 新增群聊写入必须明确开启, 不因旧 full 自动开启 |
| `injection_limit` | 30 | 每轮最多注入条数 |
| `injection_budget` | 6000 | 每轮记忆数据字符预算 |

C1/C2 已实现拆分, 群记忆与成员偏好, 并与 D1/D2 一并完成自动化回归和浏览器冒烟
真实 QQ 的跨重启记忆与提醒送达仍需现场验收, 自动化使用隔离数据库和替身适配器
前端人工管理使用已有认证, 不受模型的 memory_mode 限制; 模型工具与命令不能借此获得人工管理权限

## 旧配置迁移

- 全局 memory_scope/memory_mode 从 base_take.json 移到 memory.json, 新插件已有显式值优先, 冲突写日志
- Agent 配置的旧记忆工具开关, 注入和命令开关移交 memory; 关闭状态保留
- 会话覆盖在同一 SQLite 事务中迁移, 修订号更新, 不复制长期记忆正文
- 迁移不启用新增 get_memory 或 skill, 用户可在插件能力选择中开启
- 原基础插件仅保留搜索, 网页抓取, 代码沙箱和文档读取; 不注册旧记忆执行别名
- 保存迁移后的完整配置后, 再移除 memory 不会因 base_take 仍安装而自动重新添加

## 使用边界

记忆只在用户明确要求记住, 更正或忘记时写入, 普通讨论不自动保存为长期事实
子工作流不能修改长期记忆, 记忆读取失败不会中断普通对话
重置模型上下文与删除长期记忆是不同操作, 前端需要明确选择范围

模型结果的形状: 普通会话成功仍返回中文说明文本, 群聊成功返回群记忆宿主结果; 失败统一为框架扁平结果
`{"ok": false, "error": <说明>, "error_type": <稳定类型>, "tool_name": <工具名>}`
`error_type` 取值: `read_only` / `memory_disabled` (只读或已禁用), `invalid_argument`, `not_found`, `write_disabled` (群记忆写入未开启),
`read_only_workflow` (子工作流), `stale_call` (来源或配置已变化), `unavailable` (存储或平台暂不可用)

群提醒见 [group_chat 插件](group-chat-plugin.md#一次性提醒)


## 群记忆与成员偏好

- 群记忆按平台实例, 机器人账号与群保存, 成员偏好再绑定成员 ID; 不随 Agent 切换改变归属
- 模型新增本人偏好需包含本轮本人来源, 修改和删除必须携带查询结果的 revision
- 模型提交的群记忆增改删进入待审批, 当前有效值不变; 人工审批重新核验来源和基准版本
- 模型默认列表仅含群记忆和本人偏好, 查询其他成员须由适配器确认该成员属于当前群
- 每轮仅注入本群记忆和当前发言者偏好, 最多 20 条, 默认 6000 字符, 遗漏明确标注
- 停用 memory 停止模型访问与注入, 不删除记录; 已认证人工管理仍可使用
- 群内 /memory clear 与 mode 拒绝执行, 单条偏好写入遵守相同来源和权限规则
- 来源删除或过期后记忆保留并标注来源不可用, 审计不保存记忆正文副本
- 删除原始档案时可独立勾选删除引用所选消息的记忆和提案; 清空档案时可选择删除本群全部长期记忆
- 长期记忆硬删除与档案删除在同一事务中应用, 失败整体回滚; 恢复档案不会恢复已明确删除的记忆

人工入口: 对话记录 → 平台对话 → 长期记忆, 分为群记忆, 成员偏好和待审批记录
管理 API 基础路径为 /api/platforms/{adapter_id}/memory, memories 提供增删改查, proposals 提供审阅与 decision
