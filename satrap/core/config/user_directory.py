"""
对话记录中的用户资料与关联目录

只读汇总用户资料和有明确归属的路由, 不推断群共享对话的个人归属,
资料修改通过数据库事务和版本校验执行, 不改写上下文或平台路由
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any
import hashlib
import sqlite3
import json

from satrap.core.config.conversation_catalog import conversation_records, _route_metadata

from satrap.core.log import logger


class UserDirectoryConflict(ValueError):
    """用户资料版本冲突"""


def _revision(row: dict[str, Any] | None) -> str:
    """
    计算单个用户资料的版本, 未保存资料也有稳定版本

    参数:
    - row: 用户资料行或 None

    返回:
    - 用于修改前校验的摘要
    """
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _sessions(row: dict[str, Any]) -> list[str]:
    """
    读取手动关联列表, 损坏数据拒绝参与修改

    参数:
    - row: 用户资料行

    返回:
    - 去重后的会话 ID 列表, 格式错误时抛出 ValueError 由请求边界记录
    """
    value = json.loads(row.get("user_session") or "[]")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"用户 {row['user_id']} 的会话关联数据无效")
    return list(dict.fromkeys(value))


class UserDirectoryService:
    """浏览和维护一个平台实例内的用户资料"""

    def __init__(self, database: Path, platform: dict[str, Any]):
        """
        初始化目录, 不创建数据库或数据目录

        参数:
        - database: 平台数据库路径
        - platform: 动态平台描述
        """
        self.database = database
        self.platform = platform

    def _connect(self, mode: str = "ro") -> sqlite3.Connection:
        """
        使用明确读写模式连接已有数据库

        参数:
        - mode: ro 为只读, rw 为修改已有数据库, rwc 只用于创建用户资料

        返回:
        - 设置命名行的连接, 失败由请求边界处理
        """
        connection = sqlite3.connect(self.database.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _records(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        """
        合并保存资料与精确的用户路由, 保留没有资料的路由归属

        参数:
        - connection: 当前事务连接

        返回:
        - 用户列表及其有证据的对话关联, 群共享路由不归属个人
        """
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        profiles = {row["user_id"]: dict(row) for row in connection.execute("SELECT * FROM user_info")} if "user_info" in tables else {}
        routes: dict[str, set[str]] = {}
        if "context_sessions" in tables:
            for row in connection.execute("SELECT * FROM context_sessions"):
                route = _route_metadata(dict(row))
                if route["user_id"] and route["scope"] not in {"group", "unknown"}:
                    routes.setdefault(route["user_id"], set()).add(route["session_id"])
        conversations = {item["conversation_id"]: item for item in conversation_records(connection, self.platform)}
        result = []
        for identity in sorted(profiles.keys() | routes.keys()):
            profile = profiles.get(identity)
            warning = ""
            try:
                manual = _sessions(profile) if profile else []
            except (ValueError, TypeError) as error:
                logger.warning(f"[用户目录] 关联数据无效: {self.platform['id']}/{identity}, {error}")
                manual = []
                warning = "保存的列表关联格式无效, 请检查数据库; 路由关联仍可查看"
            routed = routes.get(identity, set())
            related = []
            for session in sorted(set(manual) | routed):
                record = conversations.get(session)
                related.append({"conversation_id": session, "title": record["title"] if record else session,
                                "exists": record is not None, "manual": session in manual, "routed": session in routed,
                                "last_activity_at": record.get("last_activity_at") if record else None})
            result.append({"platform_id": self.platform["id"], "platform_label": self.platform.get("label", self.platform["id"]),
                           "platform_type": self.platform.get("type", "unknown"), "user_id": identity,
                           "user_nickname": profile["user_nickname"] if profile else "",
                           "user_platform": profile["user_platform"] if profile else "", "has_profile": profile is not None,
                           "revision": _revision(profile), "user_session": manual, "conversations": related,
                           "conversation_count": len(related), "warning": warning})
        return result

    def records(self) -> list[dict[str, Any]]:
        """
        只读浏览用户目录

        返回:
        - 用户记录, 数据库不存在时返回空列表, 不创建文件或迁移表
        """
        if not self.database.is_file():
            return []
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            return self._records(connection)

    def mutate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        原子修改用户资料或手动关联, 保留对话及实际路由

        参数:
        - payload: action, user_id, expected_revision 及操作字段

        返回:
        - 最新用户记录或删除成功标识, 冲突和无效请求由控制请求边界记录并返回
        """
        action = payload.get("action")
        identity = payload.get("user_id")
        if action not in {"create", "update", "delete", "associate", "dissociate"}:
            raise ValueError("无效的用户资料操作")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("用户 ID 不能为空")
        identity = identity.strip()
        if not isinstance(payload.get("expected_revision"), str):
            raise ValueError("缺少用户资料版本, 请重新读取")
        if action == "create":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        elif not self.database.is_file():
            raise KeyError("用户资料不存在")
        with closing(self._connect("rwc" if action == "create" else "rw")) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            row = connection.execute("SELECT * FROM user_info WHERE user_id=?", (identity,)).fetchone() if "user_info" in tables else None
            profile = dict(row) if row else None
            if payload["expected_revision"] != _revision(profile):
                raise UserDirectoryConflict("用户资料已变化, 请重新读取后再修改")
            if action == "create":
                if profile:
                    raise UserDirectoryConflict("用户资料已存在")
                nickname = payload.get("nickname", "")
                if not isinstance(nickname, str):
                    raise ValueError("昵称必须是文本")
                connection.execute("CREATE TABLE IF NOT EXISTS user_info (user_id TEXT PRIMARY KEY, user_platform TEXT NOT NULL, user_nickname TEXT NOT NULL, user_session TEXT NOT NULL)")
                connection.execute("INSERT INTO user_info VALUES (?, ?, ?, ?)", (identity, self.platform.get("type", ""), nickname, "[]"))
            else:
                if not profile:
                    raise KeyError("用户资料不存在, 请先添加资料")
                if action == "update":
                    nickname = payload.get("nickname")
                    if not isinstance(nickname, str):
                        raise ValueError("昵称必须是文本")
                    connection.execute("UPDATE user_info SET user_nickname=? WHERE user_id=?", (nickname, identity))
                elif action == "delete":
                    connection.execute("DELETE FROM user_info WHERE user_id=?", (identity,))
                else:
                    session = payload.get("session_id")
                    if not isinstance(session, str) or not session.strip():
                        raise ValueError("缺少对话 ID")
                    sessions = _sessions(profile)
                    if action == "associate":
                        if session not in {item["conversation_id"] for item in conversation_records(connection, self.platform)}:
                            raise KeyError("当前平台内的对话不存在")
                        sessions = list(dict.fromkeys([*sessions, session]))
                    else:
                        sessions = [item for item in sessions if item != session]
                    connection.execute("UPDATE user_info SET user_session=? WHERE user_id=?", (json.dumps(sessions, ensure_ascii=False), identity))
            records = self._records(connection)
            connection.commit()
        logger.info(f"[用户资料] {action}: {self.platform['id']}/{identity}")
        return {"ok": True, "user": next((item for item in records if item["user_id"] == identity), None)}


def missing_profile_revision() -> str:
    """
    返回新建资料所需的空版本

    返回:
    - 尚未保存资料的版本摘要
    """
    return _revision(None)
