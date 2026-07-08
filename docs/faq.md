# 常见问题

## ModuleNotFoundError: No module named 'colorlog'

说明依赖没有安装到当前 Python 环境。

```bash
python -m pip install -e .
python -c "import colorlog; print(colorlog.__file__)"
```

如果使用管理面板:

```bash
python -m pip install -e .[admin]
```

## API key 应该放在哪里

直接写代码时可以传给 `LLM` / `AsyncLLM`:

```python
LLM(api_key="your-api-key", base_url="https://api.example.com/v1", model="your-model")
```

后端和平台接入场景推荐使用:

```bash
satrap model set llm default --set api_key=sk-xxx base_url=https://api.example.com/v1 model=your-model
```

平台 token 建议放在环境变量中, 配置文件里使用 `${ENV_NAME}` 引用。

## base_url 应该写到哪里

推荐写到 `/v1`:

```text
https://api.example.com/v1
```

如果误写成完整 endpoint:

```text
https://api.example.com/v1/chat/completions
```

Satrap 的多模态处理会尝试归一化为 OpenAI client 需要的 base URL。

## 工具调用失败会中断 Agent 吗

默认不会。`ToolsManager` 会把错误包装为结构化结果, 写回 tool result, 让模型继续基于错误信息给出最终回答。

常见错误类型:

- `not_found`
- `disabled`
- `invalid_arguments`
- `execution_error`
- `invalid_tool_call`

## 图片太大会怎样

本地图片会在发送前压缩并转为 JPEG data URL。默认最长边为 1600px, 目标大小小于 4MB, 原图文件不会被修改。

远程 URL 和已经是 data URL 的图片不会改写文件。

## 上下文为什么变短了

`ContextManager.get_model_context()` 会按 `max_context * context_threshold` 计算阈值, 超过后按策略截断。

可选策略:

- `sliding`: 滑动窗口, 删除最旧对话轮次
- `mid_truncate`: 中间截断, 保留头尾轮次

初始化示例:

```python
ctx = ContextManager(
    conversation_id="demo",
    max_context=128000,
    context_threshold=0.9,
    exceed_process="sliding",
)
```

## CLI 写配置时提示后端在线

写配置命令默认会优先走后端 API。需要直接写本地文件时, 使用:

```bash
satrap model set llm default --set model=your-model --offline
```

后端在线但仍要本地写入时:

```bash
satrap model set llm default --set model=your-model --force-offline
```

## Session 扫描不到类

检查三点:

1. 类必须继承 `Session` 或 `AsyncSession`
2. 文件必须位于 `session_scan_paths` 中
3. `class_path` 必须能被 Python import, 例如 `my_app.sessions.AssistantSession`

可以先运行:

```bash
satrap session scan --path .satrap/session
```

## 管理面板启动后看不到后端状态

先确认后端是否已启动:

```bash
satrap status
satrap run --config config.yaml
```

如果改过 API 地址, 需要保证 CLI, 管理面板和配置文件使用同一个 `api.host` / `api.port`。
