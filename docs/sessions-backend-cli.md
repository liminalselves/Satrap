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

基础 `Session` 内置命令处理器。默认命令由 `satrap.expend.command.session_commands` 注册, 常见用途包括查看历史上下文, 新建上下文和切换上下文。可以通过 `register_command()` 添加或覆盖命令。

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

## 管理面板

```bash
pip install -e .[admin]
streamlit run satrap/admin.py
```

当前管理面板包含:

- 仪表盘
- 模型配置
- Session 管理
- 平台状态
- 日志监控
- 系统设置

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
