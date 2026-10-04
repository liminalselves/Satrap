# base_take 插件: 基础能力集

`base_take` 是官方预设目录 (`satrap/expend/plugins`) 下的目录插件, 提供通用基础能力: 网页搜索、网页抓取、代码沙箱、文档解析。与 `satrap_coding` 插件互补, 可独立或组合使用。

## 安装

```python
from satrap import SimpleSession

session = SimpleSession("conv-1", llm, db_path="chat.db")
session.install_plugin("./satrap/expend/plugins/base_take")

plugin = session.list_plugins()[0]
plugin.name          # "base_take"
plugin.tools         # 4 个工具
plugin.handlers      # 无记忆处理器
plugin.commands      # 无记忆命令
```

异步版 `AsyncSimpleSession` 同样支持 (`await session.install_plugin(path)`)。

## 能力清单

| 类别 | 能力 | 说明 |
| --- | --- | --- |
| 搜索 | search / fetch_page | 网页搜索与抓取 (复用 expend.tools.search) |
| 沙箱 | code_sandbox | 在隔离目录中执行 Python 代码 |
| 文档 | read_document | 解析 xlsx / docx / pdf / 纯文本为纯文本 |

## 插件配置

meta.yaml 声明 `config_schema`, 支持以下配置项 (全局默认 + 按会话覆盖两级):

| 配置键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| sandbox_root | path | 空 | 独立调用的兜底值; 平台运行时始终使用会话私有 sandbox |
| workspace_root | path | 项目根 | read_document 白名单根目录 |
| search_timeout | number | 10 | 搜索超时 (秒) |

安装时经 `install_plugin(path, config={...})` 传入会话级覆盖; 全局默认存于 `.satrap/config/plugins/base_take.json`。

## 沙箱协调

base_take 与 satrap_coding 在同一会话内共享该会话的私有 sandbox, 不与其他会话共享。当两插件同时启用时, ChatService 自动停用 base_take 的 `code_sandbox` 工具, 避免能力重复。

## 记忆能力拆分

长期记忆已独立为 [memory 插件](memory-plugin.md), 本插件不再持有记忆状态, 工具, 命令或注入处理器
旧 Agent 配置中的记忆设置与能力开关由配置迁移逻辑移交, 已有数据库记录与 ID 保留

## 文档解析

`read_document` 工具支持解析以下格式为纯文本:

- `.xlsx` — openpyxl 读取, 按 sheet 输出 TSV
- `.docx` — python-docx 读取, 段落 + 表格
- `.pdf` — pdfplumber 逐页提取文本
- 纯文本 — 直接读取 (utf-8)

文件路径限制在 workspace_root 白名单内 (项目会话为项目工作区, 调用时按会话解析); 若工作区根下未找到, 只回退搜索当前会话的私有 `uploads/`。输出按 `max_length` 截断 (默认 131072 字符)。
