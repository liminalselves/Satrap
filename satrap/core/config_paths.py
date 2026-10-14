"""
集中管理 JSON 配置路径

为模型, Agent, Chat, 插件和日志提供统一默认目录,
通过跨进程锁迁移旧版文件, 故障时保留原配置并记录诊断
"""
from __future__ import annotations

import traceback
from pathlib import Path
import os

def _data_dir() -> Path:
    """按模块位置返回项目 .satrap, 避免日志初始化依赖通用工具包"""
    return Path(__file__).resolve().parents[2] / ".satrap"


def get_config_dir() -> Path:
    """
    获取集中配置目录

    返回:
    - 环境变量指定的目录或项目 .satrap/config
    """
    override = os.getenv("SATRAP_CONFIG_ROOT")
    return Path(override) if override else _data_dir() / "config"


def get_config_path(name: str, *, legacy_name: str | None = None) -> Path:
    """
    返回默认配置路径, 首次访问时迁移旧文件或插件配置目录

    参数:
    - name: 新目录内的文件名或目录名
    - legacy_name: 旧版 .satrap 内的名称, 默认与 name 相同

    返回:
    - 新配置路径; 迁移失败时记录原因并保留旧路径, 避免重建空配置
    """
    old_name = legacy_name or name
    if any(Path(value).name != value or value in {"", ".", ".."} for value in (name, old_name)):
        raise ValueError("配置名称必须是单个文件名或目录名")
    target = get_config_dir() / name
    legacy = _data_dir() / old_name
    if os.getenv("SATRAP_CONFIG_ROOT") or target.exists() or not legacy.exists():
        return target
    return migrate_legacy_path(legacy, target)


def migrate_legacy_path(legacy: Path, target: Path, *, require_idle: bool = False) -> Path:
    """
    串行迁移旧文件, 已有目标优先, 失败时保留旧路径

    参数:
    - legacy: 已知旧版文件或目录
    - target: 新分类目录下的目标
    - require_idle: 锁文件必须先确认没有操作系统锁持有者

    返回:
    - 新位置, 或迁移失败时仍可读取的旧位置
    """
    if target.exists() or not legacy.exists():
        return target
    from satrap.core.log.managed import LogFileLock, report_failure

    lock = LogFileLock(target.parent / ".migration.lock")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not lock.acquire(5):
            raise TimeoutError("配置迁移正在被其它进程使用")
        if not target.exists() and legacy.exists():
            if legacy.is_symlink() or legacy.resolve() != legacy.absolute():
                raise ValueError("旧配置路径不能被链接重定向")
            if target.parent.resolve() != target.parent.absolute():
                raise ValueError("新配置目录不能被链接重定向")
            if require_idle:
                idle_lock = LogFileLock(legacy)
                if not idle_lock.acquire():
                    raise TimeoutError("旧运行锁仍被进程持有")
                idle_lock.release()
            legacy.rename(target)
        return target
    except Exception as error:
        report_failure(f"配置迁移失败 ({legacy.name} -> {target.parent.name}/{target.name}): {error}\n{traceback.format_exc()}")
        return target if target.exists() else legacy
    finally:
        try:
            lock.release()
        except Exception as error:
            report_failure(f"配置迁移锁释放失败: {error}\n{traceback.format_exc()}")
