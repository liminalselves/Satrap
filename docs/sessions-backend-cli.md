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

## 后端启动

```bash
satrap run --config config.yaml
```

常用控制命令:

```bash
satrap status
satrap reload
satrap stop
satrap restart
```

默认 HTTP API 地址为 `http://127.0.0.1:19870`。`satrap status`, `reload`, `stop`, `restart` 会通过这个 API 与后端通信。

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
