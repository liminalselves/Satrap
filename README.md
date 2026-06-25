# Satrap

Satrap 是一个面向 Python Agent 应用的轻量框架, 提供 OpenAI-compatible LLM 调用, 多模态上下文, 工具调用, Agent workflow, 平台适配器, CLI 和管理面板能力

> 当前项目仍处于早期开发阶段, API 可能继续调整

## 功能特性

- **LLM 调用封装**: 支持同步 / 异步调用 OpenAI-compatible Chat Completions API
- **多模态输入**: 支持文本, 远程图片 URL, data URL 和本地图片路径, 可自动压缩大图
- **上下文管理**: 支持 SQLite 持久化, token 估算, 多模态消息保存与加载
- **工具调用**: 支持同步 / 异步工具注册, 执行, 启用, 禁用和结构化错误返回
- **Agent workflow**: 提供 `full_agent()` 封装, 可完成用户输入到工具执行再到模型输出的完整流程
- **平台接入**: 内置 OneBot, Misskey 等平台适配相关模块
- **CLI 与管理面板**: 提供 `satrap` 命令和 Streamlit 管理面板
- **可安装包**: 支持 editable 安装和可选依赖分组

## 安装

开发安装:

```bash
pip install -e .
```

安装管理面板依赖:

```bash
pip install -e .[admin]
```

安装向量数据库相关可选依赖:

```bash
pip install -e .[vector]
```

安装全部可选依赖:

```bash
pip install -e .[all]
```

也可以继续使用传统依赖文件:

```bash
pip install -r requirements.txt
```

## 快速开始

### 基础 LLM 调用

```python
from satrap import ContextManager, LLM

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="your-model",
)

ctx = ContextManager(conversation_id="demo")
ctx.add_user_message("你好, 简单介绍一下 Satrap")

response = llm.call(ctx.get_context())
print(response.content)
```

### 图片输入

```python
from satrap import ContextManager, LLM

llm = LLM(
    api_key="your-api-key",
    base_url="https://api.example.com/v1",
    model="vision-model",
)

ctx = ContextManager(conversation_id="vision-demo")
ctx.add_user_message(
    "描述这张图片",
    img_urls=["./example.jpg"],
)

response = llm.call(ctx.get_context())
print(response.content)
```

### 工具调用

```python
from satrap import Tool, ToolsManager


class AddTool(Tool):
    tool_name = "add"
    description = "计算两个数字的和"
    params_dict = {
        "a": ("number", "第一个数字"),
        "b": ("number", "第二个数字"),
    }

    def execute(self, a: float, b: float) -> float:
        return a + b


tools = ToolsManager()
tools.register_tool(AddTool())

tool_result = tools.execute_tool("add", {"a": 1, "b": 2})
print(tool_result)
```

工具执行失败时会返回结构化错误:

```python
{
    "error": "工具 add 已禁用",
    "ok": False,
    "error_type": "disabled",
    "tool_name": "add",
}
```

### Agent workflow

```python
from satrap import LLM, ModelWorkflowFramework, ToolsManager

llm = LLM(api_key="your-api-key", base_url="https://api.example.com/v1", model="your-model")
tools = ToolsManager()

agent = ModelWorkflowFramework(llm=llm, context_id="agent-demo", tools_manager=tools)
answer = agent.full_agent("帮我完成一个任务", callback=True, max_iterations=10)
print(answer)
```

## CLI

安装后可以使用 `satrap` 命令:

```bash
satrap --help
satrap run
satrap status
satrap config show
satrap model list
satrap session list
satrap platform list
```

## 管理面板

安装 `admin` 可选依赖后启动:

```bash
streamlit run satrap/admin.py
```

## 详细使用指南

完整示例和常见问题见 [docs/usage-guide.md](docs/usage-guide.md)

## 兼容性

- Python >= 3.10
- LLM 模块兼容 OpenAI-compatible Chat Completions API
- 图片输入当前支持 JPEG, PNG, WebP, GIF, BMP

## 许可证

GPL-v3.0
