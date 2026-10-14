# 本轮请求资料与一次性历史清洗

平台机器人身份, 群聊环境资料和管理者私聊的群查询说明使用 `RequestContextBlock` 列表传递. 名称与生产者联合唯一, 内容只作为数据附到当前轮用户消息的请求副本中, 不写入 `chat_history`, 不参与历史摘要. 发送者标记, 消息 ID, 引用及媒体仍保存在历史中. memory/goal 本次保留原有行为

共享数据结构定义在 `satrap/core/type.py`, 校验, 快照序列化与请求副本拼接位于 `satrap/core/utils/context/request.py`. 插件继续从 `satrap.edictum` 导入公开类型, 共享类型模块不依赖上下文管理器

每个 Agent 执行固定一份资料快照. 工具循环的每次模型请求都包含该快照一次, 恢复任务使用执行 payload 中的原快照. 执行记录可以保存请求资料用于恢复和审计; 它们与对话历史是不同的数据. 资料不替代来源核验, 路由检查或工具授权

群聊正文的发言人和消息 ID 标记优先保留, 不再依赖完整环境 JSON 补足历史身份. 字符额度不足时先省略昵称; 连账号和消息 ID 都无法容纳时明确拒绝输入, 不静默保存缺失身份的消息

资料占用请求 token 预算. 滑动窗口和中间截取只裁剪可删的旧历史, 保留本轮消息与资料. 总结只读取保存的历史; 若当前消息, 资料和固定内容仍超过预算, 明确报超限, 不静默丢弃当前身份或媒体

## 一次性脚本

`scripts/clean_request_context.py` 独立运行, 应用启动, 模型请求和恢复入口均不调用它. 仅支持经源码核对的旧格式, 包括早期 `available_tools` 字段以及后来的 `available_group_chat_tools` 字段. 识别开头的已知资料块序列, 保留持续目标与两种记忆格式, 剥离环境资料和平台身份行. 遇到未知字段, 边界不明确或冲突账号时保留原文并报告

旧记录没有生产者标记, 用户自行粘贴完全相同的开头仍可能成为候选. 执行前须核对预览范围. 报告只记录位置和计数, 不输出消息正文

扫描范围:

- `chat_history`, 同步更新 `content` 和多模态 `content_json`
- `state_snapshots` 的 `domains.messages`
- `conversation_data_backups` 的上下文备份及历史备份内的版本增量
- `display_turn_variants.context_messages`
- 数据库同目录 `trash/sessions` 内的归档包, 额外范围用 `--archive-root` 指定

归档内容清洗后同步更新版本 2 清单的 SHA-256. 受影响的摘要缓存失效, 下次需要时由应用重建. 不改变消息数量, 行 ID, 媒体, 工具结果或检查点水位. 未完成任务按实际上下文 ID 检查 `agent_runs.scope`, 除 `completed` 和 `cancelled` 外均阻止应用; 不修改旧指纹强行恢复

## 执行步骤

PowerShell 会话先固定 UTF-8:

```powershell
$taskUtf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $taskUtf8
[Console]::OutputEncoding = $taskUtf8
$OutputEncoding = $taskUtf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
```

先预览, `--database` 可以重复指定:

```powershell
python scripts/clean_request_context.py --database '绝对路径/platform.db' --report '绝对路径/preview.json'
```

停止所有持有这些数据库或其内存上下文的后端进程, 包括后台 Agent 与聊天服务. 仅暂停收消息或取得会话维护锁不足以防止旧内存全量写回. 脚本会检查可识别的 Satrap 后端进程, 仍要求操作员确保没有其他写入者

确认停服后执行:

```powershell
python scripts/clean_request_context.py --database '绝对路径/platform.db' --apply --offline-confirmed --backup-dir '应用数据范围之外的绝对路径' --report '绝对路径/result.json'
```

有活跃进程, 未完成任务, 未知格式或无法读取的归档时整批阻止写入, 返回退出码 2. 预览默认只读. 应用先备份数据库与待修改归档, 原位事务更新数据库, 原子替换归档文件; 报告保存在备份目录. 出现跨文件故障时按报告从外部备份人工回退, 不把旧备份放回应用自动恢复目录

执行后再次预览, 确认可清洗行数为零, 再恢复服务. 重复执行不会新增内容修改. 清洗脚本不会成为定时任务或后续请求兼容层
