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
satrap config init       # 创建默认配置文件
satrap config path       # 显示当前生效的配置文件路径
satrap config show       # 显示解析后的配置 (api_key 等敏感字段脱敏)
satrap config raw        # 显示原始配置文本 (不脱敏, 注意勿外泄)
```

校验与修改:

```bash
satrap config validate                              # 只校验不落盘, 失败退出码 1
satrap config set --set api.port=19870 data_root=.satrap/data
```

`config set` 会先校验再落盘, 支持 `api.host` 这类点分嵌套键; 后端在线时改动默认走在线链路, 需要直写文件时加 `--offline`。

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
| `data_root` | `.satrap/data` | 按平台实例隔离的运行数据根目录 |
| `session_class_config_path` | `null` | Session 类配置 JSON 路径 |
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
| `workspace_roots` | `["."]` | Chat 项目允许浏览和绑定的工作区根目录 |
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

### LLM 配置字段

`llm` 类型支持以下字段 (`satrap model set llm <name> --set key=value`):

| 字段 | 说明 |
| --- | --- |
| `model` | 模型名称 |
| `base_url` | API base URL |
| `api_key` | API 密钥 |
| `temperature` | 采样温度 |
| `top_p` | top-p 参数 |
| `max_tokens` | 最大输出 token (未配置 `context_window` 时生效) |
| `context_window` | 总上下文窗口, 与 `ContextManager.max_context` 同源 |
| `history_ratio` | 历史上下文比例, 输出预算 = `context_window × (1 - history_ratio)` |
| `allow_insecure_base_url` | 是否允许非回环地址使用明文 HTTP, 默认 `false`; 仅限已知可信的内网兼容服务 |

`base_url` 默认要求 HTTPS; `localhost`、`*.localhost` 与回环 IP 可继续使用 HTTP 进行本地开发。远程明文 HTTP 必须显式设置 `allow_insecure_base_url=true`。

`context_window` 与 `history_ratio` 同时配置时, `SessionManager` 会:

1. 将输出预算 (`context_window × (1 - history_ratio)`) 注入 LLM 实例的 `max_tokens`, 优先级高于 `max_tokens` 字段
2. 将 `context_window` / `history_ratio` 注入 Session 的 `ContextManager`, 驱动滞回截断

这样模型上下文窗口与输出上限在同一份配置中保持一致, 无需分别维护。

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

`class_path` 只允许指向 Satrap 内置模块或 `session_scan_paths` 扫描目录中的模块。管理接口保存冷配置时不会导入类, 实际加载时还会再次核对模块来源和源码路径。

## 技能代码信任边界

技能目录中的 `skill.md` 和 `meta.yaml` 作为数据读取, 但 `tools.py` 属于可执行 Python 代码。默认仅执行 Satrap 官方预设技能中的 `tools.py`; 用户技能目录中的 `tools.py` 会被跳过。仅当应用所有者已审核代码时, 才可通过 `SkillsManager(trusted_code_roots=[...])` 显式加入可信代码根。

## 环境变量

`ConfigLoader.merge_env()` 支持这些覆盖项:

| 环境变量 | 覆盖字段 |
| --- | --- |
| `SATRAP_MODEL_CONFIG_PATH` | `model_config_path` |
| `SATRAP_SESSION_CLASS_CONFIG_PATH` | `session_class_config_path` |
| `SATRAP_API_HOST` | `api_host` |
| `SATRAP_API_PORT` | `api_port` |
| `SATRAP_DATA_ROOT` | `data_root` |
| `SATRAP_LLM_TIMEOUT` | `llm_timeout` |

平台配置中的敏感字段可以写成 `${ENV_NAME}` 形式, 由相关配置编辑流程解析。

管理 HTTP/WS 服务始终启用共享令牌鉴权。回环监听时首次启动会生成 `.satrap/api-token`, CLI 和开发脚本会自动读取该文件。浏览器管理界面由回环客户端和白名单 Origin 引导建立 HttpOnly 会话, Cookie 使用服务端保存且可撤销的随机会话 ID, 不包含 API token, 默认 8 小时过期。该引导流程信任本机进程与白名单前端; 如需关闭无 Bearer token 的回环引导, 设置 `SATRAP_LOOPBACK_BOOTSTRAP=0`。绑定 `0.0.0.0`、局域网地址或其他非回环地址时必须显式设置至少 32 个字符的 `SATRAP_API_TOKEN`, 否则服务拒绝启动。

跨域浏览器访问使用精确 Origin 白名单。默认仅允许 Satrap 的本地服务端口和 Vite 开发端口, 额外来源通过逗号分隔的 `SATRAP_ALLOWED_ORIGINS` 配置, 例如 `https://admin.example.com`。不要把 `.satrap/api-token` 提交到版本库或写入前端构建变量。

内置 HTTP 服务默认限制 256 个并发连接和 64 个 WebSocket 连接。WebSocket 每 30 秒发送 ping, 连续 5 分钟未收到客户端帧时主动关闭; 客户端单帧上限为 64 KiB。前端静态产物可在鉴权前访问以支持登录引导, 因此构建目录仅应包含公开文件; 服务会拒绝隐藏文件和 source map。
