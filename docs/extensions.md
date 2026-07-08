# 扩展模块

`satrap.expend` 放的是可选扩展能力, 适合直接注册进 Agent 或在业务 Session 中组合使用。当前顶层导出包括长期记忆, RAG, 搜索, 网页抓取和代码沙箱。

```python
from satrap.expend import (
    AsyncCodeSandboxTool,
    AsyncFetchPageTool,
    AsyncSearchTool,
    CodeSandboxTool,
    DataBaseRAG,
    FetchPageTool,
    LiteVectorRAG,
    Mem0Memory,
    SearchTool,
)
```

`satrap.expend.agent` 里的 sub-agent 目前没有从 `satrap.expend` 顶层导出, 需要从模块路径直接导入。

## 搜索与网页抓取

`SearchTool` / `AsyncSearchTool` 使用 Bing 搜索页解析结果, 返回 JSON 字符串。`FetchPageTool` / `AsyncFetchPageTool` 会抓取指定网页, 提取标题和正文纯文本。

```python
from satrap import ToolsManager
from satrap.expend import FetchPageTool, SearchTool

tools = ToolsManager()
tools.register_tool(SearchTool(timeout=10))
tools.register_tool(FetchPageTool(timeout=10))

print(tools.execute_tool("search", {
    "query": "Satrap Python Agent",
    "max_results": 5,
}))

print(tools.execute_tool("fetch_page", {
    "url": "https://example.com",
    "max_length": 3000,
}))
```

工具名:

| 类 | 工具名 | 主要参数 |
| --- | --- | --- |
| `SearchTool` | `search` | `query`, `max_results` |
| `AsyncSearchTool` | `search` | `query`, `max_results` |
| `FetchPageTool` | `fetch_page` | `url`, `max_length` |
| `AsyncFetchPageTool` | `fetch_page` | `url`, `max_length` |

这类工具依赖网络访问, 运行环境无法访问目标站点时会返回错误 JSON。

## 代码沙箱工具

`CodeSandboxTool` / `AsyncCodeSandboxTool` 封装 `satrap.core.utils.sandbox.CodeSandbox`, 可以让 Agent 在受控目录中执行 Python 代码或管理文件。

```python
import sys

from satrap import ToolsManager
from satrap.core.utils.sandbox import CodeSandbox
from satrap.expend import CodeSandboxTool

sandbox = CodeSandbox(
    sandbox_path=".satrap/sandbox",
    env=sys.executable,
)

tools = ToolsManager()
tools.register_tool(CodeSandboxTool(sandbox))

result = tools.execute_tool("code_sandbox", {
    "operation": "run",
    "code": "print(1 + 2)",
})
print(result)
```

支持的 `operation`:

| 操作 | 说明 | 常用参数 |
| --- | --- | --- |
| `run` | 执行代码字符串 | `code` |
| `run_file` | 执行沙箱内文件 | `path` |
| `save` | 保存代码到文件 | `code`, `path` |
| `read` | 读取文件内容 | `path` |
| `delete` | 删除文件 | `path` |
| `delete_dir` | 删除目录 | `path` |
| `list` | 列出文件 | `path` |

沙箱工具会尝试从 Markdown 代码块中提取代码。给 Agent 使用时, 建议在系统提示中限制可执行范围和文件路径。

## RAG

`LiteVectorRAG` 基于 `LiteVectorDB`, `DataBaseRAG` 基于 `DataBase`, 都用于把文本切块, 向量化, 存入本地向量库并检索。

```python
import asyncio

from satrap.expend import LiteVectorRAG


async def main():
    rag = LiteVectorRAG(
        base_url="https://api.example.com/v1",
        api_key="your-api-key",
        embed_model="your-embedding-model",
        persist_directory=".satrap/rag",
        default_vectorstore_name="default",
    )

    await rag.add_documents([
        "Satrap 支持 LLM 调用, 工具调用和 Session 管理。",
        "Satrap 可以接入 Misskey 和 OneBot 平台。",
    ])

    docs = await rag.simple_query("Satrap 支持哪些平台?", k=4)
    print(docs)


asyncio.run(main())
```

常用能力:

| 方法 | 说明 |
| --- | --- |
| `add_documents()` | 添加文本列表, 自动切块和向量化 |
| `add_text_file()` | 添加文本文件 |
| `simple_query()` | 语义检索, 返回文本列表 |
| `get_collection_names()` | 查看集合名称 |
| `get_vectorstore_overview()` | 查看向量库概览 |
| `delete_collection()` | 删除集合 |

RAG 依赖 embedding 服务。需要向量检索增强时, 建议安装:

```bash
pip install -e .[vector]
```

## 长期记忆

`Mem0Memory` 是基于 `DataBase` 的长期记忆系统。它用 `AsyncLLM` 从对话中提取事实, 用 `AsyncEmbedding` 做语义检索, 并按用户维度保存记忆。

```python
import asyncio

from satrap.core.APICall.EmbedCall import AsyncEmbedding
from satrap import AsyncLLM
from satrap.expend import Mem0Memory


async def main():
    llm = AsyncLLM(
        api_key="your-api-key",
        base_url="https://api.example.com/v1",
        model="your-model",
    )
    embedding = AsyncEmbedding(
        api_key="your-api-key",
        base_url="https://api.example.com/v1",
        model="your-embedding-model",
    )

    memory = Mem0Memory(
        llm=llm,
        embedding=embedding,
        persist_path=".satrap/mem0",
        top_k=10,
        similarity_threshold=0.5,
    )

    affected_ids = await memory.add(
        user_message="我喜欢用中文回答。",
        assistant_message="已记住, 以后优先使用中文。",
        user_id="user-1",
    )
    print(affected_ids)

    results = await memory.search("用户的语言偏好是什么?", user_id="user-1")
    print(results)


asyncio.run(main())
```

常用方法:

| 方法 | 说明 |
| --- | --- |
| `add()` | 从一轮对话中提取并更新记忆 |
| `search()` | 按语义检索用户记忆 |
| `get_all()` | 获取用户全部记忆 |
| `delete()` | 按 `memory_id` 删除 |
| `clear()` | 清空用户全部记忆 |
| `get_summary()` | 获取用户摘要 |
| `get_stats()` | 获取记忆统计 |

## Sub-Agent

`SubAgent` / `AsyncSubAgent` 可以把一个任务数组分配给多个独立上下文中的子 Agent 并汇总结果。它适合拆解可并行处理的任务。

```python
from satrap.expend.agent import SubAgent

sub_agent_tool = SubAgent(llm=llm, tools_manager=tools)
tools.register_tool(sub_agent_tool)
```

`sub_agent` 工具的参数是 `task`, 期望是 JSON 数组字符串, 例如:

```json
["搜索项目 A 的信息", "总结项目 B 的风险", "比较 A 和 B"]
```

同步版本内部使用线程池并发执行。异步版本使用 `asyncio.gather()` 并发执行。需要注意底层模型服务的并发限制和费用。

## 默认 Session 命令

`satrap.expend.command.session_commands` 提供 Session 默认命令函数, 基础 Session 会将它们注册到命令处理器中。

| 命令 | 说明 |
| --- | --- |
| `new` | 基于当前 Session 创建新上下文 |
| `history` | 查看当前用户可切换的上下文 |
| `switch` | 切换到指定上下文 |
| `about` | 返回命令说明 |

这些命令依赖 `UserManager` 的上下文绑定能力。只在裸 `Session` 中直接调用时, `history` 可能返回空列表。
