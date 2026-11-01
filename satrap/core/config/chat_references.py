"""Chat 默认模型与会话绑定引用扫描, 删除或重命名前必须完整可读"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
import sqlite3

from satrap.display.settings import ChatSettingsStore


class ChatReferenceScanError(RuntimeError):
    """Chat 引用来源无法完整读取, 调用方必须拒绝变更"""


def list_chat_model_references(
    name: str,
    *,
    settings_path: str | Path | None = None,
    database_path: str | Path,
    active_models: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """
    扫描 Chat 整体默认、已持久会话和可选活跃会话的模型引用

    参数:
    - name: 待删除或重命名的 LLM 配置名
    - settings_path: Chat 整体设置文件, 缺省使用集中配置目录
    - database_path: 包含 conversation_meta 的 Chat 数据库, 不存在表示尚无会话
    - active_models: 可选的会话 ID 到当前模型配置名映射, 用于尚未落库的绑定

    返回:
    - 结构化引用清单; 任一现有来源损坏时抛出 ChatReferenceScanError
    """
    references: list[dict[str, str]] = []
    try:
        settings = ChatSettingsStore(Path(settings_path) if settings_path is not None else None).read()
    except (OSError, ValueError, TypeError) as error:
        raise ChatReferenceScanError(f"无法读取 Chat 默认设置 ({type(error).__name__})") from error
    if settings["default_model"] == name:
        references.append({"summary": "Chat 整体默认模型", "kind": "chat_default"})

    database = Path(database_path)
    try:
        exists = database.exists()
    except OSError as error:
        raise ChatReferenceScanError(f"无法检查 Chat 会话数据库 ({type(error).__name__})") from error
    if exists:
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_meta'",
                ).fetchone()
                rows = [] if table is None else connection.execute(
                    "SELECT conversation_id, model FROM conversation_meta WHERE model=?", (name,),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatReferenceScanError(f"无法扫描 Chat 会话模型引用 ({type(error).__name__})") from error
        for conversation_id, _model in rows:
            if not isinstance(conversation_id, str) or not conversation_id:
                raise ChatReferenceScanError("Chat 会话模型引用包含无效会话 ID")
            references.append({
                "summary": f"Chat 会话 {conversation_id} 的模型绑定",
                "kind": "chat_conversation",
                "conversation_id": conversation_id,
            })

    for conversation_id, model in (active_models or {}).items():
        if model == name:
            references.append({
                "summary": f"Chat 活跃会话 {conversation_id} 的模型绑定",
                "kind": "chat_conversation",
                "conversation_id": conversation_id,
            })
    return list({(item["kind"], item.get("conversation_id", "")): item for item in references}.values())
