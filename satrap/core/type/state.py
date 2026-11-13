"""状态快照、检查点与恢复契约"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, cast
import sqlite3


# ==================== State (检查点 / 回滚 / 分支) ====================

CURRENT_SNAPSHOT_VERSION = 1
"""当前状态快照格式版本"""
SUPPORTED_SNAPSHOT_VERSIONS = {CURRENT_SNAPSHOT_VERSION}
"""支持读取的快照格式版本集合"""

JsonRow = Dict[str, object]
"""领域数据行: 可 JSON 序列化的字段字典, 是快照的持久化单元"""


@dataclass(frozen=True)
class StateScope:
    """状态作用域: 定位一份可被检查点管理的状态"""
    namespace: str
    """命名空间, 如 "conversation" (对话) / "session" (会话)"""
    scope_id: str
    """作用域 ID, 如 conversation_id / session_id"""
    branch_id: str = ""
    """分支 ID, 由 fork 产生的剧情线; 无分支时为空字符串"""


@dataclass
class StateSnapshot:
    """版本化状态快照: 各领域数据以行字典列表存放"""
    version: int = CURRENT_SNAPSHOT_VERSION
    """快照格式版本"""
    scope: Dict[str, str] = field(default_factory=dict[str, str])
    """快照冻结的命名空间, 作用域和分支身份, 用于恢复时定位目标"""
    domains: Dict[str, List[JsonRow]] = field(default_factory=dict[str, List[JsonRow]])
    """按领域名保存的可序列化数据行, 恢复时交由对应 restorer 处理"""

    @classmethod
    def from_dict(cls, value: Dict[str, object]) -> "StateSnapshot":
        """
        从持久化字典读取快照, 校验版本与结构

        参数:
        - value: 持久化字典 (to_dict 的输出)

        返回:
        - StateSnapshot: 解析后的快照

        异常:
        - ValueError: 版本不支持或结构不完整
        - TypeError: 字段类型错误
        """
        version = value.get("snapshot_version")
        if not isinstance(version, int) or version not in SUPPORTED_SNAPSHOT_VERSIONS:
            raise ValueError(
                f"不支持的状态快照版本: {version!r}, 当前版本为 {CURRENT_SNAPSHOT_VERSION}"
            )
        scope = value.get("scope")
        if not isinstance(scope, dict):
            raise TypeError("快照 scope 必须是字典")
        raw_domains = cast(dict[str, Any], value.get("domains"))
        if not isinstance(raw_domains, dict):
            raise TypeError("快照 domains 必须是字典")
        domains: Dict[str, List[JsonRow]] = {}
        for name, rows in raw_domains.items():
            if not isinstance(name, str) or not isinstance(rows, list):
                raise TypeError(f"快照领域 {name!r} 必须是行列表")
            rows = cast(list[Any], rows)
            domains[name] = [cast(JsonRow, row) for row in rows if isinstance(row, dict)]
        return cls(version=CURRENT_SNAPSHOT_VERSION, scope={**scope}, domains=domains)

    def to_dict(self) -> Dict[str, object]:
        """
        转换为稳定的持久化字典

        返回:
        - Dict[str, object]: 转换为稳定的持久化字典
        """
        return {
            "snapshot_version": self.version,
            "scope": dict(self.scope),
            "domains": {name: list(rows) for name, rows in self.domains.items()},
        }


@dataclass
class StateCheckpoint:
    """状态检查点: 某一时刻的完整状态快照引用"""
    checkpoint_id: str
    """检查点唯一标识"""
    namespace: str
    """所属命名空间"""
    scope_id: str
    """所属作用域 ID"""
    branch_id: str = ""
    """所属分支 ID"""
    name: str = ""
    """检查点显示名称"""
    description: str = ""
    """检查点说明"""
    snapshot_id: str = ""
    """引用的独立快照 ID"""
    batch_id: str = ""
    """会话级聚合检查点的批次 ID (同一批检查点共享), 非聚合时为空"""
    state_revision: int = 0
    """检查点创建时的状态版本"""
    position: int = 0
    """检查点创建时的领域水位 (如最大消息 ID)"""
    checkpoint_kind: str = "manual"
    """检查点类型, manual (手动) 或 stable (自动)"""
    parent_checkpoint_id: Optional[str] = None
    """fork 来源检查点 ID"""
    source: str = ""
    """创建时的变更来源 (审计), 如 session_checkpoint / checkpoint_fork"""
    reason: str = ""
    """创建时的变更原因 (审计)"""
    created_at: float = 0.0
    """创建时间戳"""


@dataclass(frozen=True)
class RestoreOptions:
    """恢复选项: 由框架统一传给各领域恢复器"""
    preserve_ids: bool
    """True=原位恢复 (回滚), False=新作用域恢复 (fork, 引用需重映射)"""
    id_map: Dict[str, str] = field(default_factory=dict[str, str])
    """引用字段的旧值 -> 新值映射表, fork 时由框架构建"""


@dataclass(frozen=True)
class SnapshotDomain:
    """领域注册声明: 框架与领域数据的唯一契约"""
    name: str
    """领域名称, 如 \"messages\" / \"session_config\""""
    builder: Callable[[sqlite3.Connection, StateScope], List[JsonRow]]
    """读取领域数据, 返回行字典列表"""
    restorer: Callable[[sqlite3.Connection, StateScope, List[JsonRow], RestoreOptions], None]
    """恢复领域数据 (清空后重写)"""
    cleaner: Callable[[sqlite3.Connection, StateScope], None]
    """恢复前清理领域数据"""
    reference_fields: Tuple[str, ...] = ()
    """fork 时需要重映射的跨领域引用字段名"""
    position_provider: Optional[Callable[[sqlite3.Connection, StateScope], int]] = None
    """提供领域水位 (如最大消息 ID), 用于检查点排序与级联清理"""


@dataclass(frozen=True)
class MutationContext:
    """一次原子状态变更的审计上下文"""
    source: str = "manual"
    """变更来源, 如 \"checkpoint_rollback\" / \"checkpoint_fork\""""
    reason: str = ""
    """变更原因说明"""
    change_set_id: str = ""
    """变更批次唯一标识"""
