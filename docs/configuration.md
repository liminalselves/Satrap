# 配置说明

Satrap 的配置分为三层:

- **项目配置**: `config.yaml` / `config.json`, 用于后端, 平台和路径
- **模型配置**: `.satrap/model_config.json`, 由 `satrap model` 管理
- **Session 类配置**: `.satrap/session_class_config.json`, 由 `satrap session` 管理

## 配置文件位置

后端启动时会按顺序查找:

1. `config.yaml`
2. `config.yml`
3. `config.json`
4. `.satrap/config.yaml`
5. `.satrap/config.yml`
6. `.satrap/config.json`
7. `satrap/config.yaml`
8. `satrap/config.yml`
9. `satrap/config.json`

找不到配置时会创建 `.satrap/config.yaml`。

## 生成配置

```bash
satrap config init
satrap config path
satrap config show
```

也可以从项目根目录复制样例:

```bash
cp config.example.yaml config.yaml
```

Windows PowerShell:

```powershell
Copy-Item config.example.yaml config.yaml
```

## 配置字段

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `model_config_path` | `null` | 模型配置 JSON 路径 |
| `session_class_config_path` | `null` | Session 类配置 JSON 路径 |
| `session_db_path` | `null` | Session 实例配置 SQLite 路径 |
| `user_db_path` | `null` | 用户绑定 SQLite 路径 |
| `default_session_type` | `default` | 默认 Session 类型 |
| `max_sessions` | `1000` | 活跃 Session 池最大容量 |
| `idle_timeout` | `3600` | Session 闲置超时秒数 |
| `rate_limit` | `1.0` | 管线限流速率 |
| `rate_burst` | `5` | 管线突发容量 |
| `llm_timeout` | `120.0` | 单次 LLM 调用超时秒数 |
| `error_feedback` | `true` | 出错或限流时是否向用户反馈 |
| `api.host` | `127.0.0.1` | 后端 HTTP API 监听地址 |
| `api.port` | `19870` | 后端 HTTP API 监听端口 |
| `session_classes` | `{}` | 启动时静态注册的 Session 类 |
| `session_scan_paths` | `[".satrap/session"]` | 管理面板和 CLI 扫描 Session 类的目录 |
| `platforms` | `[]` | 平台适配器实例配置 |

## 模型配置

常用命令:

```bash
satrap model list
satrap model show llm default
satrap model set llm default --set api_key=sk-xxx base_url=https://api.example.com/v1 model=your-model
satrap model remove llm default
```

支持的类型:

- `llm`
- `embedding`
- `rerank`

`SessionManager` 创建 Session 时, 会从 Session 参数中的 `model_name` 读取模型配置名称, 默认使用 `default`。

## Session 类配置

注册一个 Session 类:

```bash
satrap session register assistant --class-path my_package.sessions.AssistantSession --description "默认助手"
```

扫描目录:

```bash
satrap session scan --path .satrap/session
```

配置参数:

```bash
satrap session config assistant --set model_name=default system_prompt=你好
satrap session config assistant --show
```

禁用或启用:

```bash
satrap session disable assistant
satrap session enable assistant
```

## 环境变量

`ConfigLoader.merge_env()` 支持这些覆盖项:

| 环境变量 | 覆盖字段 |
| --- | --- |
| `SATRAP_MODEL_CONFIG_PATH` | `model_config_path` |
| `SATRAP_SESSION_CLASS_CONFIG_PATH` | `session_class_config_path` |
| `SATRAP_API_HOST` | `api_host` |
| `SATRAP_API_PORT` | `api_port` |
| `SATRAP_DB_DIR` | `session_db_path` 和 `user_db_path` 所在目录 |
| `SATRAP_LLM_TIMEOUT` | `llm_timeout` |

平台配置中的敏感字段可以写成 `${ENV_NAME}` 形式, 由相关配置编辑流程解析。
