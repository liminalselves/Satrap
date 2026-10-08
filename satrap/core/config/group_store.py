"""
逐群接入和显式配置的事务存储

账号身份由调用方根据可信连接确认, 本模块仅接受规范身份并按平台数据库隔离,
旧群字段只在首次确认账号时采用一次, 后续由群表作为权威配置
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
import hashlib
import json
import re
import sqlite3
import time

from satrap.core.config.platform_policy import normalize_group_whitelist
from satrap.core.config.platform_schema import ensure_platform_tables
from satrap.core.config.group_policy import policy_values
from satrap.core.config.group_session import session_values


_IDENTITY = re.compile(r"[1-9][0-9]*\Z")
_SECTIONS = frozenset({"policy", "session", "events", "approval"})
GROUP_APPROVAL_ACTIONS = frozenset({
    "recall_message", "kick_group_member", "ban_group_member", "set_group_whole_ban",
    "ban_anonymous", "set_group_admin", "set_group_anonymous", "set_group_card",
    "set_group_name", "set_group_special_title", "leave_group", "handle_group_request",
})
"""逐群审批允许配置的有明确群目标的管理写动作, 不含好友请求或私聊操作"""
HIGH_IMPACT_ACTIONS = frozenset({
    "kick_group_member", "set_group_admin", "set_group_whole_ban", "leave_group",
})
"""新配置中默认需要审批的高影响群管理动作"""

from satrap.core.config.group_events import event_values


class GroupConfigConflict(ValueError):
    """群配置修订已变化, 调用方需要重新读取"""


class GroupLegacyConflict(ValueError):
    """已采用的旧群配置再次被修改, 需要显式导入"""


@dataclass(frozen=True)
class GroupRuntimeSnapshot:
    """同一 SQLite 读取事务中的账号群状态"""

    self_id: str
    mode: str
    exceptions: dict[str, bool]
    policies: dict[str, dict[str, object]]
    revisions: dict[str, int]
    routes: dict[str, tuple[dict[str, object], int]]
    events: dict[str, dict[str, bool]]
    membership: dict[str, str]
    account_revision: int


def _identity(self_id: str, group_id: str | None = None) -> None:
    """
拒绝空账号和非规范群号

    参数:
    - self_id: 已核验的机器人账号
    - group_id: 可选的目标群号
    """
    if not isinstance(self_id, str) or not _IDENTITY.fullmatch(self_id):
        raise ValueError("self_id 必须是已确认的正整数账号")
    if group_id is not None and (not isinstance(group_id, str) or not _IDENTITY.fullmatch(group_id)):
        raise ValueError("group_id 必须是规范的正整数群号")


def legacy_group_fingerprint(settings: Mapping[str, object]) -> str:
    """
