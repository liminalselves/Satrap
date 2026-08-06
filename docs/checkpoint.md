# 检查点 (Checkpoint)

Satrap 的状态检查点体系提供**消息上下文的快照、回滚、重试与分支**能力。它在 `chat_history` 消息库之上, 用同一 SQLite 文件内的两张表 (`state_checkpoints` / `state_snapshots`) 记录状态, 不引入独立存储。

## 核心概念

| 概念 | 说明 |
|---|---|
| 检查点 | 消息水位 (position) + 状态版本 (state_revision) 的引用, 可回滚恢复 |
| 指针式检查点 | stable / manual 检查点默认只写一行元数据, 不物化消息快照 (零空间成本) |
| 保护快照 | 回滚 / 重试 / 编辑消息前的检查点会物化完整 JSON 快照, 保证可恢复 |
| 批次 (batch) | 会话级聚合检查点 (Session.create_checkpoint) 同批共享 batch_id, 一次回滚整个会话 |
| 分支 (fork) | 从任意检查点派生新对话线, 新 ID 形如 `{conversation}:fork:{branch}` |
| 血缘 (lineage) | 通过 parent_checkpoint_id 链追踪分支来源 |

检查点类型 (checkpoint_kind):

- `stable`: 每次写入消息后自动保存 (auto_checkpoint, 同水位去重, 指针式)
- `manual`: 手动创建 (ContextManager.create_checkpoint / CLI / 管理面板)
- `protect`: 编辑 / 回滚前的保护快照, 自动创建并物化

## 编程 API

ContextManager (同步) 与 AsyncContextManager (异步) 均支持:

| 方法 | 说明 |
|---|---|
| `create_checkpoint(name=, description=, checkpoint_kind=)` | 创建检查点 |
| `list_checkpoints()` | 列出当前对话全部检查点 (新在前) |
| `rollback(checkpoint_id)` | 回滚到检查点, 删除未来检查点 |
| `retry(checkpoint_id)` | 回到检查点但保留未来检查点 (物化保护) |
| `fork(branch_name, checkpoint_id=None)` | 从检查点分支新对话, 返回新 ContextManager |
| `delete_checkpoint(checkpoint_id)` | 删除单个检查点 |

启用方式: 构造时传 `enable_checkpoint=True`, 或显式传 `state_store=`; `auto_checkpoint=False` 可关闭自动 stable 保存。

```python
from satrap.core.utils.context import ContextManager

ctx = ContextManager("conv-1", enable_checkpoint=True)
ctx.add_user_message("你好")
cp = ctx.create_checkpoint(name="开场")
ctx.add_user_message("继续")
ctx.rollback(cp.checkpoint_id)          # 消息恢复到场检查点
fork = ctx.fork("试另一条线")            # 派生分支
print(fork.conversation_id)              # conv-1:fork:试另一条线
```

会话级聚合 (整个 Session 的 session_ctx + wf_ctx 一起):

```python
session.create_checkpoint(name="剧情点")   # 批检查点
session.rollback(checkpoint_id)           # 双上下文一并回滚
session.fork(branch_name="alternate")     # 双上下文一并分支
```

## CLI

```bash
satrap checkpoint create   --conversation conv-1 --name 开场
satrap checkpoint list     --conversation conv-1
satrap checkpoint rollback --conversation conv-1 --checkpoint-id manual-xxxx
satrap checkpoint retry    --conversation conv-1 --checkpoint-id manual-xxxx
satrap checkpoint fork     --conversation conv-1 --branch-name v2
satrap checkpoint lineage  --checkpoint-id manual-xxxx
satrap checkpoint branches --conversation conv-1
satrap checkpoint audit    --conversation conv-1
```

默认操作 `.satrap/chat_history.db`, 可用 `--db` 覆盖。CLI 与运行时 Session 解耦, 直接读写上下文库。

## HTTP API

