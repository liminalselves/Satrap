# 配置说明

Satrap 的配置分为三层:

- **项目配置**: `config.yaml` / `config.json`, 用于后端, 平台和路径
- **模型配置**: `.satrap/config/model_config.json`, 由 `satrap model` 管理
- **Session 类配置**: `.satrap/config/session_class_config.json`, 由 `satrap session` 管理

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

JSON 配置统一保存在 `.satrap/config/`:

```text
.satrap/
├── config.yaml
├── config/
│   ├── model_config.json
│   ├── session_class_config.json
│   ├── edictum_session_config.json
│   ├── chat_plugins.json
│   ├── logging.json
│   └── plugins/                    # 各插件的全局 JSON 配置
├── background-services/
│   ├── stdout.log
│   └── stderr.log
├── credentials/
│   └── api-token                   # 本地管理接口共享凭据
├── runtime/                       # PID, 后端及发行启动器的实例锁
├── logs/                          # 按日期和进程划分的应用日志
└── data/                          # 对话, 用户及运行状态
```

首次使用默认路径时会将旧版根目录下对应 JSON 和 `plugin_config/` 自动迁移到新位置, 内容保持不变。新位置已有配置时优先使用新配置, 不覆盖任一份文件; 迁移失败时记录错误并继续使用旧配置, 避免生成空配置。升级前应先停止旧版本进程, 避免它们继续写入旧位置。

`SATRAP_CONFIG_ROOT` 可统一指定 JSON 配置目录, 设置后不迁移项目旧配置。单独指定的 `SATRAP_MODEL_CONFIG_PATH`、`SATRAP_SESSION_CLASS_CONFIG_PATH`、`SATRAP_EDICTUM_CONFIG_PATH`、`SATRAP_LOG_CONFIG` 或管理器显式路径继续优先。`config.yaml`、运行账本、对话备份和编码插件状态保留各自位置。

`scripts/start-background-services.ps1 -Detach` 的启动输出写入 `background-services/stdout.log` 和 `stderr.log`, 与应用日志分开保存。

`api-token` 保存到 `.satrap/credentials/`, PID 和后端实例锁保存到 `.satrap/runtime/`; 发行启动器的实例记录, 停止标记与锁也使用 `runtime/`。旧版默认文件首次使用时自动迁移, 令牌内容不变; 仍被持有的旧实例锁会继续使用旧路径, 避免绕过单实例保护。升级前应停止旧版本服务。`SATRAP_CREDENTIALS_ROOT` 和 `SATRAP_RUNTIME_ROOT` 可分别覆盖这两个目录。配置写入锁与数据写入锁仍随对应配置或数据库保存。

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
| `session_scan_paths` | `[".satrap/session"]` | 管理面板和 CLI 扫描 Session 类的目录; 目录下的 .py 会被导入执行, 属于可信代码目录, 不要允许不可信来源写入 |
| `workspace_roots` | `["."]` | Chat 项目允许浏览和绑定的工作区根目录 |
| `media_allowed_roots` | `null` | 本地媒体允许目录, 仅接受字符串列表或 null; 规则见下文 |
| `platforms` | `[]` | 平台适配器实例配置 |

平台实例 `settings` 内的策略字段 (唤醒规则, 窗口与输入预算, 媒体下载, 命令入口等) 由 `satrap/core/config/platform_policy.py` 的 `POLICY_FIELD_CONTRACT` 声明并校验, 逐字段口径与默认值见[平台接入](../platform/platforms.md)。其中 `command_operators` 是 `/approve` 与 `/plan` 的操作员名单: 缺失, 为空或取值非法时这两条命令一律拒绝 (fail-closed), 升级后需先在平台设置中登记操作员, 否则群内与私聊都会收到固定拒绝文案。

平台实例的 `session_type` 与 `session_provider` 在保存阶段不做存在性校验, 可以先保存一个暂时不可用的绑定。可用性只在平台启用时强制: `enable: false` 的平台允许绑定缺失或失效, 重载成功且实例保持惰性; `enable: true` 时只有定义或 Provider 不存在才拒绝应用 (保留原实例与原生效版本, 不回退到 `default_session_type`), 而绑定的会话定义被禁用时平台照常连接与启动, 该绑定的消息在进入唤醒与窗口之前被丢弃, 重新启用定义后无需重建平台即恢复。详见[平台接入](../platform/platforms.md)的配置保存与生效一节与[会话 Provider 与 Edictum 冷配置](../edictum/session-providers.md)的 `enabled` 完整效果。

### 媒体来源白名单

`media_allowed_roots` 为 `null` 或空列表时允许 `.satrap/sandbox`, 以及实际 `data_root` 下的以下目录及其子目录:

- `platforms/<平台键>/cache`
- `platforms/<平台键>/sessions/<会话键>/{uploads,artifacts,sandbox,cache}`
- `group-chat/stickers/<64位小写十六进制内容摘要>`: 共享表情库登记的图片文件, 不包含相邻的 catalog.db 或任意文件名

不按任意祖先目录名放行, 因此数据根中的数据库、用户、项目、索引和回收站不属于默认媒体目录。系统临时目录 (`%TEMP%` / `/tmp`) 不默认授权, `data_root` 位于其中时仍只允许上述媒体子目录。需要发送其他临时文件时, 显式授权其所在目录。

