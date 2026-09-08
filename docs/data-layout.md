# 运行数据布局

Satrap 的运行数据统一放在 `.satrap/data`。平台适配器实例是最高隔离边界, 适配器类型不是隔离边界。例如 `onebot-main` 和 `onebot-backup` 即使都使用 OneBot, 也拥有互不相干的数据目录和数据库。

```text
.satrap/data/
├── rag/
│   ├── indexes/            # 全局 (scope=global) RAG 知识库索引, 按库 ID 分目录
│   └── locks/              # 知识库重建 / 写入文件锁
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

恢复回收包为凭据式两阶段: 先把 `records.json` 的数据库行插回并写 `archive_restores` 恢复凭据, 再将 `files/` 原子发布回原会话目录, 最后标记凭据完成并清理回收包; 恢复前严格校验回收包身份, 摘要与布局版本, 目标会话目录已存在则拒绝。永久删除回收包 (`purge`) 由独立管理操作完成, 不可恢复; Chat 回收站见 [聊天展示层](chat-display.md)。

## 路径键

外部 ID 不直接作为目录名。目录键由可读片段和原始 ID 的 SHA-256 短哈希组成, 因此 `a:b` 与 `a_b` 不会映射到同一个目录。真实 ID 保存在 `platform.json` 或 `meta.json` 中。
