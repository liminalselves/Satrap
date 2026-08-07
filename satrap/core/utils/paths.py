"""路径工具函数 - 统一项目根目录和数据目录"""
from __future__ import annotations

from pathlib import Path


def get_project_root() -> Path:
    """获取项目根目录
    
    通过当前文件位置推断项目根目录（satrap 包的父目录）
    """
    # satrap/core/utils/paths.py -> satrap/core/utils -> satrap/core -> satrap -> 项目根目录
    return Path(__file__).resolve().parent.parent.parent.parent


def get_data_dir() -> Path:
    """获取数据目录 (.satrap)
    
    统一使用项目根目录下的 .satrap 目录
    """
    return get_project_root() / ".satrap"


def ensure_data_dir() -> Path:
    """确保数据目录存在并返回路径"""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
