"""
运行标识与凭据的分类路径

凭据保存到 credentials, 进程记录与实例锁保存到 runtime,
兼容旧版根目录文件, 活动锁迁移失败时继续使用原锁
"""
from pathlib import Path
import os

from satrap.core.config_paths import _data_dir, migrate_legacy_path


def _categorized_path(name: str, category: str, *, data_dir: Path | None = None, require_idle: bool = False) -> Path:
    """
    返回分类路径并迁移旧版文件

    参数:
    - name: 固定文件名
    - category: credentials 或 runtime
    - data_dir: 显式项目数据目录, 缺省按模块位置推断
    - require_idle: 迁移前检查旧锁未被占用

    返回:
    - 新分类路径, 失败时返回旧文件路径
    """
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("运行文件名称必须是单个文件名")
    root = data_dir if data_dir is not None else _data_dir()
    override = os.getenv(f"SATRAP_{category.upper()}_ROOT") if data_dir is None else None
    target = (Path(override) if override else root / category) / name
    return target if override else migrate_legacy_path(root / name, target, require_idle=require_idle)


def get_credential_path(name: str = "api-token", *, data_dir: Path | None = None) -> Path:
    """
    获取持久凭据位置

    参数:
    - name: 凭据文件名, 默认为 api-token
    - data_dir: 显式项目数据目录

    返回:
    - 默认 .satrap/credentials 内的路径
    """
    return _categorized_path(name, "credentials", data_dir=data_dir)


def get_runtime_path(name: str, *, data_dir: Path | None = None, require_idle: bool = False) -> Path:
    """
    获取进程标识及实例锁位置

    参数:
    - name: PID, 运行记录或实例锁文件名
    - data_dir: 显式项目数据目录
    - require_idle: 锁文件迁移前检查未被占用

    返回:
    - 默认 .satrap/runtime 内的路径
    """
    return _categorized_path(name, "runtime", data_dir=data_dir, require_idle=require_idle)