计算旧群字段的稳定摘要, 不纳入无关平台配置

    参数:
    - settings: 已校验的平台 settings

    返回:
    - 白名单和旧群策略的 SHA-256 摘要
    """
    groups = normalize_group_whitelist(settings.get("group_whitelist", []))
    overrides = settings.get("wake_group_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("wake_group_overrides 必须是对象")
    encoded = json.dumps(
        {"group_whitelist": sorted(groups), "wake_group_overrides": overrides},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class GroupConfigStore:
    """在单个平台数据库中按机器人账号和群号保存接入模式与显式配置"""

    def __init__(self, database: str | Path) -> None:
        """
        初始化群配置存储并执行向前结构迁移

        参数:
        - database: 服务端解析的平台数据库路径
        """
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_platform_tables(connection)

    def _connect(self) -> sqlite3.Connection:
        """
        打开配置数据库连接

        返回:
        - 启用行名访问的 SQLite 连接
        """
        connection = sqlite3.connect(str(self.database), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def read_account(self, self_id: str) -> dict[str, Any] | None:
        """
        读取已采用账号的群接入模式

        参数:
        - self_id: 已确认的机器人账号

        返回:
        - 账号设置和修订号; 尚未采用时返回 None
        """
        _identity(self_id)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM group_accounts WHERE self_id=?", (self_id,)).fetchone()
        if row is None:
            return None
        return {
            "self_id": self_id, "mode": row["mode"],
            "approval_defaults": json.loads(row["approval_defaults_json"]),
            "revision": int(row["revision"]), "migrated_at": float(row["migrated_at"]),
            "last_bound_at": float(row["last_bound_at"]),
            "legacy_adopted": row["legacy_source_fingerprint"] is not None,
        }

    def list_accounts(self) -> list[dict[str, Any]]:
        """
        列出本平台已确认的机器人账号供当前和历史数据选择

        返回:
        - 按最近绑定时间降序排列的账号摘要
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT self_id, mode, revision, last_bound_at FROM group_accounts "
                "ORDER BY last_bound_at DESC, self_id",
            ).fetchall()
        return [
            {"self_id": str(row["self_id"]), "mode": str(row["mode"]),
             "revision": int(row["revision"]), "last_bound_at": float(row["last_bound_at"])}
            for row in rows
        ]

    def adopt_legacy(self, self_id: str, settings: Mapping[str, object]) -> dict[str, Any]:
        """
        首次确认账号时原子采用旧群字段, 已采用后检查旧字段未被改写

        参数:
        - self_id: 可信连接确认的机器人账号
        - settings: 已校验的平台 settings, 新格式由 group_management_version 标识

        返回:
        - 采用后的账号接入模式与修订号
        """
        _identity(self_id)
        new_format = settings.get("group_management_version") == 1
        groups = [] if new_format else normalize_group_whitelist(settings.get("group_whitelist", []))
        raw_overrides = {} if new_format else settings.get("wake_group_overrides", {})
        if not isinstance(raw_overrides, dict):
            raise ValueError("wake_group_overrides 必须是对象")
        overrides = cast(dict[str, object], raw_overrides)
        fingerprint = None if new_format else legacy_group_fingerprint(settings)
        mode = "selected" if new_format or groups else "all"
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            original = connection.execute(
                "SELECT self_id FROM group_legacy_adoption WHERE singleton=1",
            ).fetchone()
            if not new_format and original is not None and original["self_id"] != self_id:
                groups, overrides, fingerprint, mode = [], {}, None, "selected"
            row = connection.execute(
                "SELECT legacy_source_fingerprint FROM group_accounts WHERE self_id=?", (self_id,),
            ).fetchone()
            if row is not None:
                if row["legacy_source_fingerprint"] != fingerprint:
                    raise GroupLegacyConflict("已采用的旧群字段发生变化, 需要显式导入")
                connection.execute(
                    "UPDATE group_accounts SET last_bound_at=? WHERE self_id=?", (now, self_id),
                )
            else:
                if fingerprint is not None and original is None:
                    connection.execute(
                        "INSERT INTO group_legacy_adoption (singleton, self_id, source_fingerprint, adopted_at) "
                        "VALUES (1, ?, ?, ?)", (self_id, fingerprint, now),
                    )
                connection.execute(
                    "INSERT INTO group_accounts (self_id, mode, approval_defaults_json, revision, "
                    "legacy_source_fingerprint, migrated_at, last_bound_at) "
                    "VALUES (?, ?, '{}', 1, ?, ?, ?)",
                    (self_id, mode, fingerprint, now, now),
                )
                for group_id in sorted(set(groups) | set(overrides)):
                    _identity(self_id, group_id)
                    policy: dict[str, object] = {}
                    if group_id in groups:
                        policy["enabled"] = {"mode": "value", "value": True}
                    if group_id in overrides:
                        raw_policy = overrides[group_id]
                        if not isinstance(raw_policy, dict):
                            raise ValueError("旧群策略必须是对象")
                        for key, value in cast(dict[str, object], raw_policy).items():
                            policy[key] = {"mode": "value", "value": value}
                    connection.execute(
                        "INSERT INTO group_configs "
                        "(self_id, group_id, config_json, schema_version, revision, route_generation, updated_at) "
                        "VALUES (?, ?, ?, 1, 1, 0, ?)",
                        (self_id, group_id, json.dumps({"policy": policy}, ensure_ascii=False, allow_nan=False), now),
                    )
        result = self.read_account(self_id)
        if result is None:
            raise RuntimeError("账号采用后无法读取")
        return result

    def check_legacy_source(self, self_id: str, settings: Mapping[str, object]) -> None:
        """
        检查已采用账号的旧字段未发生未经导入的变化

        参数:
        - self_id: 已确认的机器人账号
        - settings: 当前平台 settings
        """
        _identity(self_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT legacy_source_fingerprint FROM group_accounts WHERE self_id=?", (self_id,),
            ).fetchone()
            original = connection.execute(
                "SELECT self_id FROM group_legacy_adoption WHERE singleton=1",
            ).fetchone()
        if row is None:
            return
        first_account = original is None or original["self_id"] == self_id
        fingerprint = (
            legacy_group_fingerprint(settings)
            if settings.get("group_management_version") != 1 and first_account else None
        )
        if row["legacy_source_fingerprint"] != fingerprint:
            raise GroupLegacyConflict("已采用的旧群字段发生变化, 需要显式导入")

    def patch_account(
        self, self_id: str, *, expected_revision: int, mode: str, approval_defaults: Mapping[str, object],
    ) -> dict[str, Any]:
        """
        乐观并发更新账号接入模式和管理动作默认审批策略

        参数:
        - self_id: 已采用的机器人账号
        - expected_revision: 最近读取的账号设置修订号
        - mode: selected 或 all
        - approval_defaults: 已由服务层校验的按动作审批默认值

        返回:
        - 更新后的账号设置
        """
        _identity(self_id)
        if mode not in {"selected", "all"}:
            raise ValueError("mode 必须是 selected 或 all")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision 必须是正整数")
        if any(
            action not in GROUP_APPROVAL_ACTIONS or not isinstance(mode_value, str)
            or mode_value not in {"approval_required", "auto_execute"}
            for action, mode_value in approval_defaults.items()
        ):
            raise ValueError("审批设置包含非群目标动作或未知执行模式")
        encoded = json.dumps(dict(approval_defaults), ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE group_accounts SET mode=?, approval_defaults_json=?, revision=revision+1 "
                "WHERE self_id=? AND revision=?",
                (mode, encoded, self_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise GroupConfigConflict("账号接入设置已变化, 请刷新后重试")
        result = self.read_account(self_id)
        if result is None:
            raise RuntimeError("账号设置更新后无法读取")
        return result

    def read_group(self, self_id: str, group_id: str) -> dict[str, Any]:
        """
        读取群显式配置, 未创建记录时返回空配置和零修订号

        参数:
        - self_id: 已确认的机器人账号
        - group_id: 目标群号

        返回:
        - 分区显式配置、修订号和路由代次
        """
        _identity(self_id, group_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT config_json, schema_version, revision, route_generation, updated_at "
                "FROM group_configs WHERE self_id=? AND group_id=?", (self_id, group_id),
            ).fetchone()
        if row is None:
            return {"explicit": {}, "schema_version": 1, "revision": 0, "route_generation": 0, "updated_at": None}
        explicit = json.loads(row["config_json"])
        if not isinstance(explicit, dict):
            raise RuntimeError("群配置数据损坏: 显式配置不是对象")
        return {
            "explicit": explicit, "schema_version": int(row["schema_version"]),
            "revision": int(row["revision"]), "route_generation": int(row["route_generation"]),
            "updated_at": float(row["updated_at"]),
        }

    def access_snapshot(self, self_id: str) -> tuple[str, dict[str, bool]]:
        """
        读取同一数据库快照中的账号模式和逐群响应例外

        参数:
        - self_id: 已确认的机器人账号

        返回:
        - (selected 或 all, 群号到显式启停值); 账号未采用时抛出异常
        """
        _identity(self_id)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            account = connection.execute(
                "SELECT mode FROM group_accounts WHERE self_id=?", (self_id,),
            ).fetchone()
            if account is None:
                raise RuntimeError("群接入账号尚未采用")
            rows = connection.execute(
                "SELECT group_id, config_json FROM group_configs WHERE self_id=?", (self_id,),
            ).fetchall()
        exceptions: dict[str, bool] = {}
        for row in rows:
            explicit = json.loads(row["config_json"])
            if not isinstance(explicit, dict):
                raise RuntimeError("群配置数据损坏: 显式配置不是对象")
            policy = explicit.get("policy", {})
            if not isinstance(policy, dict):
                raise RuntimeError("群配置数据损坏: policy 不是对象")
            enabled = policy.get("enabled")
            if enabled is None or enabled == {"mode": "inherit"}:
                continue
            if not isinstance(enabled, dict) or enabled.get("mode") != "value" or type(enabled.get("value")) is not bool:
                raise RuntimeError("群配置数据损坏: enabled 不是显式布尔值")
            exceptions[str(row["group_id"])] = enabled["value"]
        return str(account["mode"]), exceptions

    def runtime_snapshot(self, self_id: str) -> GroupRuntimeSnapshot:
        """在一次数据库读事务中构造完整的账号群运行时快照"""

        _identity(self_id)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            account = connection.execute(
                "SELECT mode, revision FROM group_accounts WHERE self_id=?", (self_id,),
            ).fetchone()
            if account is None:
                raise RuntimeError("群接入账号尚未采用")
            rows = connection.execute(
                "SELECT group_id, config_json, revision, route_generation FROM group_configs WHERE self_id=?",
                (self_id,),
            ).fetchall()
            members = connection.execute(
                "SELECT group_id, membership FROM group_directory WHERE self_id=?", (self_id,),
            ).fetchall()
        exceptions: dict[str, bool] = {}
        policies: dict[str, dict[str, object]] = {}
        revisions: dict[str, int] = {}
        routes: dict[str, tuple[dict[str, object], int]] = {}
        events: dict[str, dict[str, bool]] = {}
        for row in rows:
            group_id = str(row["group_id"])
            explicit = json.loads(row["config_json"])
            if not isinstance(explicit, dict):
                raise RuntimeError("群配置数据损坏")
            policy = explicit.get("policy", {})
            session = explicit.get("session", {})
            event_config = explicit.get("events", {})
            if not isinstance(policy, dict) or not isinstance(session, dict) or not isinstance(event_config, dict):
                raise RuntimeError("群配置区域数据损坏")
            policy_values(policy)
            session_values(session)
            enabled = policy.get("enabled")
            if enabled is not None and enabled != {"mode": "inherit"}:
                if not isinstance(enabled, dict) or enabled.get("mode") != "value" or type(enabled.get("value")) is not bool:
                    raise RuntimeError("群配置数据损坏: enabled 不是显式布尔值")
                exceptions[group_id] = enabled["value"]
            policies[group_id] = policy
            revisions[group_id] = int(row["revision"])
            routes[group_id] = (session, int(row["route_generation"]))
            events[group_id] = event_values(event_config)
        membership = {str(row["group_id"]): str(row["membership"]) for row in members}
        return GroupRuntimeSnapshot(self_id, str(account["mode"]), exceptions, policies,
                                    revisions, routes, events, membership, int(account["revision"]))

    def policy_snapshot(self, self_id: str) -> dict[str, dict[str, object]]:
        """读取已确认账号所有群的显式策略供运行时冻结使用"""
        _identity(self_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT group_id, config_json FROM group_configs WHERE self_id=?", (self_id,),
            ).fetchall()
        policies: dict[str, dict[str, object]] = {}
        for row in rows:
            explicit = json.loads(row["config_json"])
            if not isinstance(explicit, dict) or not isinstance(explicit.get("policy", {}), dict):
                raise RuntimeError("群策略数据损坏")
            policy = cast(dict[str, object], explicit.get("policy", {}))
            policy_values(policy)
            policies[str(row["group_id"])] = policy
        return policies

    def event_snapshot(self, self_id: str) -> dict[str, dict[str, bool]]:
        """读取逐群业务事件订阅开关, 不影响协议维护与请求账本"""

        _identity(self_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT group_id, config_json FROM group_configs WHERE self_id=?", (self_id,),
            ).fetchall()
        events: dict[str, dict[str, bool]] = {}
        for row in rows:
            explicit = json.loads(row["config_json"])
            if not isinstance(explicit, dict) or not isinstance(explicit.get("events", {}), dict):
                raise RuntimeError("群事件配置数据损坏")
            events[str(row["group_id"])] = event_values(explicit.get("events", {}))
        return events

    def revision_snapshot(self, self_id: str) -> dict[str, int]:
        """读取账号内每个群的已保存修订号供运行时应用状态对比"""
        _identity(self_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT group_id, revision FROM group_configs WHERE self_id=?", (self_id,),
            ).fetchall()
        return {str(row["group_id"]): int(row["revision"]) for row in rows}

    def route_snapshot(self, self_id: str) -> dict[str, tuple[dict[str, object], int]]:
        """冻结群会话显式配置和路由代次供入站事件使用"""
        _identity(self_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT group_id, config_json, route_generation FROM group_configs WHERE self_id=?",
                (self_id,),
            ).fetchall()
        result: dict[str, tuple[dict[str, object], int]] = {}
        for row in rows:
            explicit = json.loads(row["config_json"])
            if not isinstance(explicit, dict) or not isinstance(explicit.get("session", {}), dict):
                raise RuntimeError("群会话配置数据损坏")
            session = cast(dict[str, object], explicit.get("session", {}))
            session_values(session)
            result[str(row["group_id"])] = (session, int(row["route_generation"]))
        return result

    def group_exists(self, self_id: str, group_id: str) -> bool:
        """确认群已发现或保留配置, 禁止为任意未见群创建设置"""
        _identity(self_id, group_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM group_directory WHERE self_id=? AND group_id=? "
                "UNION SELECT 1 FROM group_configs WHERE self_id=? AND group_id=? LIMIT 1",
                (self_id, group_id, self_id, group_id),
            ).fetchone()
        return row is not None

    def patch_group(
        self, self_id: str, group_id: str, section: str, values: Mapping[str, object], *, expected_revision: int,
    ) -> dict[str, Any]:
        """在资源引用扫描锁内保存逐群配置"""
        from satrap.core.config.asr_references import REFERENCE_SCAN_LOCK

        with REFERENCE_SCAN_LOCK:
            return self._patch_group_locked(
                self_id, group_id, section, values, expected_revision=expected_revision,
            )

    def _patch_group_locked(
        self, self_id: str, group_id: str, section: str, values: Mapping[str, object], *, expected_revision: int,
    ) -> dict[str, Any]:
        """
        在同一事务中只替换指定配置区域并核验整体修订号

        参数:
        - self_id: 已确认的机器人账号
        - group_id: 目标群号
        - section: policy, session, events 或 approval
        - values: 已由服务层校验的区域显式字段
        - expected_revision: 最近读取的群配置修订号

        返回:
        - 更新后的显式配置和路由代次
        """
        _identity(self_id, group_id)
        if section not in _SECTIONS or not isinstance(values, Mapping):
            raise ValueError("未知配置区域或无效字段对象")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision 必须是非负整数")
        replacement = dict(values)
        if section == "policy":
            policy_values(replacement)
        if section == "session":
            session_values(replacement)
        if section == "approval":
            from satrap.core.config.group_approval import approval_values

            approval_values(replacement)
        if section == "events":

            event_values(replacement)
        if any(not isinstance(key, str) for key in replacement):
            raise ValueError("配置字段名必须是字符串")
        json.dumps(replacement, ensure_ascii=False, allow_nan=False)
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT config_json, revision, route_generation FROM group_configs "
                "WHERE self_id=? AND group_id=?", (self_id, group_id),
            ).fetchone()
            revision = int(row["revision"]) if row else 0
            if revision != expected_revision:
                raise GroupConfigConflict("群配置已变化, 请刷新后重试")
            explicit = json.loads(row["config_json"]) if row else {}
            if not isinstance(explicit, dict):
                raise RuntimeError("群配置数据损坏: 显式配置不是对象")
            old_session = explicit.get("session", {})
            if replacement:
                explicit[section] = replacement
            else:
                explicit.pop(section, None)
            generation = int(row["route_generation"]) if row else 0
            if section == "session" and (
                not isinstance(old_session, dict)
                or any(old_session.get(key) != replacement.get(key) for key in ("binding", "scope"))
            ):
                generation += 1
            encoded = json.dumps(explicit, ensure_ascii=False, allow_nan=False)
            connection.execute(
                "INSERT INTO group_configs "
                "(self_id, group_id, config_json, schema_version, revision, route_generation, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?) ON CONFLICT(self_id, group_id) DO UPDATE SET "
                "config_json=excluded.config_json, revision=excluded.revision, "
                "route_generation=excluded.route_generation, updated_at=excluded.updated_at",
                (self_id, group_id, encoded, revision + 1, generation, now),
            )
        return self.read_group(self_id, group_id)
