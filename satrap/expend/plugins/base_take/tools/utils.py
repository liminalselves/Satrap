"""
base_take 插件工具集: search / fetch_page / code_sandbox / read_document / memory

约定:
- get_tools(session, config) 工厂: 按会话形态返回同步/异步工具, 配置经合成后注入
- search/fetch_page 复用 expend.tools.search, timeout 从 config
- code_sandbox 复用 expend.tools.sandbox_tools, sandbox_root 从 config (全局共享目录)
- read_document 提取文档文本, 按模型能力分页渲染 PDF 页面
- memory 复用公共 MemoryStore (expend.tools.memory_store), scope 从 config
"""

from __future__ import annotations
import inspect
from pathlib import Path
from typing import Any, cast
from typing import Awaitable, Callable
from satrap.core.utils.paths import get_project_root
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
        numbered = "  ".join(
            f"{index}. {option}" for index, option in enumerate(options, 1)
        )
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
        question = (
            f"代码沙箱仅限制工作目录, 代码仍可访问系统资源. 是否允许{description}?"
        )
        return _execution_approved(
            _call_user_input_provider(provider, question, ["允许", "拒绝"])
        )

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
        question = (
            f"代码沙箱仅限制工作目录, 代码仍可访问系统资源. 是否允许{description}?"
        )
        answer = _call_user_input_provider(provider, question, ["允许", "拒绝"])
        if inspect.isawaitable(answer):
            answer = await cast(Awaitable[object], answer)
        return _execution_approved(answer)

    return authorize


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
