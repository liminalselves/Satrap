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


def get_satrapdata_dir() -> Path:
    """获取数据库文件存放目录 (.satrap/satrapdata)

    所有 .db 默认路径统一放该目录, 与配置/日志等其它 .satrap 内容分离
    """
    return get_data_dir() / "satrapdata"


def get_db_path(filename: str) -> str:
    """获取某个数据库文件的默认完整路径 (.satrap/satrapdata/<filename>)

    参数:
    - filename: 数据库文件名 (如 "chat_history.db")

    返回字符串路径, 供各 Manager 的 db_path 默认值使用;
    调用方如需 Path 对象可自行包装
    """
    return str(get_satrapdata_dir() / filename)
