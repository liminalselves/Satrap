"""
base_take 插件工具集: search / fetch_page / code_sandbox / read_document / memory

约定:
- get_tools(session, config) 工厂: 按会话形态返回同步/异步工具, 配置经合成后注入
- search/fetch_page 复用 expend.tools.search, timeout 从 config
- code_sandbox 复用 expend.tools.sandbox_tools, sandbox_root 从 config (全局共享目录)
- read_document 解析 xlsx/docx/pdf 为纯文本 (core.docread)
- memory 复用公共 MemoryStore (expend.tools.memory_store), scope 从 config
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, cast
from typing import Awaitable, Callable
import sys

from satrap.expend.plugins.base_take.core.docread import extract_text
from satrap.expend.plugins.base_take.state import get_plugin_state
from satrap.expend.tools.memory_store import MemoryStore
from satrap.core.utils.TCBuilder import AsyncTool, Tool
from satrap.core.utils.sandbox import CodeSandbox
from satrap.core.utils.paths import get_project_root
from satrap.expend.tools import (
    AsyncCodeSandboxTool,
    AsyncFetchPageTool,
    AsyncSearchTool,
    CodeSandboxTool,
    FetchPageTool,
    SearchTool,
)
from satrap.core.type import safe_getattr, safe_getattr_callable
from satrap.edictum import AsyncSimpleSession, SimpleSession

SessionType = SimpleSession | AsyncSimpleSession
"""插件支持的会话类型"""

DEFAULT_SANDBOX_ROOT = get_project_root() / ".satrap" / "sandbox"
"""默认沙箱根目录 (全局唯一, 与 coding 插件共享)"""


def _call_user_input_provider(
    provider: Callable[..., object],
    question: str,
    options: list[str],
) -> object:
    """
    兼容新旧用户输入通道, 优先结构化传递选项

    参数:
    - provider: 动态用户输入回调
    - question: 提示问题
    - options: 可选回答列表

    返回:
    - 用户输入回调的同步结果或可等待结果
    """
    try:
        inspect.signature(provider).bind(question, options)
    except (TypeError, ValueError):
        numbered = "  ".join(f"{index}. {option}" for index, option in enumerate(options, 1))
        return provider(f"{question} 可选: {numbered}")
    return provider(question, options)


def _execution_approved(answer: object) -> bool:
    """
    判断代码执行授权回答是否明确同意

    参数:
    - answer: 用户输入通道返回的动态回答

    返回:
    - 回答明确同意执行时返回 True
    """
    return str(answer).strip().lower() in ("y", "yes", "允许", "批准")


def _make_sync_execution_authorizer(session: SimpleSession):
    """
    构造同步代码执行授权器, 无输入通道时保持拒绝

    参数:
    - session: 提供同步用户输入通道的会话

    返回:
    - 接收执行说明并返回授权决定的同步回调
    """
    def authorize(description: str) -> bool:
        """
        请求用户确认单次代码执行

        参数:
        - description: 本次执行操作说明

        返回:
        - 用户明确批准时返回 True
        """
        provider = safe_getattr_callable(session, "user_input_provider")
        if provider is None:
            return False
        question = f"代码沙箱仅限制工作目录, 代码仍可访问系统资源. 是否允许{description}?"
        return _execution_approved(_call_user_input_provider(provider, question, ["允许", "拒绝"]))

    return authorize


def _make_async_execution_authorizer(session: AsyncSimpleSession):
    """
    构造异步代码执行授权器, 无输入通道时保持拒绝

    参数:
    - session: 提供异步用户输入通道的会话

    返回:
    - 接收执行说明并返回授权决定的异步回调
    """
    async def authorize(description: str) -> bool:
        """
        异步请求用户确认单次代码执行

        参数:
        - description: 本次执行操作说明

        返回:
        - 用户明确批准时返回 True
        """
        provider = safe_getattr_callable(session, "user_input_provider")
        if provider is None:
            return False
        question = f"代码沙箱仅限制工作目录, 代码仍可访问系统资源. 是否允许{description}?"
        answer = _call_user_input_provider(provider, question, ["允许", "拒绝"])
        if inspect.isawaitable(answer):
            answer = await cast(Awaitable[object], answer)
        return _execution_approved(answer)

    return authorize


# ================= read_document 工具 =================


def _resolve_doc_path(
    path: str,
    workspace_root: Path,
    uploads_root: Path | None = None,
) -> Path:
    """
    解析文档路径: 相对路径基于工作区根, 绝对路径须在工作区内;
    若工作区根下未找到, 只在当前会话独占 uploads 目录中按文件名搜索

    参数:
    - path: 路径
    - workspace_root: workspace根目录
    - uploads_root: 当前会话独占 uploads 目录

    返回:
    - Path: 解析文档路径: 相对路径基于工作区根, 绝对路径须在工作区内
    """
    p = Path(path)
    workspace = workspace_root.resolve()
    uploads = uploads_root.resolve() if uploads_root is not None else None
    abs_path = p.resolve() if p.is_absolute() else (workspace / p).resolve()
    in_workspace = abs_path.is_relative_to(workspace)
    in_uploads = uploads is not None and abs_path.is_relative_to(uploads)
    if not in_workspace and not in_uploads:
        raise ValueError(f"路径越出当前会话可见范围: {path}")
    if abs_path.is_file():
        return abs_path
    # fallback: 只搜索当前会话 uploads (上传文件保存为 {uuid}_{filename})
    if uploads is not None and uploads.is_dir():
        target_name = p.name.lower()
        for fpath in uploads.iterdir():
            if not fpath.is_file():
                continue
            fname = fpath.name.lower()
            if fname == target_name or fname.endswith(f"_{target_name}"):
                return fpath.resolve()
    # 均未找到, 返回原始路径 (让调用方报文件不存在)
    return abs_path


def _doc_workspace_root(tool: Any) -> Path:
    """
    文档工具的工作区根 (调用时解析): 会话鸭子属性优先 (项目会话), 否则安装期基线

    参数:
    - tool: 工具

    返回:
    - Path: 文档工具的工作区根 (调用时解析): 会话鸭子属性优先 (项目会话), 否则安装期基线
    """
    override = safe_getattr(getattr(tool, "_session", None), "coding_workspace_root")
    if override:
        return Path(str(override)).resolve()
    return cast(Path, tool._base_root)


def _doc_upload_root(tool: Any) -> Path | None:
    """
    获取当前会话独占 uploads 目录

    参数:
    - tool: 文档工具

    返回:
    - Path | None: 未注入会话存储时返回 None
    """
    override = safe_getattr(getattr(tool, "_session", None), "coding_upload_root")
    return Path(str(override)).resolve() if override else None


class ReadDocumentTool(Tool):
    """读取文档 (xlsx/docx/pdf/txt/md 等) 并解析为纯文本"""

    tool_name = "read_document"
    description = "读取文档并解析为纯文本, 支持 xlsx/docx/pdf/txt/md 等; 适合读取表格、文档、PDF 内容"
    params_dict = {
        "path": ("string", "文档路径 (绝对或相对工作区)"),
        "max_length": ("number", "返回文本最大长度, 默认 131072, 超出截断"),
    }

    def __init__(self, workspace_root: Path) -> None:
        """
        初始化 ReadDocumentTool

        参数:
        - workspace_root: 工作区根目录
        """
        super().__init__()
        self._base_root = workspace_root
        """安装期基线工作区根 (cfg/全局); 项目会话经会话鸭子属性在调用时覆盖"""

    def execute(self, path: str, max_length: int = 131072) -> str:
        """
        执行

        参数:
        - path: 路径
        - max_length: 最大长度

        返回:
        - str: 执行
        """
        try:
            abs_path = _resolve_doc_path(path, _doc_workspace_root(self), _doc_upload_root(self))
        except ValueError as e:
            return f"错误: {e}"
        try:
            limit = max(1, min(1_000_000, int(max_length)))
        except (TypeError, ValueError, OverflowError):
            return "错误: max_length 必须是整数"
        try:
            text = extract_text(abs_path, max_length=limit + 1)
        except ValueError as e:
            return f"错误: {e}"
        if len(text) > limit:
            return text[:limit] + f"\n... (已截断, 共 {len(text)} 字符)"
        return text or "(文档为空)"

    def _bind(self, session: SimpleSession) -> None:
        self._session = session


class AsyncReadDocumentTool(AsyncTool):
    """读取文档并解析为纯文本 (异步)"""

    tool_name = "read_document"
    description = "读取文档并解析为纯文本, 支持 xlsx/docx/pdf/txt/md 等; 适合读取表格、文档、PDF 内容"
    params_dict = {
        "path": ("string", "文档路径 (绝对或相对工作区)"),
        "max_length": ("number", "返回文本最大长度, 默认 131072, 超出截断"),
    }

    def __init__(self, workspace_root: Path) -> None:
        """
        初始化 AsyncReadDocumentTool

        参数:
        - workspace_root: 工作区根目录
        """
        super().__init__()
        self._base_root = workspace_root
        """安装期基线工作区根 (cfg/全局); 项目会话经会话鸭子属性在调用时覆盖"""

    async def execute(self, path: str, max_length: int = 131072) -> str:
        """
        执行

        参数:
        - path: 路径
        - max_length: 最大长度

        返回:
        - str: 执行
        """
        try:
            abs_path = _resolve_doc_path(path, _doc_workspace_root(self), _doc_upload_root(self))
        except ValueError as e:
            return f"错误: {e}"
        try:
            limit = max(1, min(1_000_000, int(max_length)))
        except (TypeError, ValueError, OverflowError):
            return "错误: max_length 必须是整数"
        try:
            text = await asyncio.to_thread(extract_text, abs_path, max_length=limit + 1)
        except ValueError as e:
            return f"错误: {e}"
        if len(text) > limit:
            return text[:limit] + f"\n... (已截断, 共 {len(text)} 字符)"
        return text or "(文档为空)"

    def _bind(self, session: AsyncSimpleSession) -> None:
        self._session = session


# ================= memory (模型自驱增删改) =================


class _MemoryToolBase(Tool):
    """记忆工具基类: store 绑定"""

    def __init__(self, store: MemoryStore) -> None:
        """
        初始化 _MemoryToolBase

        参数:
        - store: 存储实例
        """
        super().__init__()
        self.store = store


class AddMemoryTool(_MemoryToolBase):
    """添加一条长期记忆 (用户偏好/项目约定/关键决策)"""

    tool_name = "add_memory"
    description = "添加一条当前会话的长期记忆, 记忆仅会注入当前会话的后续上下文"
    params_dict = {
        "title": ("string", "简短记忆标题"),
        "content": ("string", "记忆内容"),
        "tags": ("array", "分类标签"),
        "importance": ("number", "重要程度 1-5, 默认 1"),
    }

    def execute(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 1,
    ) -> str:
        """
        执行

        参数:
        - title: 标题
        - content: 内容
        - tags: 标签集合
        - importance: 重要度

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("添加")
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
        """
        执行

        参数:
        - memory_id: 记忆id
        - content: 内容
        - title: 标题

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("更新")
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
        """
        执行

        参数:
        - memory_id: 记忆id

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("删除")
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
        """
        执行

        返回:
        - str: 执行
        """
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
        """
        初始化 _AsyncMemoryToolBase

        参数:
        - store: 存储实例
        """
        super().__init__()
        self.store = store


