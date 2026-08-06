"""可选扩展包入口

目录结构:
- tools/: 工具类集 (agent / mem0 / rag / sandbox_tools / search), 推荐从 tools 子包导入
- mcp/: MCP 生态扩展 (预留)
- command/: 可复用的 Session 命令
- skills/: 内置技能 (coding-agent / web-research)

顶层 `from satrap.expend import X` 导出保持可用, 旧模块路径
`satrap.expend.<mod>` 亦通过别名兼容 (见下文)。
"""
import sys

from . import tools
from .tools import (
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

# 旧路径兼容: satrap.expend.<mod> -> satrap.expend.tools.<mod>
for _name in ("agent", "mem0", "rag", "sandbox_tools", "search"):
    sys.modules[f"{__name__}.{_name}"] = getattr(tools, _name)

__all__ = [
    "Mem0Memory",
    "LiteVectorRAG",
    "DataBaseRAG",
    "CodeSandboxTool",
    "AsyncCodeSandboxTool",
    "SearchTool",
    "AsyncSearchTool",
    "FetchPageTool",
    "AsyncFetchPageTool",
]
