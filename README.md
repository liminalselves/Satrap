# Satrap

Satrap 是一个面向 Python Agent 应用的轻量框架。它提供 OpenAI-compatible LLM 调用, 多模态上下文, 工具调用, Agent workflow, Session 管理, 后端服务, CLI, Streamlit 管理面板, 以及 OneBot / Misskey 等平台适配能力。

> 当前项目仍处于早期开发阶段, API 可能继续调整。建议在业务项目中锁定版本或提交号后再集成。

## 能做什么

- **LLM 调用封装**: 同步 / 异步调用 OpenAI-compatible Chat Completions API
- **多模态输入**: 支持文本, 远程图片 URL, data URL 和本地图片路径, 本地大图会在发送前压缩
- **上下文管理**: 使用 SQLite 持久化对话, 支持 token 估算和上下文截断
- **工具调用**: 定义同步 / 异步 Tool, 生成 OpenAI function calling 描述, 执行并返回结构化错误
- **Agent workflow**: `full_agent()` 封装“用户输入 -> 模型请求工具 -> 工具执行 -> 模型最终回复”的完整流程
- **Session 与后端**: 管理多会话, 持久化 Session 配置, 支持后端守护进程和 HTTP 管理 API
- **平台接入**: 内置 Misskey, OneBot / aiocqhttp 适配器, 可配置多个同类型平台实例
- **管理入口**: 提供 `satrap` CLI 和 Streamlit 管理面板

## 安装

```bash
pip install -e .
```

可选依赖:

```bash
pip install -e .[admin]   # Streamlit 管理面板
pip install -e .[vector]  # faiss 向量检索
pip install -e .[all]     # 全部可选依赖
```

也可以继续使用传统依赖文件:

```bash
pip install -r requirements.txt
```

## 最小示例

```python
from satrap import ContextManager, LLM

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="your-model",
)

ctx = ContextManager(conversation_id="demo")
ctx.add_user_message("用一句话介绍 Satrap")

response = llm.call(ctx.get_context())
print(response.content if response else "调用失败")
```

## CLI 与管理面板

安装后可以使用:

```bash
satrap --help
satrap run
satrap status
satrap config show
satrap model list
satrap session list
satrap platform list
```

启动管理面板:

```bash
pip install -e .[admin]
streamlit run satrap/admin.py
```

## 文档

- [文档首页](docs/README.md)
- [快速开始](docs/quick-start.md)
- [配置说明](docs/configuration.md)
- [核心 API](docs/core-api.md)
- [工具与 Agent](docs/tools-and-agent.md)
- [扩展模块](docs/extensions.md)
- [Session, 后端与 CLI](docs/sessions-backend-cli.md)
- [平台接入](docs/platforms.md)
- [常见问题](docs/faq.md)

旧入口 [docs/usage-guide.md](docs/usage-guide.md) 已改为文档导航页。

## 兼容性

- Python >= 3.10
- LLM 模块兼容 OpenAI Chat Completions API
- 图片输入支持 JPEG, PNG, WebP, GIF, BMP 等常见格式

## 许可

GPL-3.0-only