class AsyncAddMemoryTool(_AsyncMemoryToolBase):
    """添加长期记忆 (异步)"""

    tool_name = "add_memory"
    description = "添加一条当前会话的长期记忆, 记忆仅会注入当前会话的后续上下文"
    params_dict = {
        "title": ("string", "简短记忆标题"),
        "content": ("string", "记忆内容"),
        "tags": ("array", "分类标签"),
        "importance": ("number", "重要程度 1-5, 默认 1"),
    }

    async def execute(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 1,
    ) -> str:
        """
        执行

        参数:
        - title: 标题
        - content: 内容
        - tags: 标签集合
        - importance: 重要度

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("添加")
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
        """
        执行

        参数:
        - memory_id: 记忆id
        - content: 内容
        - title: 标题

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("更新")
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
        """
        执行

        参数:
        - memory_id: 记忆id

        返回:
        - str: 执行
        """
        if not self.store.can_write():
            return self.store.write_denied_reason("删除")
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
        """
        执行

        返回:
        - str: 执行
        """
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
    """
    按会话形态构建全部工具 (注入配置: timeout/sandbox_root/workspace_root/memory)

    参数:
    - session: 会话
    - config: 配置信息

    返回:
    - list[Any]: 按会话形态构建全部工具 (注入配置: timeout/sandbox_root/workspace_root/memory)
    """
    cfg = config or {}
    timeout = int(cfg.get("search_timeout") or 10)
    # 项目会话: 会话鸭子属性 (ChatService 建会话时赋值) 优先于插件配置
    ws_override = safe_getattr(session, "coding_workspace_root")
    sb_override = safe_getattr(session, "coding_sandbox_root")
    sandbox_root = Path(str(sb_override or cfg.get("sandbox_root") or DEFAULT_SANDBOX_ROOT))
    workspace_root = Path(str(ws_override or cfg.get("workspace_root") or get_project_root()))

    state = get_plugin_state(session, cfg)
    store = state["store"]
    assert isinstance(store, MemoryStore)

    sandbox = CodeSandbox(str(sandbox_root), sys.executable)

    if isinstance(session, AsyncSimpleSession):
        tools: list[Any] = [
            AsyncSearchTool(timeout=timeout),
            AsyncFetchPageTool(timeout=timeout),
            AsyncCodeSandboxTool(sandbox, _make_async_execution_authorizer(session)),
            AsyncReadDocumentTool(workspace_root),
            AsyncAddMemoryTool(store),
            AsyncUpdateMemoryTool(store),
            AsyncDeleteMemoryTool(store),
            AsyncListMemoriesTool(store),
        ]
    else:
        tools = [
            SearchTool(timeout=timeout),
            FetchPageTool(timeout=timeout),
            CodeSandboxTool(sandbox, _make_sync_execution_authorizer(session)),
            ReadDocumentTool(workspace_root),
            AddMemoryTool(store),
            UpdateMemoryTool(store),
            DeleteMemoryTool(store),
            ListMemoriesTool(store),
        ]
    # 绑定会话 (read_document 等工具按会话解析工作区)
    for tool in tools:
        bind = safe_getattr_callable(tool, "_bind")
        if bind is not None:
            bind(session)
    return tools