非空列表完全替换上述默认授权。`SATRAP_EXTRA_MEDIA_ROOTS` 始终追加目录, 用当前系统的路径分隔符分隔 (Windows 为 `;`, Unix 为 `:`); 配置列表中的路径不按此分隔符拆分, 允许 Windows 目录名含 `;`。路径在校验时解析符号链接和 `..`。

下载和内联媒体的临时文件位于实际 `data_root` 的 `platforms/<local平台键>/cache/temp`。使用显式白名单且需要下载、Base64 转换或文件回调时, 必须将该缓存目录或其父目录列入白名单; 未授权时拒绝落盘, 不额外扩大权限。HTTP(S) 来源可直接交给平台处理。

本地来源支持裸路径和标准 `file:` URI, URI 中的 `#`、`%` 和中文文件名须转义 (Python 可用 `Path.as_uri()`)。所有本地路径先校验授权, 不以文件是否存在决定是否检查; Windows 支持 UNC authority, Unix 拒绝非本机 authority。此白名单约束的是 Satrap 允许外发的路径, 不证明独立部署的 OneBot 服务能访问同一文件系统。

### 群管理插件升级

group_admin 开启 `write_tools_enabled` 后还必须在 `allowed_callers` 中显式配置可发起模型写操作的成员 ID. 留空拒绝全部模型管理写操作, 不从群管理员或其他配置自动补名单. `allowed_read_callers` 独立限制本插件现有查询工具, 留空不额外限制, 不影响 group_chat 或 friend_manager 的权限

`high_risk_approval` 开启后, 踢人, 禁言, 设置管理员, 修改群名, 退群及处理加群请求等高危模型动作必须进入群管理页面的持久审批队列. 宿主账号/群策略要求审批时, 关闭此项也不能取消审批. pending 仅代表申请已保存, succeeded 才表示已执行; 不增加另一套即时询问流程. 已保存配置不会被自动改写, 升级前允许空写名单的配置需由管理员明确补充授权成员

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
- `asr` (OpenAI 兼容语音转录, 见下文)

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

文件日志的保留策略在「设置 → 日志保留」独立管理, 默认自动保留 30 个自然日。配置位于 `.satrap/config/logging.json`, 修改无需重启后端, 详见 [日志保留](logging.md)。`SATRAP_LOG_ROOT` 和 `SATRAP_LOG_CONFIG` 分别覆盖日志目录与策略文件路径。

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

管理 HTTP/WS 服务始终启用共享令牌鉴权。回环监听时首次启动会生成 `.satrap/credentials/api-token`, CLI 和开发脚本会自动读取该文件。浏览器管理界面由回环客户端和白名单 Origin 引导建立 HttpOnly 会话, Cookie 使用服务端保存且可撤销的随机会话 ID, 不包含 API token, 默认 8 小时过期。该引导流程信任本机进程与白名单前端; 如需关闭无 Bearer token 的回环引导, 设置 `SATRAP_LOOPBACK_BOOTSTRAP=0`。绑定 `0.0.0.0`、局域网地址或其他非回环地址时必须显式设置至少 32 个字符的 `SATRAP_API_TOKEN`, 否则服务拒绝启动。

跨域浏览器访问使用精确 Origin 白名单。默认仅允许 Satrap 的本地服务端口和 Vite 开发端口, 额外来源通过逗号分隔的 `SATRAP_ALLOWED_ORIGINS` 配置, 例如 `https://admin.example.com`。不要把 `.satrap/credentials/api-token` 提交到版本库或写入前端构建变量。

内置 HTTP 服务默认限制 256 个并发连接和 64 个 WebSocket 连接。WebSocket 每 30 秒发送 ping, 连续 5 分钟未收到客户端帧时主动关闭; 客户端单帧上限为 64 KiB。前端静态产物可在鉴权前访问以支持登录引导, 因此构建目录仅应包含公开文件; 服务会拒绝隐藏文件和 source map。

### ASR 配置字段

`asr` 类型使用 OpenAI 兼容的 `/audio/transcriptions` 协议, 本地与云端服务共用同一套字段, 不针对特定本地推理框架做适配:

| 字段 | 说明 |
| --- | --- |
| `model` | 转录模型名称, 不限定为 OpenAI 官方模型 |
| `base_url` | 服务地址, 非回环 HTTP 地址需要显式 `allow_insecure_base_url=true` |
| `api_key` | 密钥, 只在后端读取, 控制面板与 CLI 默认脱敏展示 |
| `language` | 可选默认语言, 留空由服务端检测 |
| `prompt` | 可选提示词, 按服务端能力透传 |
| `timeout` | 请求超时秒数, 默认 60 |

```bash
satrap model set asr default --set api_key=sk-xxx base_url=http://127.0.0.1:9000/v1 model=whisper-1 language=zh
```

控制面板「模型配置 → ASR 配置」提供同样的增删改查, 卡片上的「测试转录」上传一段不超过 8 MiB 的短音频, 由后端调用 `POST /config/models/asr/<name>/test` 完成一次真实转录并返回文本、语言、音频时长与耗时; 浏览器不会直接访问转录服务商。
