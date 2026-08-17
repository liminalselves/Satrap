# base_take 插件: 基础能力集

`base_take` 是官方预设目录 (`satrap/expend/plugins`) 下的目录插件, 提供通用基础能力: 网页搜索、网页抓取、代码沙箱、文档解析与长期记忆。与 `satrap_coding` 插件互补, 可独立或组合使用。

## 安装

```python
from satrap import SimpleSession

session = SimpleSession("conv-1", llm, db_path="chat.db")
session.install_plugin("./satrap/expend/plugins/base_take")

plugin = session.list_plugins()[0]
plugin.name          # "base_take"
plugin.tools         # 8 个工具
plugin.handlers      # base_take.memory_inject
```

异步版 `AsyncSimpleSession` 同样支持 (`await session.install_plugin(path)`)。

## 能力清单

| 类别 | 能力 | 说明 |
| --- | --- | --- |
| 搜索 | search / fetch_page | 网页搜索与抓取 (复用 expend.tools.search) |
| 沙箱 | code_sandbox | 在隔离目录中执行 Python 代码 |
| 文档 | read_document | 解析 xlsx / docx / pdf / 纯文本为纯文本 |
| 记忆 | add_memory / update_memory / delete_memory / list_memories | 长期记忆增删改查 |
| 处理器 | base_take.memory_inject | 用户消息进入模型前注入记忆块 |

## 插件配置

meta.yaml 声明 `config_schema`, 支持以下配置项 (全局默认 + 按会话覆盖两级):

| 配置键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| sandbox_root | path | .satrap/sandbox | 沙箱根目录 (全局共享) |
| workspace_root | path | 项目根 | read_document 白名单根目录 |
| search_timeout | number | 10 | 搜索超时 (秒) |
| memory_scope | string | web_chat | 记忆作用域 |

安装时经 `install_plugin(path, config={...})` 传入会话级覆盖; 全局默认存于 `.satrap/plugin_config/base_take.json`。

## 沙箱协调

base_take 与 satrap_coding 共享同一沙箱目录 (`.satrap/sandbox`)。当两插件同时启用时, 由 ChatService 层自动停用 base_take 的 `code_sandbox` 工具 (coding 的 shell 能力更强), 避免模型困惑。

## 长期记忆

记忆存储使用公共 MemoryStore (`.satrap/satrapdata/memory.db`), 按 scope 隔离 (默认 `web_chat`)。记忆由注入处理器自动拼接到后续用户消息头部 (importance 降序, 上限 30 条), 保证模型每轮都携带已知约定。

## 文档解析

`read_document` 工具支持解析以下格式为纯文本:

- `.xlsx` — openpyxl 读取, 按 sheet 输出 TSV
- `.docx` — python-docx 读取, 段落 + 表格
- `.pdf` — pdfplumber 逐页提取文本
- 纯文本 — 直接读取 (utf-8)

文件路径限制在 workspace_root 白名单内, 输出按 `max_length` 截断 (默认 8000 字符)。
