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

当前 C1 完成拆分基础, 群约定与成员偏好的写入在 C2 完成前明确拒绝
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

群约定, 成员偏好, 审批与提醒的后续契约见 [第二阶段计划](../development/group-chat-phase2-contracts.md)
