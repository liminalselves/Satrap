from __future__ import annotations

import time
from typing import Any

from satrap.core.utils.context import AsyncContextManager, ContextManager


def _extract_user_id(session_id: str) -> str | None:
    """从 session_id 中提取用户 ID, 忽略多对话时间戳后缀。"""
    parts = session_id.split(":")
    if len(parts) < 3:
        return None
    return parts[2]


def _base_session_id(session_id: str) -> str:
    """获取 type:platform:user_id 形式的基础 session_id。"""
    parts = session_id.split(":")
    if len(parts) >= 3:
        return ":".join(parts[:3])
    return session_id


def _get_user_contexts(session: Any) -> list[str]:
    """读取当前用户绑定的所有上下文。"""
    user_manager = getattr(session, "_user_manager", None)
    user_id = _extract_user_id(getattr(session, "session_id", ""))
    if not user_manager or not user_id:
        return []
    return list(user_manager.get_user_session_ids(user_id))


def _bind_session_to_user(session: Any, session_id: str) -> None:
    """将新上下文绑定到当前用户。"""
    user_manager = getattr(session, "_user_manager", None)
    user_id = _extract_user_id(getattr(session, "session_id", ""))
    if user_manager and user_id:
        user_manager.bind_session(user_id, session_id)


def _register_if_missing(command_handler: Any, name: str, handler: Any, intro: str) -> None:
    """仅在未存在同名命令时注册, 避免覆盖用户自定义命令。"""
    commands = getattr(command_handler, "commands", {})
    if name not in commands:
        command_handler.register_command(name, handler, intro)


def register_session_commands(session: Any) -> None:
    """为同步 Session 注册通用命令。"""

    def new_cmd() -> str:
        old_session_id = session.session_id
        new_session_id = f"{_base_session_id(old_session_id)}:{int(time.time())}"
        _bind_session_to_user(session, new_session_id)

        session.session_id = new_session_id
        session.session_ctx = ContextManager(new_session_id)
        session.session_ctx.load_context()

        hook = getattr(session, "on_session_switched", None)
        if hook:
            hook(old_session_id, new_session_id)
        return "已开始新对话（旧对话可通过 /history 查看和切换）"

    def history_cmd() -> str:
        contexts = _get_user_contexts(session)
        if not contexts:
            return "暂无其他对话"

        lines = ["当前用户的所有对话:"]
        for session_id in contexts:
            tag = " ← 当前" if session_id == session.session_id else ""
            lines.append(f"  {session_id}{tag}")
        return "\n".join(lines)

    def switch_cmd(target_id: str = "") -> str:
        if not target_id:
            return "用法: /switch <session_id>\n请通过 /history 查看可用对话的 session_id"

        contexts = _get_user_contexts(session)
        if target_id not in contexts:
            return f"对话不存在: {target_id}\n请通过 /history 查看可用对话"
        if target_id == session.session_id:
            return f"已在当前对话: {target_id}"

        old_session_id = session.session_id
        session.session_id = target_id
        session.session_ctx = ContextManager(target_id)
        session.session_ctx.load_context()

        hook = getattr(session, "on_session_switched", None)
        if hook:
            hook(old_session_id, target_id)
        return f"已切换至: {target_id}"

    def about_cmd() -> str:
        provider = getattr(session, "get_about_text", None)
        if provider:
            return str(provider())
        return "Satrap AI 助手\n基于 Satrap 框架\n功能: 多轮对话 / 多对话切换"

    _register_if_missing(session.command_handler, "new", new_cmd, "开始新对话")
    _register_if_missing(session.command_handler, "history", history_cmd, "查看此用户的所有对话")
    _register_if_missing(session.command_handler, "switch", switch_cmd, "切换至指定对话: /switch <session_id>")
    _register_if_missing(session.command_handler, "about", about_cmd, "关于")


def register_async_session_commands(session: Any) -> None:
    """为异步 Session 注册通用命令。"""

    async def new_cmd() -> str:
        old_session_id = session.session_id
        new_session_id = f"{_base_session_id(old_session_id)}:{int(time.time())}"
        _bind_session_to_user(session, new_session_id)

        session.session_id = new_session_id
        session.session_ctx = AsyncContextManager(new_session_id)
        await session.session_ctx.initialize()

        hook = getattr(session, "on_session_switched", None)
        if hook:
            await hook(old_session_id, new_session_id)
        return "已开始新对话（旧对话可通过 /history 查看和切换）"

    async def history_cmd() -> str:
        contexts = _get_user_contexts(session)
        if not contexts:
            return "暂无其他对话"

        lines = ["当前用户的所有对话:"]
        for session_id in contexts:
            tag = " ← 当前" if session_id == session.session_id else ""
            lines.append(f"  {session_id}{tag}")
        return "\n".join(lines)

    async def switch_cmd(target_id: str = "") -> str:
        if not target_id:
            return "用法: /switch <session_id>\n请通过 /history 查看可用对话的 session_id"

        contexts = _get_user_contexts(session)
        if target_id not in contexts:
            return f"对话不存在: {target_id}\n请通过 /history 查看可用对话"
        if target_id == session.session_id:
            return f"已在当前对话: {target_id}"

        old_session_id = session.session_id
        session.session_id = target_id
        session.session_ctx = AsyncContextManager(target_id)
        await session.session_ctx.initialize()

        hook = getattr(session, "on_session_switched", None)
        if hook:
            await hook(old_session_id, target_id)
        return f"已切换至: {target_id}"

    async def about_cmd() -> str:
        provider = getattr(session, "get_about_text", None)
        if provider:
            return str(provider())
        return "Satrap AI 助手\n基于 Satrap 框架\n功能: 多轮对话 / 多对话切换"

    _register_if_missing(session.command_handler, "new", new_cmd, "开始新对话")
    _register_if_missing(session.command_handler, "history", history_cmd, "查看此用户的所有对话")
    _register_if_missing(session.command_handler, "switch", switch_cmd, "切换至指定对话: /switch <session_id>")
    _register_if_missing(session.command_handler, "about", about_cmd, "关于")
