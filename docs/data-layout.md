# 运行数据布局

Satrap 的运行数据统一放在 `.satrap/data`。平台适配器实例是最高隔离边界, 适配器类型不是隔离边界。例如 `onebot-main` 和 `onebot-backup` 即使都使用 OneBot, 也拥有互不相干的数据目录和数据库。

```text
.satrap/data/
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
        │       ├── indexes/
        │       └── cache/
        ├── projects/
        └── trash/
            └── sessions/
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

删除会话按以下顺序执行:

1. 停止运行任务并释放插件状态
2. 将会话目录整体移动到同平台的 `trash/sessions`
3. 删除 `chat_history`, 状态检查点和 `session:<session_id>` 长期记忆
4. 删除会话配置, 用户绑定和上下文路由

回收区与原会话位于同一平台目录, 移动操作不会跨平台。永久清理回收区应由独立管理操作完成。

## 路径键

外部 ID 不直接作为目录名。目录键由可读片段和原始 ID 的 SHA-256 短哈希组成, 因此 `a:b` 与 `a_b` 不会映射到同一个目录。真实 ID 保存在 `platform.json` 或 `meta.json` 中。
