"""
会话审批状态的宿主持有与旧文件迁移

审批状态与插件生命周期分离, 卸载执行插件不改变已保存模式;
旧状态和日志只在目标文件缺失时复制, 保留原文件用于回溯
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from pathlib import Path

from satrap.core.plugin_interop.environment import environment_snapshot
from satrap.core.storage.file_lock import FileLock
from satrap.core.approval.policy import PermissionEngine
from satrap.core.utils.paths import get_project_root
from satrap.core.storage import storage_key

from satrap.core.log import logger


_LOCK = RLock()


@dataclass
class SessionApproval:
    """只挂在实际会话上的审批引擎, 不使用全局会话 ID 注册表"""

    engine: PermissionEngine
    root: Path


def _configuration(session: object) -> Mapping[str, object] | None:
    """
    只读取成功发布的权限插件配置, 安装失败不改变宿主策略

    参数:
    - session: 实际宿主会话

    返回:
    - 已启用权限插件的配置, 尚未成功安装时返回 None
    """
    from satrap.core.plugin_authorization import PluginEntryBinding

    plugins = getattr(session, "_plugins", {})
    plugin = plugins.get("permissions") if isinstance(plugins, Mapping) else None
    if plugin is None or not getattr(plugin, "enabled", False):
        return None
    registry = getattr(session, "command_handler", None) or getattr(session, "cmd_handler", None)
    binding = getattr(registry, "_permission_bindings", {}).get("approve")
    return binding.config if isinstance(binding, PluginEntryBinding) and binding.plugin_name == "permissions" else None


def validate_approval_root(session: object, data_root: Path | None) -> None:
    """
    安装期只验证目录绑定, 不创建状态或修改审批模型

    参数:
    - session: 实际宿主会话
    - data_root: 待安装的配置目录, None 表示宿主默认目录
    """
    current = getattr(session, "_approval_state", None)
    if isinstance(current, SessionApproval) and current.root != (data_root or approval_root(session)).resolve():
        raise ValueError("会话审批目录已绑定, 更改目录后请重启会话")


def approval_root(session: object) -> Path:
    """
    推导宿主会话的审批目录

    参数:
    - session: 实际宿主会话

    返回:
    - 隔离于 coding 插件的默认审批目录
    """
    snapshot = environment_snapshot(session)
    if snapshot.session_root is not None:
        return snapshot.session_root / "cache" / "permissions"
    context = getattr(session, "session_ctx", None)
    database = getattr(context, "db_path", None)
    key = storage_key(str(getattr(session, "session_id", "session")), fallback="session")
    if database:
        return Path(str(database)).resolve().parent / "sessions" / key / "cache" / "permissions"
    return get_project_root() / ".satrap" / "data" / "unscoped" / key / "permissions"


def _copy_legacy(root: Path, legacy: Path) -> bool:
    """
    在目录锁内复制缺失的审批文件, 不覆盖新状态

    参数:
    - root: 当前宿主审批目录
    - legacy: 旧 coding 数据目录

    返回:
    - 是否复制了旧模式文件, 迁移失败由安装或请求边界记录并拒绝
    """
    if root == legacy or not legacy.is_dir():
        return False
    root.mkdir(parents=True, exist_ok=True)
    copied_mode = False
    with FileLock(root / ".migration.lock"):
        for name in ("permissions.json", "approval_log.jsonl", "permissions.legacy-v1.json"):
            source, target = legacy / name, root / name
            if not target.exists() and source.is_file():
                temporary = target.with_suffix(target.suffix + ".migrating")
                temporary.write_bytes(source.read_bytes())
                temporary.replace(target)
                copied_mode |= name == "permissions.json"
                logger.info(f"[审批迁移] 已保留并迁移旧文件, 来源={source}, 目标={target}")
    return copied_mode


def session_approval(session: object, *, data_root: Path | None = None,
                     legacy_root: Path | None = None) -> PermissionEngine:
    """
    获取会话唯一审批引擎, 默认保留旧 coding 状态与日志

    参数:
    - session: 实际宿主会话, 相同 ID 的不同对象仍分别持有状态
    - data_root: 权限插件显式指定的持久目录, 初始化后不允许改绑
    - legacy_root: coding 提供的旧数据目录, 仅用于一次性文件迁移

    返回:
    - 会话共享引擎; 目录冲突或迁移失败抛出异常供业务边界处理
    """
    with _LOCK:
        current = getattr(session, "_approval_state", None)
        config = _configuration(session)
        if config is not None and data_root is None:
            configured = str(config.get("data_root") or "").strip()
            data_root = Path(configured) if configured else None
        root = (data_root or approval_root(session)).resolve()
        if isinstance(current, SessionApproval):
            if (data_root is not None or config is not None) and current.root != root:
                raise ValueError("会话审批目录已绑定, 更改目录后请重启会话")
            if legacy_root is not None and _copy_legacy(current.root, legacy_root.resolve()):
                current.engine._load()
                current.engine.revision += 1   # 延迟迁移同样撤销已创建的旧审批请求
            if config is not None:
                current.engine.set_approval_model(str(config.get("approval_model") or "").strip())
            return current.engine
        default = approval_root(session)
        context = getattr(session, "session_ctx", None)
        scoped = environment_snapshot(session).session_root is not None or getattr(context, "db_path", None)
        default_legacy = default.with_name("satrap_coding") if scoped else default.parent
        _copy_legacy(root, (legacy_root or default_legacy).resolve())
        engine = PermissionEngine(rules_file=root / "permissions.json", log_file=root / "approval_log.jsonl")
        if config is not None:
            engine.set_approval_model(str(config.get("approval_model") or "").strip())
        setattr(session, "_approval_state", SessionApproval(engine, root))
        return engine
