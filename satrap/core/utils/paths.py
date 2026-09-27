"""路径工具函数 - 统一项目根目录和数据目录"""
from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path
import os


def get_project_root() -> Path:
    """
    获取项目根目录

    通过当前文件位置推断项目根目录(satrap 包的父目录)

    返回:
    - Path: 项目根目录
    """
    # satrap/core/utils/paths.py -> satrap/core/utils -> satrap/core -> satrap -> 项目根目录
    return Path(__file__).resolve().parent.parent.parent.parent


def get_data_dir() -> Path:
    """
    获取数据目录 (.satrap)

    统一使用项目根目录下的 .satrap 目录

    返回:
    - Path: 数据目录 (.satrap)
    """
    return get_project_root() / ".satrap"


def ensure_data_dir() -> Path:
    """
    确保数据目录存在并返回路径

    返回:
    - Path: 路径
    """
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_storage_dir() -> Path:
    """
    获取 v2 持久化数据根目录

    返回:
    - Path: `.satrap/data` 目录
    """
    return get_data_dir() / "data"


def get_db_path(*, platform_id: str = "local") -> str:
    """
    获取指定平台实例唯一的数据库路径

    参数:
    - platform_id: 平台实例 ID, 默认 `local`

    返回:
    - str: `.satrap/data/platforms/<platform-key>/platform.db`
    """
    from satrap.core.storage import default_storage_layout

    return str(default_storage_layout.platform_db(platform_id))


class MediaSourcePermissionError(PermissionError):
    """媒体来源路径位于白名单之外, 继承 PermissionError 以保持权限拒绝语义"""


@lru_cache(maxsize=4)
def _resolve_allowed_roots(extra_roots_raw: str) -> tuple[Path, ...]:
    """
    解析媒体白名单根目录, 以环境变量原始值为缓存键

    参数:
    - extra_roots_raw: SATRAP_EXTRA_MEDIA_ROOTS 的原始值, 变化时重新解析

    返回:
    - tuple[Path, ...]: 已解析的允许根目录, 含默认根目录
    """
    roots = [get_data_dir().resolve(), Path(tempfile.gettempdir()).resolve()]
    for item in extra_roots_raw.split(os.pathsep):
        if item.strip():
            roots.append(Path(item.strip()).expanduser().resolve())
    return tuple(roots)


def get_allowed_media_roots() -> list[Path]:
    """
    获取媒体组件允许读取的本机根目录

    默认只允许 `.satrap` 数据目录与系统临时目录; 例外场景通过环境变量
    `SATRAP_EXTRA_MEDIA_ROOTS` 追加, 多个路径用系统路径分隔符 (`;` 或 `:`) 隔开

    返回:
    - list[Path]: 已解析的允许根目录列表
    """
    return list(_resolve_allowed_roots(os.environ.get("SATRAP_EXTRA_MEDIA_ROOTS", "")))


def ensure_allowed_media_path(path: str) -> str:
    """
    校验本机媒体来源路径位于允许根目录内, 防止消息组件读取任意本地文件外发

    解析符号链接与 `..` 之后按真实路径校验; 白名单外抛出 MediaSourcePermissionError

    参数:
    - path: 本地路径

    返回:
    - str: 输入路径的绝对路径形式 (校验按解析后的真实路径执行)
    """
    resolved = Path(path).expanduser().resolve()
    for root in get_allowed_media_roots():
        if resolved.is_relative_to(root):
            return os.path.abspath(path)
    raise MediaSourcePermissionError(
        f"媒体来源不在允许目录内 (可用 SATRAP_EXTRA_MEDIA_ROOTS 追加): {resolved}"
    )
