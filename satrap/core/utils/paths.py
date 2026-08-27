"""路径工具函数 - 统一项目根目录和数据目录"""
from __future__ import annotations

from pathlib import Path


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
