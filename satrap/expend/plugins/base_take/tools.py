"""base_take 插件工具集: search / fetch_page / code_sandbox / read_document / memory

约定:
- get_tools(session, config) 工厂: 按会话形态返回同步/异步工具, 配置经合成后注入
- search/fetch_page 复用 expend.tools.search, timeout 从 config
- code_sandbox 复用 expend.tools.sandbox_tools, sandbox_root 从 config (全局共享目录)
- read_document 解析 xlsx/docx/pdf 为纯文本 (core.docread)
- memory 复用公共 MemoryStore (expend.tools.memory_store), scope 从 config
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from satrap.core.utils.paths import get_project_root
from satrap.core.utils.sandbox import CodeSandbox
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.edictum import AsyncSimpleSession, SimpleSession
from satrap.expend.tools import (
    AsyncCodeSandboxTool,
    AsyncFetchPageTool,
    AsyncSearchTool,
    CodeSandboxTool,
    FetchPageTool,
    SearchTool,
)
from satrap.expend.tools.memory_store import MemoryStore

SessionType = SimpleSession | AsyncSimpleSession
"""插件支持的会话类型"""

DEFAULT_SANDBOX_ROOT = get_project_root() / ".satrap" / "sandbox"
"""默认沙箱根目录 (全局唯一, 与 coding 插件共享)"""


# ================= read_document =================


def _resolve_doc_path(path: str, workspace_root: Path) -> Path:
    """解析文档路径: 相对路径基于工作区根, 绝对路径须在工作区内;
    若工作区根下未找到, 尝试在 .satrap/uploads/ 各会话目录中按文件名搜索"""
    p = Path(path)
    abs_path = p.resolve() if p.is_absolute() else (workspace_root / p).resolve()
    if not abs_path.is_relative_to(workspace_root.resolve()):
        raise ValueError(f"路径越出工作区: {path}")
    # 工作区根下找到则直接返回
    if abs_path.is_file():
        return abs_path
    # fallback: 在 uploads 目录中按文件名搜索 (上传文件保存为 {uuid}_{filename})
    uploads_root = workspace_root / ".satrap" / "uploads"
    if uploads_root.is_dir():
        target_name = p.name.lower()
        for conv_dir in uploads_root.iterdir():
            if not conv_dir.is_dir():
                continue
            for fpath in conv_dir.iterdir():
                if not fpath.is_file():
                    continue
                # 匹配 uuid_filename 格式中的文件名部分
                fname = fpath.name.lower()
                if fname == target_name or fname.endswith(f"_{target_name}"):
                    return fpath.resolve()
    # 均未找到, 返回原始路径 (让调用方报文件不存在)
    return abs_path


class ReadDocumentTool(Tool):
    """读取文档 (xlsx/docx/pdf/txt/md 等) 并解析为纯文本"""

    tool_name = "read_document"
    description = "读取文档并解析为纯文本, 支持 xlsx/docx/pdf/txt/md 等; 适合读取表格、文档、PDF 内容"
    params_dict = {
        "path": ("string", "文档路径 (绝对或相对工作区)"),
        "max_length": ("number", "返回文本最大长度, 默认 131072, 超出截断"),
    }

    def __init__(self, workspace_root: Path) -> None:
        super().__init__()
        self.workspace_root = workspace_root

    def execute(self, path: str, max_length: int = 131072) -> str:
        from satrap.expend.plugins.base_take.core.docread import extract_text

        try:
            abs_path = _resolve_doc_path(path, self.workspace_root)
        except ValueError as e:
            return f"错误: {e}"
        try:
            text = extract_text(abs_path)
        except ValueError as e:
            return f"错误: {e}"
        limit = max(1, int(max_length))
        if len(text) > limit:
            return text[:limit] + f"\n... (已截断, 共 {len(text)} 字符)"
        return text or "(文档为空)"


class AsyncReadDocumentTool(AsyncTool):
    """读取文档并解析为纯文本 (异步)"""

    tool_name = "read_document"
    description = "读取文档并解析为纯文本, 支持 xlsx/docx/pdf/txt/md 等; 适合读取表格、文档、PDF 内容"
    params_dict = {
        "path": ("string", "文档路径 (绝对或相对工作区)"),
        "max_length": ("number", "返回文本最大长度, 默认 131072, 超出截断"),
    }

    def __init__(self, workspace_root: Path) -> None:
        super().__init__()
        self.workspace_root = workspace_root

    async def execute(self, path: str, max_length: int = 131072) -> str:
        import asyncio

        from satrap.expend.plugins.base_take.core.docread import extract_text

        try:
            abs_path = _resolve_doc_path(path, self.workspace_root)
        except ValueError as e:
            return f"错误: {e}"
        try:
            text = await asyncio.to_thread(extract_text, abs_path)
        except ValueError as e:
            return f"错误: {e}"
        limit = max(1, int(max_length))
        if len(text) > limit:
            return text[:limit] + f"\n... (已截断, 共 {len(text)} 字符)"
        return text or "(文档为空)"


# ================= memory (模型自驱增删改) =================


class _MemoryToolBase(Tool):
    """记忆工具基类: store 绑定"""

    def __init__(self, store: MemoryStore) -> None:
        super().__init__()
        self.store = store


class AddMemoryTool(_MemoryToolBase):
    """添加一条长期记忆 (用户偏好/项目约定/关键决策)"""

    tool_name = "add_memory"
    description = "添加一条长期记忆, 记忆会注入后续对话上下文; 适合记录用户偏好、项目约定、关键决策"
    params_dict = {
        "title": ("string", "简短记忆标题"),
        "content": ("string", "记忆内容"),
        "tags": ("array", "分类标签"),
        "importance": ("number", "重要程度 1-5, 默认 1"),
    }

    def execute(self, title: str, content: str, tags: list[str] | None = None, importance: int = 1) -> str:
        if not self.store.can_write():
            return "记忆处于只读模式, 无法添加"
        result = self.store.add(title, content, tags, importance)
        if result.get("ok"):
            return f"记忆已添加: [{title}] {content}"
        return f"添加失败: {result.get('error')}"


class UpdateMemoryTool(_MemoryToolBase):
    """更新一条已有记忆"""

    tool_name = "update_memory"
    description = "按记忆 ID 更新已有长期记忆 (信息变化或修正时使用)"
    params_dict = {
        "memory_id": ("string", "要更新的记忆 ID"),
        "content": ("string", "更新后的内容"),
        "title": ("string", "更新后的标题"),
    }

    def execute(self, memory_id: str, content: str = "", title: str = "") -> str:
        if not self.store.can_write():
            return "记忆处于只读模式, 无法更新"
        fields: dict[str, Any] = {}
        if content:
            fields["content"] = content
        if title:
            fields["title"] = title
        result = self.store.update(memory_id, **fields)
        if result.get("ok"):
            return f"记忆已更新: [{result['title']}] {result['content']}"
        return f"更新失败: {result.get('error')}"


class DeleteMemoryTool(_MemoryToolBase):
    """删除一条记忆"""

    tool_name = "delete_memory"
    description = "按记忆 ID 删除一条长期记忆"
    params_dict = {
        "memory_id": ("string", "要删除的记忆 ID"),
    }

    def execute(self, memory_id: str) -> str:
        if not self.store.can_write():
            return "记忆处于只读模式, 无法删除"
        result = self.store.delete(memory_id)
        if result.get("ok"):
            return f"记忆已删除: {memory_id}"
        return f"删除失败: {result.get('error')}"


class ListMemoriesTool(_MemoryToolBase):
    """查看全部长期记忆"""

    tool_name = "list_memories"
    description = "列出当前全部长期记忆 (含 ID, 供 update/delete 定位)"
    params_dict: dict[str, Any] = {}

    def execute(self) -> str:
        memories = self.store.list_all()
        if not memories:
            return "当前没有长期记忆"
        lines = [f"共 {len(memories)} 条记忆:"]
        for m in memories:
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            lines.append(f"- {m['id'][:8]} [{m['title']}] {m['content']}{tags} (重要度 {m['importance']})")
        return "\n".join(lines)


# ================= 异步 memory 工具 =================


class _AsyncMemoryToolBase(AsyncTool):
    """异步记忆工具基类"""

    def __init__(self, store: MemoryStore) -> None:
        super().__init__()
        self.store = store


class AsyncAddMemoryTool(_AsyncMemoryToolBase):
    """添加长期记忆 (异步)"""

    tool_name = "add_memory"
    description = "添加一条长期记忆, 记忆会注入后续对话上下文; 适合记录用户偏好、项目约定、关键决策"
    params_dict = {
        "title": ("string", "简短记忆标题"),
        "content": ("string", "记忆内容"),
        "tags": ("array", "分类标签"),
        "importance": ("number", "重要程度 1-5, 默认 1"),
    }

    async def execute(self, title: str, content: str, tags: list[str] | None = None, importance: int = 1) -> str:
        if not self.store.can_write():
            return "记忆处于只读模式, 无法添加"
        result = self.store.add(title, content, tags, importance)
        if result.get("ok"):
            return f"记忆已添加: [{title}] {content}"
        return f"添加失败: {result.get('error')}"


class AsyncUpdateMemoryTool(_AsyncMemoryToolBase):
    """更新长期记忆 (异步)"""

    tool_name = "update_memory"
    description = "按记忆 ID 更新已有长期记忆 (信息变化或修正时使用)"
    params_dict = {
        "memory_id": ("string", "要更新的记忆 ID"),
        "content": ("string", "更新后的内容"),
        "title": ("string", "更新后的标题"),
    }

    async def execute(self, memory_id: str, content: str = "", title: str = "") -> str:
        if not self.store.can_write():
            return "记忆处于只读模式, 无法更新"
        fields: dict[str, Any] = {}
        if content:
            fields["content"] = content
        if title:
            fields["title"] = title
        result = self.store.update(memory_id, **fields)
        if result.get("ok"):
            return f"记忆已更新: [{result['title']}] {result['content']}"
        return f"更新失败: {result.get('error')}"


class AsyncDeleteMemoryTool(_AsyncMemoryToolBase):
    """删除长期记忆 (异步)"""

    tool_name = "delete_memory"
    description = "按记忆 ID 删除一条长期记忆"
    params_dict = {
        "memory_id": ("string", "要删除的记忆 ID"),
    }

    async def execute(self, memory_id: str) -> str:
        if not self.store.can_write():
            return "记忆处于只读模式, 无法删除"
        result = self.store.delete(memory_id)
        if result.get("ok"):
            return f"记忆已删除: {memory_id}"
        return f"删除失败: {result.get('error')}"


class AsyncListMemoriesTool(_AsyncMemoryToolBase):
    """查看全部长期记忆 (异步)"""

    tool_name = "list_memories"
    description = "列出当前全部长期记忆 (含 ID, 供 update/delete 定位)"
    params_dict: dict[str, Any] = {}

    async def execute(self) -> str:
        memories = self.store.list_all()
        if not memories:
            return "当前没有长期记忆"
        lines = [f"共 {len(memories)} 条记忆:"]
        for m in memories:
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            lines.append(f"- {m['id'][:8]} [{m['title']}] {m['content']}{tags} (重要度 {m['importance']})")
        return "\n".join(lines)


# ================= get_tools 工厂 (会话 + 配置注入) =================


def get_tools(session: SessionType, config: dict[str, Any] | None = None) -> list[Any]:
    """按会话形态构建全部工具 (注入配置: timeout/sandbox_root/workspace_root/memory)"""
    from satrap.expend.plugins.base_take.state import get_plugin_state

    cfg = config or {}
    timeout = int(cfg.get("search_timeout") or 10)
    sandbox_root = Path(str(cfg.get("sandbox_root") or DEFAULT_SANDBOX_ROOT))
    workspace_root = Path(str(cfg.get("workspace_root") or get_project_root()))

    state = get_plugin_state(session, cfg)
    store = state["store"]
    assert isinstance(store, MemoryStore)

    sandbox = CodeSandbox(str(sandbox_root), sys.executable)

    if isinstance(session, AsyncSimpleSession):
        return [
            AsyncSearchTool(timeout=timeout),
            AsyncFetchPageTool(timeout=timeout),
            AsyncCodeSandboxTool(sandbox),
            AsyncReadDocumentTool(workspace_root),
            AsyncAddMemoryTool(store),
            AsyncUpdateMemoryTool(store),
            AsyncDeleteMemoryTool(store),
            AsyncListMemoriesTool(store),
        ]
    return [
        SearchTool(timeout=timeout),
        FetchPageTool(timeout=timeout),
        CodeSandboxTool(sandbox),
        ReadDocumentTool(workspace_root),
        AddMemoryTool(store),
        UpdateMemoryTool(store),
        DeleteMemoryTool(store),
        ListMemoriesTool(store),
    ]

