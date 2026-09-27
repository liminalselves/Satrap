"""
OneBot 群目录快照与同步完整性状态

只在可信完整响应中判定缺席群已离开, 部分响应和失败保留旧成员关系,
同步令牌和连接代次阻止旧请求覆盖新账号或较新的同步结果
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, cast
import json
import time

from satrap.core.config.group_store import GroupConfigStore, _identity


class GroupDirectoryStore(GroupConfigStore):
    """按账号保存群目录和同步状态, 与群配置共用平台数据库"""

    def __init__(self, database: str | Path) -> None:
        """
        初始化群目录存储

        参数:
        - database: 服务端解析的平台数据库路径
        """
        super().__init__(database)

    def approval_inheritance_counts(self, self_id: str) -> dict[str, int]:
        """统计当前已加入且继承账号审批默认值的群数"""
        from satrap.core.config.group_approval import approval_values
        from satrap.core.config.group_store import GROUP_APPROVAL_ACTIONS

        _identity(self_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT c.config_json FROM group_directory AS d "
                "LEFT JOIN group_configs AS c ON c.self_id=d.self_id AND c.group_id=d.group_id "
                "WHERE d.self_id=? AND d.membership='joined'", (self_id,),
            ).fetchall()
        counts = {action: 0 for action in sorted(GROUP_APPROVAL_ACTIONS)}
        for row in rows:
            explicit = json.loads(row["config_json"]) if row["config_json"] else {}
            if not isinstance(explicit, dict) or not isinstance(explicit.get("approval", {}), dict):
                raise RuntimeError("群审批设置数据损坏")
            overrides = approval_values(explicit.get("approval", {}))
            for action in counts:
                if action not in overrides:
                    counts[action] += 1
        return counts

    def begin_sync(self, self_id: str, connection_generation: int, token: str) -> int:
        """
        登记当前账号和连接的同步任务

        参数:
        - self_id: 已确认的机器人账号
        - connection_generation: 适配器连接代次
        - token: 服务端生成的随机同步令牌

        返回:
        - 本账号递增的扫描代次
        """
        _identity(self_id)
        if not token or len(token) > 128 or connection_generation < 0:
            raise ValueError("同步令牌或连接代次无效")
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT scan_generation FROM group_sync_state WHERE self_id=?", (self_id,),
            ).fetchone()
            scan = int(row["scan_generation"]) + 1 if row else 1
            connection.execute(
                "INSERT INTO group_sync_state "
                "(self_id, connection_generation, sync_token, scan_generation, status, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?) ON CONFLICT(self_id) DO UPDATE SET "
                "connection_generation=excluded.connection_generation, sync_token=excluded.sync_token, "
                "scan_generation=excluded.scan_generation, status='running', started_at=excluded.started_at, "
                "complete=0, truncated=0, reason=NULL",
                (self_id, connection_generation, token, scan, now),
            )
        return scan

    def finish_sync(
        self, self_id: str, connection_generation: int, token: str,
        items: Sequence[Mapping[str, object]], *, complete: bool, truncated: bool, reason: str | None = None,
    ) -> bool:
        """
        提交同步结果, 只有完整响应才将缺席群标记离开

        参数:
        - self_id: 已确认的机器人账号
        - connection_generation: 发起同步时的连接代次
        - token: 发起同步时返回的令牌
        - items: 已完成协议校验的有效条目
        - complete: 协议响应完整且可信
        - truncated: 协议响应被限制或包含无效条目
        - reason: 不完整原因码

        返回:
        - 令牌仍有效并已提交时返回 True, 迟到结果返回 False
        """
        _identity(self_id)
        if complete and truncated:
            raise ValueError("截断结果不能标为完整")
        normalized: list[tuple[str, str | None, int | None, int | None]] = []
        seen: set[str] = set()
        for item in items:
            group_id = item.get("group_id")
            if not isinstance(group_id, str):
                raise ValueError("群目录条目缺少规范群号")
            _identity(self_id, group_id)
            if group_id in seen:
                raise ValueError("群目录条目重复")
            seen.add(group_id)
            name = item.get("group_name")
            if name is not None and (not isinstance(name, str) or len(name) > 200):
                raise ValueError("群名称无效")
            counts: list[int | None] = []
            for key in ("member_count", "max_member_count"):
                value = item.get(key)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    raise ValueError("群成员数量无效")
                counts.append(cast(int | None, value))
            normalized.append((group_id, cast(str | None, name), counts[0], counts[1]))
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT scan_generation, started_at FROM group_sync_state "
                "WHERE self_id=? AND connection_generation=? AND sync_token=? AND status='running'",
                (self_id, connection_generation, token),
            ).fetchone()
            if row is None:
                return False
            scan = int(row["scan_generation"])
            started_at = float(row["started_at"])
            for group_id, name, members, maximum in normalized:
                connection.execute(
                    "INSERT INTO group_directory "
                    "(self_id, group_id, group_name, member_count, max_member_count, "
                    "membership, confirmed_at, generation) VALUES (?, ?, ?, ?, ?, 'joined', ?, ?) "
                    "ON CONFLICT(self_id, group_id) DO UPDATE SET "
                    "group_name=excluded.group_name, member_count=excluded.member_count, "
                    "max_member_count=excluded.max_member_count, membership='joined', "
                    "confirmed_at=excluded.confirmed_at, generation=excluded.generation "
                    "WHERE group_directory.confirmed_at IS NULL OR group_directory.confirmed_at<=?",
                    (self_id, group_id, name, members, maximum, now, scan, started_at),
                )
            if complete:
                connection.execute(
                    "UPDATE group_directory SET membership='left', confirmed_at=?, generation=? "
                    "WHERE self_id=? AND generation<>? AND (confirmed_at IS NULL OR confirmed_at<=?)",
                    (now, scan, self_id, scan, started_at),
                )
                connection.execute(
                    "UPDATE group_actions SET state='expired', decision_at=?, "
                    "result_json=? "
                    "WHERE self_id=? AND state='pending' AND group_id IN "
                    "(SELECT group_id FROM group_directory WHERE self_id=? AND membership='left')",
                    (now, json.dumps({"reason": "membership_left"}), self_id, self_id),
                )
            connection.execute(
                "UPDATE group_sync_state SET status=?, completed_at=?, complete=?, truncated=?, "
                "reason=?, last_complete_at=CASE WHEN ? THEN ? ELSE last_complete_at END "
                "WHERE self_id=?",
                ("complete" if complete else "partial", now, int(complete), int(truncated), reason,
                 int(complete), now, self_id),
            )
        return True

    def fail_sync(self, self_id: str, connection_generation: int, token: str, reason: str) -> bool:
        """
        标记当前同步失败并保留旧目录和上次完整时间

        参数:
        - self_id: 已确认的机器人账号
        - connection_generation: 发起同步时的连接代次
        - token: 发起同步时返回的令牌
        - reason: 稳定失败原因码

        返回:
        - 令牌仍有效并已更新时返回 True
        """
        _identity(self_id)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE group_sync_state SET status='failed', completed_at=?, complete=0, "
                "truncated=0, reason=? WHERE self_id=? AND connection_generation=? "
                "AND sync_token=? AND status='running'",
                (time.time(), reason, self_id, connection_generation, token),
            )
            return cursor.rowcount == 1

    def confirm_membership(self, self_id: str, group_id: str, joined: bool, *, group_name: str | None = None) -> None:
        """
        根据可信机器人自身入退群事件更新成员关系

        参数:
        - self_id: 可信事件中的机器人账号
        - group_id: 事件中的群号
        - joined: True 表示机器人入群, False 表示机器人退群
        - group_name: 事件中可信的可选群名称
        """
        _identity(self_id, group_id)
        if group_name is not None and len(group_name) > 200:
            raise ValueError("群名称过长")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT scan_generation FROM group_sync_state WHERE self_id=?", (self_id,),
            ).fetchone()
            scan = int(row["scan_generation"]) if row else 0
            connection.execute(
                "INSERT INTO group_directory "
                "(self_id, group_id, group_name, membership, confirmed_at, generation) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(self_id, group_id) DO UPDATE SET "
                "group_name=COALESCE(excluded.group_name, group_directory.group_name), "
                "membership=excluded.membership, confirmed_at=excluded.confirmed_at, "
                "generation=excluded.generation",
                (self_id, group_id, group_name, "joined" if joined else "left", time.time(), scan),
            )
            if not joined:
                connection.execute(
                    "UPDATE group_actions SET state='expired', decision_at=?, "
                    "result_json=? "
                    "WHERE self_id=? AND group_id=? AND state='pending'",
                    (time.time(), json.dumps({"reason": "membership_left"}), self_id, group_id),
                )

    def sync_status(self, self_id: str) -> dict[str, Any]:
        """
        读取最近同步状态和最后一次完整快照时间

        参数:
        - self_id: 已确认的机器人账号

        返回:
        - 状态, 同步令牌, 连接代次, 完整性与时间; 未同步返回 never
        """
        _identity(self_id)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM group_sync_state WHERE self_id=?", (self_id,)).fetchone()
        if row is None:
            return {"status": "never", "sync_id": None, "complete": False, "truncated": False,
                    "reason": None, "started_at": None, "completed_at": None, "last_complete_at": None,
                    "connection_generation": 0}
        return {
            "status": row["status"], "sync_id": row["sync_token"],
            "complete": bool(row["complete"]), "truncated": bool(row["truncated"]),
            "reason": row["reason"], "started_at": row["started_at"],
            "completed_at": row["completed_at"], "last_complete_at": row["last_complete_at"],
            "connection_generation": int(row["connection_generation"]),
        }

    def group_record(self, self_id: str, group_id: str) -> dict[str, Any] | None:
        """读取单群目录记录, 已发现或仅有配置均可打开详情"""
        _identity(self_id, group_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT group_name, member_count, max_member_count, membership, confirmed_at "
                "FROM group_directory WHERE self_id=? AND group_id=?", (self_id, group_id),
            ).fetchone()
        if row is None:
            if not self.group_exists(self_id, group_id):
                return None
            return {"group_id": group_id, "group_name": None, "member_count": None,
                    "max_member_count": None, "membership": "config_only", "confirmed_at": None}
        return {"group_id": group_id, "group_name": row["group_name"],
                "member_count": row["member_count"], "max_member_count": row["max_member_count"],
                "membership": row["membership"], "confirmed_at": row["confirmed_at"]}

    def membership_snapshot(self, self_id: str) -> dict[str, str]:
        """返回已确认账号的群成员关系快照供管理目标校验"""
        _identity(self_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT group_id, membership FROM group_directory WHERE self_id=?", (self_id,),
            ).fetchall()
        return {str(row["group_id"]): str(row["membership"]) for row in rows}

    def list_groups(
        self, self_id: str, *, query: str = "", membership: str = "joined",
        response: str = "all", page: int = 1, page_size: int = 25,
        response_gate: bool = True,
    ) -> dict[str, Any]:
        """
        在服务端对同账号的目录和仅配置群筛选分页

        参数:
        - self_id: 固定的机器人账号
        - query: 群名包含或群号匹配文本
        - membership: joined, left, unknown, config_only 或 all
        - response: enabled, disabled 或 all
        - page: 从 1 开始的页号
        - page_size: 25, 50 或 100

        返回:
        - items, total, page, page_size 和同账号整体 counts
        """
        _identity(self_id)
        if membership not in {"joined", "left", "unknown", "config_only", "all"}:
            raise ValueError("membership 筛选无效")
        if response not in {"enabled", "disabled", "all"}:
            raise ValueError("response 筛选无效")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > 100000:
            raise ValueError("page 必须是有效正整数")
        if page_size not in {25, 50, 100}:
            raise ValueError("page_size 仅支持 25, 50 或 100")
        if len(query) > 200:
            raise ValueError("搜索文本过长")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            account = connection.execute("SELECT mode FROM group_accounts WHERE self_id=?", (self_id,)).fetchone()
            if account is None:
                raise ValueError("账号尚未采用")
            directory = connection.execute(
                "SELECT * FROM group_directory WHERE self_id=?", (self_id,),
            ).fetchall()
            configs = connection.execute(
                "SELECT group_id, config_json FROM group_configs WHERE self_id=?", (self_id,),
            ).fetchall()
        by_group: dict[str, dict[str, Any]] = {
            str(row["group_id"]): {
                "group_id": str(row["group_id"]), "group_name": row["group_name"],
                "member_count": row["member_count"], "max_member_count": row["max_member_count"],
                "membership": row["membership"], "confirmed_at": row["confirmed_at"],
            }
            for row in directory
        }
        exceptions: dict[str, bool] = {}
        for row in configs:
            group_id = str(row["group_id"])
            by_group.setdefault(group_id, {
                "group_id": group_id, "group_name": None, "member_count": None,
                "max_member_count": None, "membership": "config_only", "confirmed_at": None,
            })
            explicit = json.loads(row["config_json"])
            if not isinstance(explicit, dict) or not isinstance(explicit.get("policy", {}), dict):
                raise RuntimeError("群配置数据损坏")
            enabled = explicit.get("policy", {}).get("enabled")
            if enabled is not None:
                if not isinstance(enabled, dict) or enabled.get("mode") != "value" or type(enabled.get("value")) is not bool:
                    raise RuntimeError("群配置数据损坏: enabled 无效")
                exceptions[group_id] = enabled["value"]
        mode = str(account["mode"])
        all_items = []
        for item in by_group.values():
            enabled = exceptions.get(item["group_id"], mode == "all")
            item["response_enabled"] = response_gate and enabled
            item["response_source"] = (
                "platform" if not response_gate else
                "group" if item["group_id"] in exceptions else "account"
            )
            all_items.append(item)
        counts = {
            "joined": sum(item["membership"] == "joined" for item in all_items),
            "response_enabled": sum(item["response_enabled"] and item["membership"] == "joined" for item in all_items),
            "configured": len(configs),
        }
        search = query.strip().casefold()
        filtered = [
            item for item in all_items
            if (membership == "all" or item["membership"] == membership)
            and (response == "all" or item["response_enabled"] == (response == "enabled"))
            and (not search or search in item["group_id"] or search in str(item["group_name"] or "").casefold())
        ]
        filtered.sort(key=lambda item: (item["membership"] != "joined", item["group_id"]))
        offset = (page - 1) * page_size
        return {"items": filtered[offset:offset + page_size], "total": len(filtered),
                "page": page, "page_size": page_size, "counts": counts}
