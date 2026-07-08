from __future__ import annotations

import secrets
import string
from satrap.core.type import CommandAction

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from satrap.core.framework.Base import Session, AsyncSession
    from satrap.core.framework.command import CommandHandler, AsyncCommandHandler


_UID_ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _short_uid(n: int = 6) -> str:
    """生成 n 字符 base62 随机 ID"""
    return ''.join(secrets.choice(_UID_ALPHABET) for _ in range(n))


def _parse_user_id(session_id: str) -> str:
    """从 session_id 中提取 user_id, 兼容新旧格式"""
    parts = session_id.split(":")
    if len(parts) >= 4:
        return parts[2]
    if len(parts) == 3:
        return parts[1]
    return ""


def _base_id(session_id: str) -> str:
    """提取 session_id 的固定前缀（去掉尾部随机段）, 兼容新旧格式"""
    parts = session_id.split(":")
    if len(parts) >= 4:
        return ":".join(parts[:3])
    if len(parts) == 3:
        return ":".join(parts[:2])
    return session_id


def _get_user_contexts_list(session: Session | AsyncSession) -> list[str]:
    """获取当前用户的所有会话 ID 列表"""
    user_manager = session._user_manager
    if user_manager:
        user_id = _parse_user_id(session.session_id)
        if user_id:
            return user_manager.get_user_session_ids(user_id)
    return []


def cmd_new(session: Session) -> CommandAction:
    """开始新的对话"""
    new_id = f"{_base_id(session.session_id)}:{_short_uid()}"

    user_manager = session._user_manager
    user_id = _parse_user_id(session.session_id)
    if user_manager and user_id:
        user_manager.bind_session(user_id, new_id)

    return CommandAction(
        action="switch",
        target_session_id=new_id,
        message="已开始新对话（旧对话可通过 /history 查看和切换）"
    )


def cmd_history(session: Session) -> CommandAction | str:
    """列出此用户所有历史对话"""
    contexts = _get_user_contexts_list(session)
    if not contexts:
        return "暂无其他对话"

    current = session.session_id
    lines = ["当前用户的所有对话:"]
    for sid in contexts:
        tag = " ← 当前" if sid == current else ""
        lines.append(f"  {sid}{tag}")
    return "\n".join(lines)


def cmd_switch(session: Session, target_id: str = "") -> str | CommandAction:
    """同步：切换会话"""
    if not target_id:
        return "用法: /switch <session_id>\n请通过 /history 查看可用对话的 session_id"

    contexts = _get_user_contexts_list(session)
    if target_id not in contexts:
        return f"对话不存在: {target_id}\n请通过 /history 查看可用对话"
    if target_id == session.session_id:
        return f"已在当前对话: {target_id}"

    return CommandAction(
        action="switch_session",
        target_session_id=target_id,
        message=f"已切换至: {target_id}"
    )


def cmd_about(text: str = "") -> str:
    """返回关于信息
    
    参数:
    - text: 可选的附加文本
    """
    return text


async def cmd_new_async(session: AsyncSession) -> CommandAction:
    """开始新的对话"""
    new_id = f"{_base_id(session.session_id)}:{_short_uid()}"

    user_manager = session._user_manager
    user_id = _parse_user_id(session.session_id)
    if user_manager and user_id:
        user_manager.bind_session(user_id, new_id)

    return CommandAction(
        action="switch",
        target_session_id=new_id,
        message="已开始新对话（旧对话可通过 /history 查看和切换）"
    )


async def cmd_history_async(session: AsyncSession) -> CommandAction | str:
    """列出此用户所有历史对话"""
    contexts = _get_user_contexts_list(session)
    if not contexts:
        return "暂无其他对话"

    current = session.session_id
    lines = ["当前用户的所有对话:"]
    for sid in contexts:
        tag = " ← 当前" if sid == current else ""
        lines.append(f"  {sid}{tag}")
    return "\n".join(lines)


async def cmd_switch_async(session: AsyncSession, target_id: str = "") -> str | CommandAction:
    """异步：切换会话"""
    if not target_id:
        return "用法: /switch <session_id>\n请通过 /history 查看可用对话的 session_id"

    contexts = _get_user_contexts_list(session)
    if target_id not in contexts:
        return f"对话不存在: {target_id}\n请通过 /history 查看可用对话"
    if target_id == session.session_id:
        return f"已在当前对话: {target_id}"

    return CommandAction(
        action="switch_session",
        target_session_id=target_id,
        message=f"已切换至: {target_id}"
    )


async def cmd_about_async(text: str = "") -> str:
    """返回关于信息
    
    参数:
    - text: 可选的附加文本
    """
    return text

