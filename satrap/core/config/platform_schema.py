"""
平台数据库结构迁移

集中管理会话覆盖与逐群管理表的版本, 防止多个存储对象竞争 user_version,
并在已声明结构版本的数据库缺表时显式报告损坏
"""
from __future__ import annotations

import sqlite3


PLATFORM_SCHEMA_VERSION = 8

_OVERRIDE_TABLE = "session_config_overrides"
_GROUP_TABLES = frozenset({
    "group_accounts", "group_legacy_adoption", "group_configs", "group_directory", "group_sync_state", "group_actions",
})
_MESSAGE_TABLES = frozenset({"platform_message_chats", "platform_messages", "platform_message_backups"})
_SUMMARY_TABLES = frozenset({"group_chat_summary_snapshots", "group_chat_summaries", "group_chat_summary_refs"})


def _tables(connection: sqlite3.Connection) -> set[str]:
    """
读取当前数据库中的普通表名

    参数:
    - connection: 正在迁移的平台数据库连接

    返回:
    - 已存在的普通表名集合
    """
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def ensure_platform_tables(connection: sqlite3.Connection) -> None:
    """
在调用方事务中按版本向前迁移平台表

    参数:
    - connection: 平台数据库连接; 调用方负责事务提交或回滚
    """
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    existing = _tables(connection)
    required = (({_OVERRIDE_TABLE} if version >= 1 else set()) | (_GROUP_TABLES if version >= 2 else set())
                | (_MESSAGE_TABLES if version >= 4 else set())
                | ({"platform_message_policy"} if version >= 5 else set())
                | (_SUMMARY_TABLES if version >= 6 else set()))
    required |= {"group_chat_assets"} if version >= 7 else set()
    required |= {"friend_actions", "friend_policies"} if version >= 8 else set()
    missing = required - existing
    if missing:
        raise RuntimeError(f"平台数据库结构损坏: 版本 {version} 缺少表 {', '.join(sorted(missing))}")
    if version >= 3:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(group_actions)")}
        if "model_origin_json" not in columns:
            raise RuntimeError("平台数据库结构损坏: 群动作表缺少模型来源字段")

    if version < 1:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS session_config_overrides ("
            "session_id TEXT NOT NULL, namespace TEXT NOT NULL, "
            "config_json TEXT NOT NULL DEFAULT '{}', schema_version INTEGER NOT NULL DEFAULT 1, "
            "revision INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL, "
            "PRIMARY KEY (session_id, namespace))"
        )
        connection.execute("PRAGMA user_version = 1")

    if version < 2:
        connection.execute(
            "CREATE TABLE group_accounts ("
            "self_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('selected', 'all')), "
            "approval_defaults_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1, "
            "legacy_source_fingerprint TEXT, migrated_at REAL NOT NULL, last_bound_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE group_legacy_adoption ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1), self_id TEXT NOT NULL, "
            "source_fingerprint TEXT NOT NULL, adopted_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE group_configs ("
            "self_id TEXT NOT NULL, group_id TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}', "
            "schema_version INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1, "
            "route_generation INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, "
            "PRIMARY KEY (self_id, group_id))"
        )
        connection.execute(
            "CREATE TABLE group_directory ("
            "self_id TEXT NOT NULL, group_id TEXT NOT NULL, group_name TEXT, "
            "member_count INTEGER, max_member_count INTEGER, "
            "membership TEXT NOT NULL DEFAULT 'unknown' "
            "CHECK(membership IN ('joined', 'left', 'unknown')), "
            "confirmed_at REAL, generation INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (self_id, group_id))"
        )
        connection.execute(
            "CREATE INDEX idx_group_directory_membership ON group_directory(self_id, membership)"
        )
        connection.execute(
            "CREATE TABLE group_sync_state ("
            "self_id TEXT PRIMARY KEY, connection_generation INTEGER NOT NULL DEFAULT 0, "
            "sync_token TEXT, scan_generation INTEGER NOT NULL DEFAULT 0, "
            "status TEXT NOT NULL DEFAULT 'never', started_at REAL, completed_at REAL, "
            "last_complete_at REAL, complete INTEGER NOT NULL DEFAULT 0, "
            "truncated INTEGER NOT NULL DEFAULT 0, reason TEXT)"
        )
        connection.execute(
            "CREATE TABLE group_actions ("
            "action_id TEXT PRIMARY KEY, self_id TEXT NOT NULL, group_id TEXT NOT NULL, "
            "action_type TEXT NOT NULL, params_json TEXT NOT NULL, fingerprint TEXT NOT NULL, "
            "actor_kind TEXT NOT NULL, policy_revision INTEGER NOT NULL, "
            "state TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL, "
            "decision_at REAL, executed_at REAL, result_json TEXT)"
        )
        connection.execute(
            "CREATE INDEX idx_group_actions_identity ON group_actions(self_id, group_id, created_at)"
        )
        connection.execute("PRAGMA user_version = 2")

    if version < 3:
        connection.execute("ALTER TABLE group_actions ADD COLUMN model_origin_json TEXT")
        connection.execute("PRAGMA user_version = 3")

    if version < 4:
        connection.execute(
            "CREATE TABLE platform_message_chats ("
            "scope_key TEXT PRIMARY KEY, adapter_id TEXT NOT NULL, self_id TEXT NOT NULL, "
            "conversation_kind TEXT NOT NULL, chat_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', "
            "revision INTEGER NOT NULL DEFAULT 0, delete_before REAL, delete_token TEXT, "
            "captured_from REAL, captured_to REAL)"
        )
        connection.execute(
            "CREATE TABLE platform_messages ("
            "scope_key TEXT NOT NULL, message_id TEXT NOT NULL, sender_id TEXT NOT NULL DEFAULT '', "
            "nickname TEXT NOT NULL DEFAULT '', card TEXT NOT NULL DEFAULT '', message_time REAL NOT NULL, "
            "received_at REAL NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')), "
            "text TEXT NOT NULL DEFAULT '', components_json TEXT NOT NULL DEFAULT '[]', "
            "reply_to_message_id TEXT, mentions_json TEXT NOT NULL DEFAULT '[]', media_json TEXT NOT NULL DEFAULT '[]', "
            "status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'recalled', 'deleted', 'expired')), "
            "source TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 1, truncated INTEGER NOT NULL DEFAULT 0, "
            "delete_token TEXT, time_source TEXT NOT NULL DEFAULT 'platform' CHECK(time_source IN ('platform', 'local')), "
            "PRIMARY KEY(scope_key, message_id), "
            "FOREIGN KEY(scope_key) REFERENCES platform_message_chats(scope_key))"
        )
        connection.execute(
            "CREATE INDEX idx_platform_messages_time ON platform_messages(scope_key, status, message_time, message_id)"
        )
        connection.execute(
            "CREATE INDEX idx_platform_messages_sender ON platform_messages(scope_key, sender_id, status, message_time, message_id)"
        )
        connection.execute(
            "CREATE TABLE platform_message_backups ("
            "backup_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, action TEXT NOT NULL, "
            "created_at REAL NOT NULL, expires_at REAL NOT NULL, payload_json TEXT NOT NULL, "
            "FOREIGN KEY(scope_key) REFERENCES platform_message_chats(scope_key))"
        )
        connection.execute("CREATE INDEX idx_platform_message_backups_expiry ON platform_message_backups(expires_at)")
        connection.execute("PRAGMA user_version = 4")
    if version < 5:
        connection.execute(
            "CREATE TABLE platform_message_policy (adapter_id TEXT PRIMARY KEY, "
            "retention_days INTEGER NOT NULL CHECK(retention_days BETWEEN 1 AND 3650))"
        )
        connection.execute("PRAGMA user_version = 5")
    if version < 6:
        connection.execute(
            "CREATE TABLE group_chat_summary_snapshots (snapshot_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, "
            "owner TEXT NOT NULL, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL, read_until INTEGER NOT NULL, "
            "created_at REAL NOT NULL, expires_at REAL NOT NULL, UNIQUE(scope_key, owner, fingerprint))"
        )
        connection.execute("CREATE INDEX idx_group_chat_snapshot_expiry ON group_chat_summary_snapshots(expires_at)")
        connection.execute(
            "CREATE TABLE group_chat_summaries (summary_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, "
            "title TEXT NOT NULL, points_json TEXT NOT NULL, metadata_json TEXT NOT NULL, "
            "operation_key TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL DEFAULT 1, "
            "state TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, expires_at REAL NOT NULL)"
        )
        connection.execute("CREATE INDEX idx_group_chat_summary_scope ON group_chat_summaries(scope_key, created_at, summary_id)")
        connection.execute(
            "CREATE TABLE group_chat_summary_refs (summary_id TEXT NOT NULL, scope_key TEXT NOT NULL, "
            "message_id TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(summary_id, message_id), "
            "FOREIGN KEY(summary_id) REFERENCES group_chat_summaries(summary_id) ON DELETE CASCADE)"
        )
        connection.execute("CREATE INDEX idx_group_chat_summary_source ON group_chat_summary_refs(scope_key, message_id)")
        connection.execute("PRAGMA user_version = 6")
    if version < 7:
        connection.execute(
            "CREATE TABLE group_chat_assets (asset_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, "
            "owner TEXT NOT NULL, source_message_id TEXT, source_digest TEXT, media_index INTEGER, "
            "blob_key TEXT NOT NULL, mime_type TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, "
            "size_bytes INTEGER NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'active')"
        )
        connection.execute("CREATE INDEX idx_group_chat_asset_source ON group_chat_assets(scope_key, source_message_id)")
        connection.execute("PRAGMA user_version = 7")
    if version < 8:
        connection.execute(
            "CREATE TABLE friend_actions (self_id TEXT NOT NULL, action_id TEXT NOT NULL, action_type TEXT NOT NULL, "
            "params_json TEXT NOT NULL, actor_kind TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, "
            "expires_at REAL NOT NULL, decision_at REAL, executed_at REAL, result_json TEXT, target_json TEXT, actor_id TEXT NOT NULL, "
            "PRIMARY KEY(self_id, action_id))"
        )
        connection.execute("CREATE INDEX idx_friend_actions_account ON friend_actions(self_id, created_at)")
        connection.execute("CREATE TABLE friend_policies (self_id TEXT PRIMARY KEY, protected_json TEXT NOT NULL)")
        connection.execute("PRAGMA user_version = 8")
