"""
base_take 插件工具集: search / fetch_page / code_sandbox / read_document

约定:
- get_tools(session, config) 工厂: 按会话形态返回同步/异步工具, 配置经合成后注入
- search/fetch_page 复用 expend.tools.search, timeout 从 config
- code_sandbox 复用 expend.tools.sandbox_tools, sandbox_root 优先使用宿主会话私有目录, config 为独立调用兜底
- read_document 读取本地图片, 提取文档文本, 按模型能力分页渲染 PDF 页面
"""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

from satrap.core.plugin_interop.environment import EnvironmentSnapshot
from satrap.core.utils.paths import get_project_root
from satrap.edictum import AsyncSimpleSession, SimpleSession

if TYPE_CHECKING:
    from .base import _DocumentCore


SessionType = SimpleSession | AsyncSimpleSession

"""插件支持的会话类型"""

DEFAULT_SANDBOX_ROOT = get_project_root() / ".satrap" / "sandbox"

"""默认沙箱根目录 (全局唯一, 与 coding 插件共享)"""


def _doc_workspace_root(tool: _DocumentCore, snapshot: EnvironmentSnapshot | None = None) -> Path:
    """
    文档工具优先使用冻结宿主工作区, 未绑定时使用安装期基线

    参数:
    - tool: 工具
    - snapshot: 当前操作的冻结快照, 默认重新查询宿主环境

    返回:
    - 文档工具的工作区根, 宿主绑定优先, 否则使用安装期基线
    """
    environment = snapshot if snapshot is not None else tool._environment()
    return environment.workspace_root or tool._base_root
