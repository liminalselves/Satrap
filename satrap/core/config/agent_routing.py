"""
按平台对话类型解析 Agent 绑定与持久路由代次

平台默认和对话类型覆盖共享同一校验契约,
路由代次按真实对话保存, 防止切换回旧配置时复用旧上下文
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
import sqlite3
import json
import traceback

from satrap.core.log import logger


def validate_session_bindings(value: object) -> dict[str, dict[str, str]]:
    """
    校验按对话类型声明的完整绑定或继承配置

    参数:
    - value: 对话类型到绑定的映射, None 表示没有覆盖

    返回:
    - 规范化映射, 非法类型和不完整绑定抛出 ValueError
    """
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("session_bindings 必须是最多 32 项的对象")
    result: dict[str, dict[str, str]] = {}
    for kind, raw in value.items():
        if not isinstance(kind, str) or not kind or len(kind) > 64 or not kind.isascii() or not all(
            char.isalnum() or char in "_-" for char in kind
        ):
            raise ValueError("对话类型必须是非空的英文标识")
        if not isinstance(raw, dict):
            raise ValueError(f"对话类型 {kind} 的绑定必须是对象")
        if raw.get("mode") == "inherit" and set(raw) == {"mode"}:
            result[kind] = {"mode": "inherit"}
            continue
        if (set(raw) != {"mode", "provider", "config_name"} or raw.get("mode") != "value"
                or raw.get("provider") not in {"session_class", "edictum"}
                or not isinstance(raw.get("config_name"), str) or not raw["config_name"].strip()
                or len(raw["config_name"]) > 128):
            raise ValueError(f"对话类型 {kind} 必须指定完整的 Provider 和 Agent 配置")
        result[kind] = {"mode": "value", "provider": raw["provider"], "config_name": raw["config_name"].strip()}
    return result


def resolve_agent_binding(platform: Mapping[str, object], kind: str) -> tuple[dict[str, str], str]:
    """
    解析平台默认和指定对话类型的 Agent 绑定

    参数:
    - platform: 平台配置快照
    - kind: 适配器归一的对话类型

    返回:
    - 完整绑定及来源, 未覆盖时继承平台默认
    """
    bindings = validate_session_bindings(platform.get("session_bindings"))
    selected = bindings.get(kind, {})
    if selected.get("mode") == "value":
        return {"provider": selected["provider"], "config_name": selected["config_name"]}, "conversation_kind"
    return {"provider": str(platform.get("session_provider") or "session_class"),
            "config_name": str(platform.get("session_type") or "")}, "platform"


class AgentRouteStore:
    """按对话持久保存有效路由签名和递增代次"""

    def __init__(self, path: str | Path) -> None:
        """
        绑定已有平台数据库, 不在构造时创建存储

        参数:
        - path: 平台数据库路径
        """
        self.path = Path(path)
        self._has_routes: bool | None = None

    def revision(self, self_id: str, kind: str, chat_id: str, signature: tuple[object, ...], *, enabled: bool) -> int:
        """
        为已启用或曾启用隔离路由的对话取得持久代次

        参数:
        - self_id: 机器人账号 ID
        - kind: 已归一的对话类型
        - chat_id: 平台对话 ID
        - signature: 最终绑定, 上下文范围和群路由代次
        - enabled: 是否显式启用按对话类型路由

        返回:
        - 路由代次, 未启用且无既有记录时为 0; 存储失败记录日志并抛出异常
        """
        if not self_id or not kind or not chat_id:
            if enabled:
                logger.warning("[Agent 路由] 缺少账号或对话身份, 拒绝创建隔离路由")
                raise ValueError("隔离 Agent 路由需要机器人账号与完整对话身份")
            return 0
        if not enabled and (self._has_routes is False or not self.path.is_file()):
            return 0
        try:
            if enabled:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
                if not enabled and not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_route_versions'",
                ).fetchone():
                    self._has_routes = False
                    return 0
                connection.execute("CREATE TABLE IF NOT EXISTS agent_route_versions ("
                                   "self_id TEXT NOT NULL, kind TEXT NOT NULL, chat_id TEXT NOT NULL, "
                                   "signature TEXT NOT NULL, revision INTEGER NOT NULL, "
                                   "PRIMARY KEY (self_id, kind, chat_id))")
                self._has_routes = True
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT signature, revision FROM agent_route_versions "
                                         "WHERE self_id=? AND kind=? AND chat_id=?", (self_id, kind, chat_id)).fetchone()
                if row is None and not enabled:
                    return 0
                encoded = json.dumps(signature, ensure_ascii=True, separators=(",", ":"))
                if row is not None and row[0] == encoded:
                    return int(row[1])
                if row is not None and len(signature) == 7:
                    previous = json.loads(row[0])
                    if isinstance(previous, list) and len(previous) == 7 and tuple(previous[:5]) == signature[:5]:
                        connection.execute("UPDATE agent_route_versions SET signature=? "
                                           "WHERE self_id=? AND kind=? AND chat_id=?", (encoded, self_id, kind, chat_id))
                        return int(row[1])
                revision = int(row[1]) + 1 if row is not None else 1
                connection.execute("INSERT INTO agent_route_versions VALUES (?, ?, ?, ?, ?) "
                                   "ON CONFLICT(self_id, kind, chat_id) DO UPDATE SET "
                                   "signature=excluded.signature, revision=excluded.revision",
                                   (self_id, kind, chat_id, encoded, revision))
                return revision
        except Exception as error:
            logger.error(f"[Agent 路由] 代次保存失败: {kind}/{chat_id}, {error}\n{traceback.format_exc()}")
            raise

    def apply_platform(self, platform: Mapping[str, object]) -> int:
        """
        平台配置生效时立即推进受影响对话, 无消息期间的切换也不复用旧代次

        参数:
        - platform: 已解析平台默认绑定和 settings 的实际运行配置

        返回:
        - 有效绑定或范围发生变化的对话数; 没有隔离路由时不创建数据库
        """
        if not self.path.is_file():
            return 0
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_route_versions'").fetchone():
                    return 0
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute("SELECT self_id, kind, chat_id, signature, revision FROM agent_route_versions").fetchall()
                settings = platform.get("settings", {})
                if not isinstance(settings, Mapping):
                    raise ValueError("平台 settings 无效")
                changed = 0
                for self_id, kind, chat_id, encoded, revision in rows:
                    previous = json.loads(encoded)
                    if not isinstance(previous, list) or len(previous) != 7:
                        raise ValueError("持久 Agent 路由签名无效")
                    current = list(previous)
                    current[0] = str(platform.get("type") or "")
                    if previous[5] != "group":
                        binding, source = resolve_agent_binding(platform, kind)
                        current[1:3] = [binding["provider"], binding["config_name"]]
                        current[5] = source
                    if kind == "group" and not previous[6]:
                        current[3] = str(settings.get("context_scope", "legacy_user"))
                    advance = int(current[:5] != previous[:5])
                    if current != previous:
                        connection.execute("UPDATE agent_route_versions SET signature=?, revision=? "
                                           "WHERE self_id=? AND kind=? AND chat_id=?",
                                           (json.dumps(current, ensure_ascii=True, separators=(",", ":")),
                                            revision + advance, self_id, kind, chat_id))
                    changed += advance
                return changed
        except Exception as error:
            logger.error(f"[Agent 路由] 平台代次协调失败: {platform.get('id')}, {error}\n{traceback.format_exc()}")
            raise
