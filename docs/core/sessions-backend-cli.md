# Session, 后端与 CLI

## Session 的角色

`Session` 适合把 LLM, 上下文, 工具和命令组合成可复用的会话类。后端收到平台消息后, 会通过 `SessionManager` 找到或创建对应 Session, 然后调用 `run()`。

## 同步 Session

```python
from satrap import LLM, ModelWorkflowFramework, Session, ToolsManager


class AssistantSession(Session):
    def __init__(self, session_id: str, llm: LLM, system_prompt: str = ""):
        super().__init__(session_id)
        self.workflow = ModelWorkflowFramework(
            llm=llm,
            context_id=self.workflow_id_assign("main"),
            tools_manager=ToolsManager(),
            system_prompt=system_prompt or "你是一个可靠的助手",
            content_callback=self._content_callback,
        )

    def run(self, message: str) -> str:
        return self.workflow.full_agent(message)
```

同步 Session 的 `run()` 在共享有界线程池 (`SESSION_WORKERS`, 8 线程 / 32 槽, `satrap.core.utils.async_worker.BoundedAsyncWorker`) 中执行, 不阻塞后端事件循环; 队列饱和时调用方立即收到"同步会话处理繁忙, 请稍后重试"。取消任务时线程资源会保留到底层工作真正结束 (`wait_on_cancel`), 不会提前释放会话。同模块还提供 `DNS_WORKERS` (4/16, 出站 DNS 解析) 与 `RAG_WORKERS` (4/16, RAG 管理操作) 两个单例池。

## 异步 Session

```python
from satrap import AsyncLLM, AsyncModelWorkflowFramework, AsyncSession, AsyncToolsManager


class AssistantAsyncSession(AsyncSession):
    def __init__(self, session_id: str, llm: AsyncLLM, system_prompt: str = ""):
        super().__init__(session_id)
        self.llm = llm
        self.system_prompt = system_prompt

    async def _async_init(self):
        self.workflow = await AsyncModelWorkflowFramework.create(
            llm=self.llm,
            context_id=self.workflow_id_assign("main"),
            tools_manager=AsyncToolsManager(),
            system_prompt=self.system_prompt or "你是一个可靠的助手",
            content_callback=self._content_callback,
        )

    async def run(self, message: str) -> str:
        return await self.workflow.full_agent(message)
```

`AsyncSession` 会在第一次调用 `run()` 前执行 `initialize()`, 子类可在 `_async_init()` 中创建异步资源。

## 默认命令

基础 `Session` 默认只注册 `/help`。`satrap.expend.command.session_commands` 提供可复用的 `new`, `history`, `switch`, `about` 命令函数, 具体 Session 需要显式注册它们。注册后仍可以通过 `register_command()` 添加或覆盖命令。

```python
from functools import partial

from satrap.expend.command.session_commands import (
    cmd_about,
    cmd_history,
    cmd_new,
    cmd_switch,
)

session.register_command("new", partial(cmd_new, session), "新建上下文")
session.register_command("history", partial(cmd_history, session), "查看上下文")
session.register_command("switch", partial(cmd_switch, session), "切换上下文")
session.register_command("about", cmd_about, "查看命令说明")
```

异步 Session 使用对应的 `cmd_new_async`, `cmd_history_async`, `cmd_switch_async`, `cmd_about_async` 函数, 并注册异步处理器。

```python
def ping():
    return "pong"

session.register_command("ping", ping, "健康检查")
```

## 注册 Session 类

```bash
satrap session register assistant --class-path my_app.sessions.AssistantSession --description "默认助手"
satrap session list
```

如果 Session 构造函数里有自定义参数, 注册时会生成参数模板, 后续可以配置:

```bash
satrap session config assistant --set model_name=default system_prompt=你好
satrap session config assistant --show
```

创建 Session 实例:

```bash
satrap session create assistant --id demo-session --llm default
```

## 会话实例管理

持久化会话实例 (跨平台) 的查看与清理:

```bash
satrap session instance list                            # 列出全部平台实例的会话
satrap session instance list --platform-id misskey      # 只看指定平台实例
satrap session instance delete <session_id>             # 删除单个实例
satrap session instance bulk-delete --mode empty        # 批量清理无消息实例 (另有 single / selected)
satrap session instance restart <session_id>            # 按冷配置重启实例 (仅在线)
```

在线模式走后端 API (活跃会话即时生效); 后端停止时自动回退为直读各平台 `platform.db`, `restart` 仅支持在线。

## 后端启动

前台启动 (开发调试常用, `--log-level` 控制控制台日志级别):

```bash
satrap run --config config.yaml --log-level INFO
```

后台启动 (后端已在运行时直接提示; 控制服务在线时委托其拉起, 否则 CLI 自行孵化后台进程并等待健康检查通过):

```bash
satrap start
```

常用控制命令:

```bash
satrap status
satrap reload
satrap stop       # 后端未运行时退出码 1
satrap restart    # 等价于 stop + 前台 run
```

