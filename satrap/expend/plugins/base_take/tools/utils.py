"""
base_take 插件工具集: search / fetch_page / code_sandbox / read_document

约定:
- get_tools(session, config) 工厂: 按会话形态返回同步/异步工具, 配置经合成后注入
- search/fetch_page 复用 expend.tools.search, timeout 从 config
- code_sandbox 复用 expend.tools.sandbox_tools, sandbox_root 优先使用宿主会话私有目录, config 为独立调用兜底
- read_document 提取文档文本, 按模型能力分页渲染 PDF 页面
"""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from satrap.core.utils.paths import get_project_root
from satrap.core.type import safe_getattr, safe_getattr_callable
from satrap.edictum import AsyncSimpleSession, SimpleSession

if TYPE_CHECKING:
    from .base import _DocumentCore


SessionType = SimpleSession | AsyncSimpleSession

"""插件支持的会话类型"""

DEFAULT_SANDBOX_ROOT = get_project_root() / ".satrap" / "sandbox"

"""默认沙箱根目录 (全局唯一, 与 coding 插件共享)"""


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
    # Step.1 解析候选路径并校验当前会话可见范围
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
    # Step.2 仅在当前会话上传目录中尝试原文件名或带 UUID 的文件名
    if uploads is not None and uploads.is_dir():
        target_name = p.name.lower()
        for fpath in uploads.iterdir():
            if not fpath.is_file():
                continue
            fname = fpath.name.lower()
            if fname == target_name or fname.endswith(f"_{target_name}"):
                return fpath.resolve()
    return abs_path   # 未找到时由调用方报告文件不存在, 不扩展搜索范围


def _doc_workspace_root(tool: _DocumentCore) -> Path:
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


def _doc_upload_root(tool: _DocumentCore) -> Path | None:
    """
    获取当前会话独占 uploads 目录

    参数:
    - tool: 文档工具

    返回:
    - Path | None: 未注入会话存储时返回 None
    """
    override = safe_getattr(getattr(tool, "_session", None), "coding_upload_root")
    return Path(str(override)).resolve() if override else None
