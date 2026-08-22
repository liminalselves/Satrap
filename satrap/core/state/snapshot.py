"""快照编排: 构建, 恢复与引用重映射"""
from typing import Dict, List

from satrap.core.state.registry import DomainRegistry
from satrap.core.type import (
    JsonRow,
    RestoreOptions,
    SnapshotDomain,
    StateScope,
    StateSnapshot,
)
import sqlite3


def build_snapshot(
    conn: sqlite3.Connection,
    scope: StateScope,
    registry: DomainRegistry,
) -> StateSnapshot:
    """
    按注册顺序构建完整状态快照

    参数:
    - conn: 存储连接 (应在事务内)
    - scope: 状态作用域
    - registry: 领域注册表

    返回:
    - StateSnapshot: 完整快照
    """
    domains: Dict[str, List[JsonRow]] = {}
    for domain in registry.all():
        domains[domain.name] = domain.builder(conn, scope)
    return StateSnapshot(
        scope={
            "namespace": scope.namespace,
            "scope_id": scope.scope_id,
            "branch_id": scope.branch_id,
        },
        domains=domains,
    )


def restore_snapshot(
    conn: sqlite3.Connection,
    scope: StateScope,
    snapshot: StateSnapshot,
    registry: DomainRegistry,
    options: RestoreOptions,
) -> None:
    """
    把快照恢复到指定作用域: 先按序清理, 再按序恢复

    参数:
    - conn: 存储连接 (应在事务内)
    - scope: 目标作用域
    - snapshot: 要恢复的快照
    - registry: 领域注册表
    - options: 恢复选项 (preserve_ids / id_map)
    """
    for domain in registry.all():
        domain.cleaner(conn, scope)
    for domain in registry.all():
        rows = snapshot.domains.get(domain.name, [])
        domain.restorer(conn, scope, _remap_rows(rows, domain, options), options)


def build_id_map(
    registry: DomainRegistry,
    snapshot: StateSnapshot,
    new_scope: StateScope,
) -> Dict[str, str]:
    """
    构建引用字段的旧值 -> 新值映射表

    新值格式: {新作用域 ID}:{旧值}, 保证 fork 后引用唯一且可读

    参数:
    - registry: 领域注册表
    - snapshot: 源快照
    - new_scope: 新作用域

    返回:
    - Dict[str, str]: 旧值到新值的映射
    """
    old_values: set[str] = set()
    for domain in registry.all():
        for field in domain.reference_fields:
            for row in snapshot.domains.get(domain.name, []):
                value = row.get(field)
                if isinstance(value, str) and value:
                    old_values.add(value)
    prefix = new_scope.scope_id
    return {value: f"{prefix}:{value}" for value in sorted(old_values)}


def _remap_rows(
    rows: List[JsonRow],
    domain: SnapshotDomain,
    options: RestoreOptions,
) -> List[JsonRow]:
    """
    按领域声明的引用字段与 id_map 改写行数据 (仅 fork 时生效)

    参数:
    - rows: 数据行集合
    - domain: 数据领域
    - options: 选项集合

    回滚 (preserve_ids=True) 时原样返回, 保证引用不被改写

    返回:
    - List[JsonRow]: 按领域声明的引用字段与 id_map 改写行数据 (仅 fork 时生效)
    """
    if options.preserve_ids or not domain.reference_fields or not options.id_map:
        return rows
    remapped: List[JsonRow] = []
    for row in rows:
        new_row: JsonRow = dict(row)
        for field in domain.reference_fields:
            value = new_row.get(field)
            if isinstance(value, str) and value in options.id_map:
                new_row[field] = options.id_map[value]
        remapped.append(new_row)
    return remapped
