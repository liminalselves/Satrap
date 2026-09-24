# 运行数据布局

Satrap 的运行数据统一放在 `.satrap/data`。平台适配器实例是最高隔离边界, 适配器类型不是隔离边界。例如 `onebot-main` 和 `onebot-backup` 即使都使用 OneBot, 也拥有互不相干的数据目录和数据库。

```text
.satrap/data/
├── rag/
│   ├── indexes/            # 全局 (scope=global) RAG 知识库索引, 按库 ID 分目录
│   └── locks/              # 知识库重建 / 写入文件锁
├── manual_wake_store.json  # 手动唤醒请求与发送尝试记录 (清单 + 归档 + 锁文件见下)
├── request_ledger.json     # request 审批身份账本
└── platforms/
    └── <可读平台名>--<稳定哈希>/
        ├── platform.json
        ├── platform.db
        ├── cache/
        ├── users/
        │   └── <可读用户 ID>--<稳定哈希>/
        │       └── meta.json
        ├── sessions/
        │   └── <可读会话 ID>--<稳定哈希>/
        │       ├── meta.json
        │       ├── sandbox/
        │       ├── uploads/
        │       ├── artifacts/
        │       ├── indexes/      # 会话级 RAG 库在 indexes/rag/<库 ID>/ 下
        │       └── cache/
        ├── projects/
        └── trash/
            └── sessions/
                └── <archive_id>/     # 单个会话回收包: 时间戳+纳秒+短哈希
                    ├── files/        # 原会话目录整体移入 (sandbox/uploads/artifacts/indexes/cache)
                    ├── records.json  # 会话域数据库记录快照 (13 张表, 带 records_sha256 摘要)
                    └── manifest.json # 回收包清单 (layout_version / archive_version=2 / session_id / deleted_at)
```

## 后端状态文件

需要跨重启保留的后端状态以 JSON 文件放在数据根目录, 写入一律是同目录临时文件 + `os.replace` 的原子替换, 并在 `.` 前缀的锁文件内完成"读取-判定-写入"全过程 (锁文件保留不删除, 避免 POSIX 下双持有):

- `manual_wake_store.json`: 手动唤醒请求与发送尝试记录。伴随 `manual_wake_store.manifest.json` 记录 `expected_files` (主文件 present/missing, 归档 absent/present/missing) 与 `degraded`; 容量轮转写归档 `manual_wake_store.json.1`
- `request_ledger.json`: request 审批身份账本 (平台实例 + 已绑定账号 + 类别 + flag 摘要), 伴随 `request_ledger.manifest.json`

两类文件共用同一套损坏处理: 清单缺失但主文件已存在时按旧数据迁移并补写清单, 不当作首次初始化; 文件损坏或身份/状态不一致时先在清单里持久记录 `degraded` 原因, 标记落盘成功后才把坏文件隔离为 `<名>.corrupt-<YYYYmmdd-HHMMSS>[-序号]` (原名保留原始字节供人工核对); 标记落盘失败则保留原文件不隔离。降级状态跨重启保持, 业务侧在降级期间拒绝登记与审批, 只有显式恢复接口在校验通过后解除降级, 恢复过程不清空既有记录。

这份共用能力实现在 `satrap/core/storage/durability.py`: 清单的结构与版本校验入口, "先持久降级标记, 后隔离文件"的顺序原语与 `corrupt-*` 扫描。各存储的差异按声明传入 —— `expected_files` 的必需键与允许状态 (存储: main present/missing 与 archive absent/present/missing; 账本: entries 恒为 present), 额外键语义 (存储写入时归一化丢弃; 账本按原样保留, 顶层, `expected_files` 与 `degraded` 的额外键都在原载荷上只覆盖声明字段, 不解释的 `at` 连取值类型一起写回, 只有主动写入新降级标记时才按声明字段重建该标记), 降级原因的严格程度 (存储要求非空, 账本只要求字符串且不解释 `at`), 以及事务上下文 (两个 `FileLock` 的持有方式不变)。组件边界止于清单原语: 启动决策树, 记录解析, 容量与保留期, 归档轮转与 `recover()` 的校验结论都留在各自业务侧。

## 平台数据库

每个平台实例只有一个 `platform.db`。会话配置, 用户绑定, 上下文消息, 检查点, 长期记忆以及平台相关的展示数据使用不同表, 但不能配置为不同数据库文件。

`cache/` 只保存无法在创建时绑定到具体会话的平台级临时文件。已知会话的上传、工具产物和缓存必须写入会话目录。

保留平台实例:

- `chat`: React Chat 服务
- `local`: CLI, TUI 和没有绑定外部适配器的会话

配置项 `data_root` 可以修改整个运行数据根目录。`session_db_path`, `user_db_path` 和 `session_checkpoint_db` 已移除, 配置中出现这些字段会直接报错。环境变量使用 `SATRAP_DATA_ROOT`, 不再支持 `SATRAP_DB_DIR`。

## 会话隔离

每个会话固定拥有自己的 `sandbox`, `uploads`, `artifacts`, `indexes` 和 `cache`。插件只能通过运行时注入的会话路径访问这些目录, 不能用插件配置把存储切回全局目录。

Chat 项目绑定只共享用户选择的外部工作区。项目会话的沙箱, 上传, 索引, 缓存和长期记忆仍然属于当前会话, 不会写入 `<项目>/.satrap`。

长期记忆默认使用 `session:<session_id>` 作用域。需要项目级或平台级共享时应增加显式的数据领域和授权, 不能通过复用目录隐式共享。

## 删除生命周期

删除会话 = 归档为可恢复回收包 (软删除), 在会话级文件锁下按以下顺序执行:

1. 确认会话不在运行: 正在生成的会话需显式强制取消后才可回收; 同步删除路径对非空闲会话跳过删除
2. 释放运行时与插件状态
3. `snapshot_session_domain` 导出会话域 13 张表记录 (会话配置 / 覆盖 / 会话级 RAG 库 / 展示轮次与版本 / 工具调用 / chat_history / 状态三件套 / `session:<session_id>` 记忆 / 上下文路由) 写入回收包 `records.json`
4. 会话目录整体移动到回收包 `files/` (同平台内移动, 不跨平台)
5. 删除平台库中的会话域记录行, 写入带 `records_sha256` 摘要的 `manifest.json`
6. 任一步骤失败回滚: 文件移回原位, 数据库记录恢复, 删除半成品回收包

恢复回收包为凭据式两阶段: 先把 `records.json` 的数据库行插回并写 `archive_restores` 恢复凭据, 再将 `files/` 原子发布回原会话目录, 最后标记凭据完成并清理回收包; 恢复前严格校验回收包身份, 摘要与布局版本, 目标会话目录已存在则拒绝。永久删除回收包 (`purge`) 由独立管理操作完成, 不可恢复; Chat 回收站见 [聊天展示层](../ui/chat-display.md)。

## 路径键

外部 ID 不直接作为目录名。目录键由可读片段和原始 ID 的 SHA-256 短哈希组成, 因此 `a:b` 与 `a_b` 不会映射到同一个目录。真实 ID 保存在 `platform.json` 或 `meta.json` 中。