后端内置 HTTP 服务器 (127.0.0.1:19870) 暴露检查点管理端点, 库路径取 `session_checkpoint_db > session_db_path > .satrap/chat_history.db`:

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/checkpoints?conversation=conv-1` | 检查点 + 全部分支 |
| GET | `/api/checkpoint/branches?conversation=conv-1` | 分支列表 |
| GET | `/api/checkpoint/lineage?checkpoint_id=xxx` | 血缘链 |
| GET | `/api/checkpoint/audit?conversation=conv-1` | 变更记录 |
| POST | `/api/checkpoint/create` | body: `{conversation, name?, description?}` |
| POST | `/api/checkpoint/rollback` | body: `{conversation, checkpoint_id}` |
| POST | `/api/checkpoint/retry` | body: `{conversation, checkpoint_id}` |
| POST | `/api/checkpoint/fork` | body: `{conversation, branch_name, checkpoint_id?}` |

示例:

```bash
curl -X POST http://127.0.0.1:19870/api/checkpoint/create \
  -H "Content-Type: application/json" \
  -d '{"conversation": "conv-1", "name": "开场"}'
```

## Web 管理面板

Streamlit 面板的 **检查点管理** 页面 (07_checkpoint_management.py) 提供图形化操作: 输入对话 ID 后查看检查点 / 分支 / 变更记录, 支持创建、回滚、重试与 Fork。

## 存储与性能

- 所有数据在同一 SQLite 文件中: 消息 (chat_history) + 检查点 (state_checkpoints) + 快照 (state_snapshots)
- 消息写入为**增量追加** (只 INSERT 尾部新消息), 行 id 稳定, 指针检查点水位 = 行数 (COUNT)
- 检查点查询索引: `idx_state_ckpt_scope` (作用域), `idx_state_ckpt_scope_time` (作用域 + 时间排序), `idx_state_ckpt_parent` (血缘), `idx_state_ckpt_batch` (批次), `idx_state_ckpt_scope_id` (分支前缀范围)
- 长会话的 stable 检查点行会持续累积 (每写入一行 ~300 字节), 对查询影响很小; 需要瘦身时用 `delete_checkpoint` 或回滚清理

### 性能基线 (2026-08-06)

基准脚本: `tests/benchmark/benchmark_checkpoint.py` (Python 3.13, 消息体 100 汉字, 档位 50/100/300 轮)

| 场景 | 总耗时 | 写入 ms/op | 聚合 ms/op | DB 大小 | 检查点行 | 快照行 | 消息行 | 内存增量 |
|---|---|---|---|---|---|---|---|---|
| A 写入基线 (无检查点, 200 条) | 806.1 ms | 4.03 | - | 92.0 KB | 0 | 0 | 200 | +1.1 MB |
| B 自动 stable (指针式, 200 条) | 2006.3 ms | 10.03 | - | 188.0 KB | 200 | 0 | 200 | +0.4 MB |
| C 会话聚合 (50 轮, 800 条) | 6237.8 ms | 5.94 | 136.86 | 640.0 KB | 34 | 10 | 576 | +4.4 MB |
| C 会话聚合 (100 轮, 1600 条) | 10940.3 ms | 6.42 | 24.58 | 960.0 KB | 64 | 10 | 1376 | -1.3 MB |
| C 会话聚合 (300 轮, 4800 条) | 38657.6 ms | 7.31 | 57.37 | 2264.0 KB | 184 | 10 | 4576 | +4.4 MB |

要点:

- 开启自动 stable 后写入开销约为基线 2.5 倍 (10.03 vs 4.03 ms/op), DB 增长 x2.04, 但零物化 (快照 0 行), 换取任意水位回滚能力
- 会话聚合场景下 fork / retry / rollback 单次操作均 < 50 ms; 聚合检查点随消息量线性增长 (写入 ~6-7 ms/op)
- 首次聚合检查点 (50 轮档) 含冷启动开销 (建表 / 索引), 后续档位回落至 25-57 ms/op
- 300 轮会话 DB 约 2.2 MB, 其中检查点指针行 184 行 (每行 ~300 字节), 占比可忽略

## 注意事项

- 检查点水位基于消息行数, 增量保存保证行 id 稳定; 编辑消息 (如 reset_system_prompt) 会触发全量重写, 此时已有检查点指向的历史行 id 可能漂移, 系统通过编辑前保护快照保证可恢复
- `fork` 与 `rollback` 会清理未来检查点 (保护检查点除外), 操作前请确认
- 多进程 / 多实例同时写同一上下文库不受支持 (SQLite 锁与水位语义冲突)
