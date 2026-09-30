# 会话 Provider 与 Edictum 冷配置

## 当前边界

`SessionManager` 只负责实例配置持久化, 会话池和运行时调度, 不再直接判断具体会话实现。命名会话定义和实例创建由 Provider 负责。

- `SessionProvider`: 统一的定义查询与实例创建契约
- `SessionProviderRegistry`: 注册 Provider, 按 `provider_name + definition_name` 分发, 未指定 Provider 时只接受全局唯一名称; `binding_status()` 把绑定的可执行状态分为 `RUNNABLE` / `DISABLED` / `INVALID`
- `SessionClassProvider`: 兼容原有扫描式 `Session` / `AsyncSession` 类, 扫描仍由 `SessionClassConfigManager` 负责
- `SessionConfig.provider_name`: 持久化创建该会话所需的 Provider, 旧数据库自动迁移为 `session_class`

## Edictum 类型扩展

`EdictumTypeRegistry` 管理稳定的类型名称和运行时工厂。内置类型为:

- `simple`: `SimpleSession`
- `async_simple`: `AsyncSimpleSession`

新 Edictum 会话实现只需注册一个 `EdictumTypeDefinition`, 冷配置管理和后续 Provider 分发不需要新增类型分支。外部扩展也可以在启动前将自定义注册表传给 `BackendManager`。

## Edictum 冷配置

冷配置默认存储于 `.satrap/edictum_session_config.json`, 可通过 `edictum_config_path` 或 `SATRAP_EDICTUM_CONFIG_PATH` 修改。文件首次创建时只包含空对象 `{}`, 不生成数字名称或空白占位配置。

每个命名配置包含:

- `provider`: 固定为 `edictum`
- `edictum_type`: 类型注册表中的名称
- `enabled`: 是否启用
- `description`: 配置说明
- `model_name`: 模型配置名称
- `params`: Edictum 类型构造参数
- `plugins`: 插件名称或插件配置对象列表

## `enabled` 的完整效果

`enabled` 只约束会话执行, 不约束平台连接, 两者是独立开关:

| 场景 | 结果 |
| --- | --- |
| 平台 `enable: false` | 平台不连接, 不解析绑定, 配置照常保存与应用 |
| 平台 `enable: true`, 定义 `enabled: true` | 正常连接与执行 |
| 平台 `enable: true`, 定义 `enabled: false` | 平台照常连接与启动 (notice, 群管理, 群目录与其他绑定不受影响), 该绑定的消息在唤醒与窗口之前被静默丢弃, 不建会话也不调模型 |
| 平台 `enable: true`, 定义不存在或 `session_provider` 不存在 | 平台应用失败并保留旧实例, 不回退到默认会话类 |

判定由 `SessionProviderRegistry.binding_status()` 统一给出, 分 `RUNNABLE` / `DISABLED` / `INVALID` 三种并保留原始原因文本; 平台生命周期只对 `INVALID` 拒绝应用, 运行时闸门对 `DISABLED` 与 `INVALID` 都拒绝该条消息。判定逐事件读取, 重新启用定义后无需重建平台即恢复。被拒消息的诊断原因码为 `binding_disabled` / `binding_invalid`。

`enabled: false` 时平台设置页的“会话绑定可用”显示为否, 但平台本身仍可保持启用, 因此计划停用某个命名配置前不需要先停用引用它的平台。

控制服务 API 可在平台后端未启动时使用:

- `GET /config/edictum/types`
- `GET|POST /config/edictum/sessions`
- `GET|PATCH|DELETE /config/edictum/sessions/{name}`
- `POST /config/edictum/sessions/{name}/enable`
- `POST /config/edictum/sessions/{name}/disable`

平台后端提供对应的 `/api/config/edictum/...` 路由。

当前阶段完成的是通用 Provider 边界, SessionClassProvider 分发, Edictum 类型注册表和冷配置 CRUD。运行时 `EdictumProvider`, 插件装载和前端编辑界面属于下一阶段。
