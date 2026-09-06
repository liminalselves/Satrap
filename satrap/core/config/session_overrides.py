"""通用会话参数覆盖: SQLite 持久化, 配置域隔离和乐观并发控制"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any
from copy import deepcopy
import json
import time

from satrap.core.storage.file_lock import database_session_lock


class OverrideConflictError(ValueError):
    """覆盖配置已被其他请求更新, 调用方需要重新读取"""


def ensure_override_tables(connection: sqlite3.Connection) -> None:
    """创建覆盖记录表, 空记录保留修订号以避免恢复继承后的并发覆盖"""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS session_config_overrides ("
        "session_id TEXT NOT NULL, namespace TEXT NOT NULL, "
        "config_json TEXT NOT NULL DEFAULT '{}', schema_version INTEGER NOT NULL DEFAULT 1, "
        "revision INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL, "
        "PRIMARY KEY (session_id, namespace))"
    )


class SessionOverrideStore:
    """按平台数据库中的会话和配置域保存显式覆盖, 不创建会话文件夹"""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            ensure_override_tables(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _validate_identity(session_id: str, namespace: str) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("会话 ID 不能为空")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("配置域不能为空")
        if len(namespace) > 200 or "\x00" in namespace or "\x00" in session_id:
            raise ValueError("非法的会话或配置域标识")

    def read(self, session_id: str, namespace: str) -> dict[str, Any]:
        """读取显式覆盖及修订号, 不存在时返回空覆盖和修订号零"""
        self._validate_identity(session_id, namespace)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM session_config_overrides WHERE session_id=? AND namespace=?",
                (session_id, namespace),
            ).fetchone()
        if row is None:
            return {"overrides": {}, "revision": 0, "schema_version": 1, "updated_at": None}
        config = json.loads(row["config_json"])
        if not isinstance(config, dict):
            raise ValueError(f"会话覆盖数据损坏: {namespace}")
        return {
            "overrides": config, "revision": row["revision"],
            "schema_version": row["schema_version"], "updated_at": row["updated_at"],
        }

    def replace(
        self, session_id: str, namespace: str, values: Mapping[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        """原子替换一个配置域的显式字段, 必须携带最近读取的修订号"""
        self._validate_identity(session_id, namespace)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision 必须是非负整数")
        if not isinstance(values, Mapping) or any(not isinstance(key, str) for key in values):
            raise ValueError("覆盖配置必须是字符串键对象")
        encoded = json.dumps(dict(values), ensure_ascii=False, allow_nan=False)
        updated_at = time.time()
        with database_session_lock(self.database, session_id), closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM session_config_overrides WHERE session_id=? AND namespace=?",
                (session_id, namespace),
            ).fetchone()
            revision = int(row[0]) if row else 0
            if revision != expected_revision:
                raise OverrideConflictError("会话配置已更新, 请刷新后重试")
            if revision == 0 and not values:
                return {"overrides": {}, "revision": 0, "schema_version": 1, "updated_at": None}
            revision += 1
            connection.execute(
                "INSERT INTO session_config_overrides "
                "(session_id, namespace, config_json, schema_version, revision, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(session_id, namespace) DO UPDATE SET "
                "config_json=excluded.config_json, revision=excluded.revision, updated_at=excluded.updated_at",
                (session_id, namespace, encoded, revision, updated_at),
            )
        return {"overrides": json.loads(encoded), "revision": revision, "schema_version": 1, "updated_at": updated_at}


class SessionOverrideService:
    """可复用的配置域模板, 验证器只接收显式覆盖, 解析结果携带字段来源"""

    def __init__(self, store: SessionOverrideStore) -> None:
        self.store = store
        self._validators: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def register(self, namespace: str, validator: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        """注册配置域校验器, 未注册的配置域不能读写"""
        self._validators[namespace] = validator

    def resolve(
        self, session_id: str, namespace: str, layers: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        """逐字段覆盖, 数组与对象整体替换, 不把生效配置持久化"""
        validator = self._validators.get(namespace)
        if validator is None:
            raise ValueError(f"未注册配置域: {namespace}")
        record = self.store.read(session_id, namespace)
        overrides = validator(deepcopy(record["overrides"]))
        effective: dict[str, Any] = {}
        sources: dict[str, str] = {}
        all_layers: list[tuple[str, Mapping[str, Any]]] = [*layers, ("session", overrides)]
        for source, values in all_layers:
            for key, value in values.items():
                effective[key] = deepcopy(value)
                sources[key] = source
        return {**record, "config": effective, "sources": sources}

    def save(
        self, session_id: str, namespace: str, values: dict[str, Any], *, expected_revision: int,
    ) -> dict[str, Any]:
        """校验后保存显式覆盖, 删除字段即恢复继承"""
        validator = self._validators.get(namespace)
        if validator is None:
            raise ValueError(f"未注册配置域: {namespace}")
        return self.store.replace(
            session_id, namespace, validator(deepcopy(values)), expected_revision=expected_revision,
        )
