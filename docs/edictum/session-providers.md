# 会话 Provider 与 Edictum 冷配置

## 当前边界

`SessionManager` 只负责实例配置持久化, 会话池和运行时调度, 不再直接判断具体会话实现。命名会话定义和实例创建由 Provider 负责。

- `SessionProvider`: 统一的定义查询与实例创建契约
- `SessionProviderRegistry`: 注册 Provider, 按 `provider_name + definition_name` 分发, 未指定 Provider 时只接受全局唯一名称
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

控制服务 API 可在平台后端未启动时使用:

- `GET /config/edictum/types`
- `GET|POST /config/edictum/sessions`
- `GET|PATCH|DELETE /config/edictum/sessions/{name}`
- `POST /config/edictum/sessions/{name}/enable`
- `POST /config/edictum/sessions/{name}/disable`

平台后端提供对应的 `/api/config/edictum/...` 路由。

当前阶段完成的是通用 Provider 边界, SessionClassProvider 分发, Edictum 类型注册表和冷配置 CRUD。运行时 `EdictumProvider`, 插件装载和前端编辑界面属于下一阶段。
