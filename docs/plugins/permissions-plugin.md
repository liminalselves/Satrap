# permissions 插件

`permissions` 独立管理宿主执行的审批模式, 可在不安装 `satrap_coding` 时启用。它只提供用户命令, 不向模型暴露工具或向其他插件导出修改模式的服务。

```text
/approve mode user
/approve mode auto
/approve mode full-access
/approve status
```

Chat 在“对话设置 → 对话 → 执行审批模式”切换, 与命令共用当前会话状态。先在插件管理中启用 permissions。默认 user 由用户批准, auto 由独立模型审查并在不确定或失败时转人工, full-access 跳过操作审批。固定拒绝, 领域权限和受限 Python 沙箱边界继续生效。

平台上的模式修改保留用户原生命令和操作员入口检查, 最终还须获得 `permissions.approval_manage` 管理授权。QQ 群管理员和群主身份不授予该权限, 模型或服务转发不能修改模式。`/plan` 留在 coding, 继续限制文件修改和 Shell。

`base_take.read_document` 和 coding 的 `read_file` 使用同一宿主单文件读取边界。工作区及当前会话上传/沙箱内普通文件免审批; 外部文件的 `file_read_external` 动作按上述三档模式处理, 仍属于只读动作, 计划模式允许。审批绑定真实路径, 文件指纹及本次读取参数, 批准前不读取目标内容, 不创建持续或父目录授权。敏感文件, 保护目录和失效来源始终拒绝; 不扩大目录枚举, 搜索, 写入, worker 或发送范围。

## 配置与状态

| 配置键 | 默认值 | 作用 |
| --- | --- | --- |
| approval_model | 空 | auto 使用的宿主模型配置名; 留空使用当前模型, 审查不携带工具 |
| data_root | 空 | 宿主模式和日志目录; 留空按实际会话目录推导, 改绑后需重启会话 |

默认状态位于 `session_root/cache/permissions`, 未绑定宿主目录时按会话数据库目录或独立会话 ID 隔离。保存文件为 `permissions.json` 和 `approval_log.jsonl`。卸载 coding 或 permissions 不重置模式; coding 卸载只清除其计划模式限制。人工审批回填通过 `session.approval_provider` 传递 `ApprovalRequest -> ApprovalResult`, 与普通提问回调分离。

## 其他插件接入

工具或服务在实际执行作用域内使用工厂注入的 `plugin_host`:

```python
from satrap.edictum import RiskLevel

allowed, reason = await plugin_host.approvals.request_async(
    "file_write", RiskLevel.WRITE, "写入结果文件",
    {"path": str(target), "before": before, "after": after},
)
if not allowed:
    return reason
```

同步会话使用 `request_sync`。审批请求携带真实插件入口名, 当前会话和用户身份, 精确参数及权限版本。调用者负责准备完整参数, 审批后重新核验文件和领域权限, 再产生副作用。接口不会扩大文件访问范围或授予消息发送权限。`snapshot()` 只读当前状态; `set_mode()` 只接受获授权的用户原生命令, 不允许工具或服务提高权限。

## 升级迁移

旧 coding 配置自动拆出 `/approve` 开关与 `approval_model`, 全局配置, 命名配置和会话覆盖均保留来源层级, 新权限插件显式配置优先。已关闭的旧命令保持关闭; 新 coding 配置版本为 1, 后续删除或禁用 permissions 不会自动补回。

旧管理员针对 coding 的允许与否决同步复制到 permissions, 配置文档保存 `approval_plugin_migration: 1` 后不重复推导。旧模式和日志在新目标文件缺失时复制, 原文件保留; 已保存的新模式不被旧文件覆盖。旧无版本模式或宽泛放行规则仍备份并降级为 user, 不迁移一次性批准。