默认 HTTP API 地址为 `http://127.0.0.1:19870`。`satrap start`, `status`, `reload`, `stop`, `restart` 会通过这个 API 与后端通信。

全部 CLI 命令遵循统一约定: 退出码 `0` 成功 / `1` 业务错误 / `2` 用法错误; 错误输出固定 `错误:` / `警告:` / `提示:` 前缀; 任意位置加 `--json` 输出结构化 JSON 便于脚本处理。CLI 也可以 `python -m satrap` 形式调用, 与 `satrap` 等价。

## 用户管理

用户信息与用户-会话绑定关系存储在所属平台实例的 `platform.db`。CLI 和管理 API 必须提供平台实例作用域, 不能配置独立用户数据库。

### CLI 命令

```bash
# 列出全部用户
satrap user list

# 查看单个用户详情 (含绑定的会话)
satrap user info misskey:user1

# 创建用户 (已存在则更新平台/昵称)
satrap user create misskey:user1 --platform misskey --nickname 小美

# 更新昵称/平台
satrap user update misskey:user1 --nickname 新昵称

# 删除用户信息 (不删除会话本身)
satrap user delete misskey:user1

# 绑定 / 解绑会话
satrap user bind misskey:user1 demo-session
satrap user unbind misskey:user1 demo-session

# 列出用户绑定的会话
satrap user sessions misskey:user1
```

所有子命令使用 `--platform-id` 选择平台实例, 使用 `--data-root` 选择完整运行数据根目录。

### HTTP API

| 端点 | 说明 |
|---|---|
| `GET /api/users` | 用户列表, 支持 `?limit=` |
| `GET /api/users?user_id=xxx` | 用户详情 |
| `GET /api/user/sessions?user_id=xxx` | 用户绑定的会话列表 |
| `POST /api/user/create` | 创建用户 `{user_id, platform, nickname}` |
| `POST /api/user/update` | 更新昵称/平台 `{user_id, nickname, platform}` |
| `POST /api/user/delete` | 删除用户 `{user_id}` |
| `POST /api/user/bind` | 绑定会话 `{user_id, session_id}` |
| `POST /api/user/unbind` | 解绑会话 `{user_id, session_id}` |

### 自动创建语义

`UserManager(auto_create=True)` 时, 未知用户会随消息路由自动创建 (平台接入默认行为);
关闭 `auto_create` 后, 未知用户不会被自动创建, `resolve_session` / `route_call` / `create_user_session` 对未知用户返回空, 需要先通过 CLI / API / 管理面板显式建号。

### 管理面板

```bash
cd satrap-ui
npm install
npm run build
cd ..
# 终端 1
python -m satrap.core.backend.control_server
# 终端 2
python -m satrap.main run
```

访问 `http://127.0.0.1:19870`。当前 React 管理面板包含:

- 仪表盘
- 模型配置
- Session 管理
- 平台状态
- 日志监控
- 系统设置
- 检查点管理
- 用户管理

## 聊天插件管理

聊天服务 (19872) 的插件与单项能力管理:

```bash
satrap plugin list                                   # 插件列表 (启用状态 / 来源 / 能力计数)
satrap plugin show rag                               # 插件详情
satrap plugin enable rag                             # 启用插件
satrap plugin disable rag                            # 停用插件
satrap plugin capability rag tools search off        # 关闭单项能力 (tools/skills/handlers/commands/mcp)
satrap plugin config rag --show                      # 查看插件全局配置
satrap plugin config rag --set top_k=5               # 修改配置 (按键类型严格校验)
```

能力生效 = 插件启用 AND 能力独立启用。Chat 服务在线时写操作走 HTTP (活动会话即时同步); 离线时直写 `.satrap/chat_plugins.json` 与 `.satrap/plugin_config/`, 下次启动生效。

## Edictum 配置管理

Edictum 命名配置 (会话行为模板) 的全生命周期:

```bash
satrap edictum types                                 # 列出可用 Edictum 类型
satrap edictum list                                  # 列出命名配置
satrap edictum show my-preset                        # 查看详情
satrap edictum create my-preset --type simple --model default --set system_prompt=你好
satrap edictum update my-preset --rename new-name    # 改名会自动迁移会话引用
satrap edictum enable my-preset
satrap edictum disable my-preset
satrap edictum delete my-preset                      # 仍被会话引用时拒绝删除
satrap edictum preview                               # 预览运行时配置变更影响 (仅在线)
satrap edictum apply                                 # 应用运行时配置变更 (仅在线)
```

离线修改后, 在线后端需要 `edictum apply` 或 `satrap reload` 才会生效。

## 离线写入

部分 CLI 写操作会优先请求在线后端。需要直接写本地配置时, 可以使用:

```bash
satrap session register assistant --class-path my_app.sessions.AssistantSession --offline
satrap model set llm default --set model=your-model --offline
```

后端在线但仍要写本地文件时:

```bash
satrap model set llm default --set model=your-model --force-offline
```
