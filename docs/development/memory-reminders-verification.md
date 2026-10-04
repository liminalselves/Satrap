# C1/C2/D1/D2 自动化验收

日期: 2026-10-05

代码, 自动化检查和真实平台现场验收分别记录. 本轮没有向真实 QQ 群发送消息或修改群数据

## 检查结果

| 检查 | 结果 |
| --- | --- |
| 后端全量默认测试 | 最终完整复跑 3169 passed, 20 skipped, 用时 427.95 秒 |
| 最后并发身份复核增量检查 | 记忆与提醒相关 36 passed, 新增运行时 HTTP 认证/创建/恢复检查 1 passed |
| 前端单元测试 | 27 个文件, 234 passed |
| 前端 lint | 通过, 无警告 |
| 前端 tsc 与生产构建 | 通过 |
| 本轮生产 Python 模块 pyright | 0 errors, 0 warnings |
| 新增记忆/提醒浏览器冒烟 | 通过, 已查看 390px 窄屏截图 |
| 既有浏览器回归 | 档案, Agent 路由, 对话编辑, 群聊插件, 好友, 摘要, 媒体共 7 个脚本通过 |
| 真实 QQ 现场验收 | 未执行; 当前本地后台与控制服务未运行 |

20 个跳过项中, 16 项为需要显式启用的集成测试, 1 项缺少可选 reportlab, 3 项需要当前 Windows 会话没有的符号链接权限
默认全量测试没有调用外部模型, embedding 或 RAG 服务, 不据此声称这些外部服务已通过验收

## 契约与证据

| 批次或边界 | 自动化证据 | 覆盖内容 |
| --- | --- | --- |
| C1 独立插件与迁移 | test_memory_plugin.py, test_memory_migration.py | 旧记录 ID, 能力关闭状态, 新显式配置优先, 原子迁移与重复迁移 |
| C2 模型真实管线 | test_memory_platform.py | 同步/异步 Agent, 无 base_take/group_chat 依赖, 下一轮注入, 主/子工作流与读后身份复核 |
| 群记忆数据 | test_scoped_memories.py | 本人所有权, 当前来源, revision, 幂等, 提案审批, 失效来源与跨范围游标 |
| 人工记忆管理 | test_memory_management.py | 实际控制请求认证, 人工 CRUD, 冷数据访问与审批 |
| D1 发送权与证据 | test_reminder_store.py, test_scheduled_sends.py | 取消竞争, 事务失败不发送, 部分回执, sending 重启变 unknown, 未知结果不重试, 所有分段有确认才能 sent |
| D1 原生适配器 | test_onebot_reminders.py | 真实 OneBot 分段/串行队列代码, 独享发送账本, 队列等待后账号复核, 缺少消息 ID 不视为成功 |
| D1 时间与调度 | test_reminder_time.py, test_reminder_scheduler.py | 相对接受时间, 本地/显式时区, DST 歧义/不存在时间, 时钟跳变, 离线退避/宽限, 停用不自动恢复 |
| D2 模型工具 | test_reminder_plugin.py | 实际同步/异步模型工具管线, 创建幂等, 来源等待期间删除, 本人查询, 禁止子工作流写入与参数伪造 |
| D2 当前授权 | test_reminder_host.py | 真实命名配置/Provider, 非数字虚拟平台 ID, 平台实例, 账号, 群, 插件/能力, 来源覆盖与路由切换 |
| D2 运行时 HTTP | test_reminder_host.py, test_reminder_management.py | 完整 HTTP 认证边界, 创建幂等, 恢复版本冲突和当前权限, 冷读取/取消, 删除来源标不可用 |
| 长期数据清理 | test_conversation_long_term_cleanup.py | 默认保留, 独立勾选, 跨群/账号隔离, 提案正文擦除, 全事务回滚, 已发送权取得后不取消, 档案恢复不恢复记忆或任务 |
| 本轮人机界面 | e2e/group-chat-memory-reminders.mjs | 来源查看, 成员偏好增删, 群约定编辑/审批, 冲突保留草稿, 创建失败稳定幂等键, 日期转换, 取消/恢复, unknown 提示, 账号隔离和清理影响范围 |

OneBot 自动化使用替身客户端回包, 未连接真实适配器. 浏览器脚本运行真实前端与 Vite, API 使用替身路由; 后端认证和真实数据库操作由独立 HTTP 测试覆盖

## 复现命令

```powershell
F:/conda/python.exe -m pytest -q --durations=10
F:/conda/Scripts/pyright.exe --pythonpath F:/conda/python.exe satrap/core/memory satrap/core/group_chat/reminders.py satrap/core/group_chat/reminder_host.py satrap/core/group_chat/reminder_service.py satrap/core/group_chat/reminder_management.py satrap/core/group_chat/reminder_scheduler.py satrap/core/group_chat/reminder_time.py satrap/core/platform/scheduled.py satrap/expend/plugins/memory
```

运行命令前须按项目规定设置 PowerShell 输入/输出和 Python UTF-8, 大规模测试限制 OMP/BLAS 工作线程. 前端命令在 satrap-ui 下执行:

```powershell
npm.cmd test
npm.cmd run lint
npm.cmd run build
npm.cmd run test:e2e:group-chat-memory-reminders
```

## 待执行的真实 QQ 场景

沿用已授权测试群 1125293646, 启动包含本轮本地提交的最新后端和控制服务后进行:

1. 在该群的实际 Agent 启用 memory 工具/注入与 group_write_enabled, 明确要求保存自己的称呼偏好, 核对前端的成员和来源
2. 在下一轮读取该偏好, 重启后端再核验, 然后明确更正和忘记, 验证下一轮生效
3. 提交一条群约定, 验证 pending 时没有改变有效值, 前端批准后下一轮可读取
4. 启用 group_chat 创建提醒能力与 reminders_enabled, 建立短提醒, 核对只发送一次及实际消息 ID
5. 核验到期前取消, 等待过程中重启, 暂时离线恢复, 停用后暂停且再启用不自动恢复
6. unknown/partial 和崩溃提交窗口继续使用隔离替身验证, 不在真实群故意制造重复消息

群聊 skill 引导模型仅在明确要求记住/更正/忘记时保存. 语义理解和是否正确采用偏好仍需实际模型回复验证, 自动化机械权限检查不替代这部分观察
