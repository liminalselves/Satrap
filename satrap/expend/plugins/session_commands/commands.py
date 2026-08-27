"""为同步和异步 Edictum 会话构建平台会话命令"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from satrap.core.framework.Base import AsyncSession, Session
from satrap.expend.command.session_commands import (
    cmd_about,
    cmd_about_async,
    cmd_history,
    cmd_history_async,
    cmd_new,
    cmd_new_async,
    cmd_switch,
    cmd_switch_async,
)


def _about_text(
    session: Session | AsyncSession,
    config: dict[str, Any] | None = None,
) -> str:
    """
    生成当前平台会话的关于信息

    参数:
    - session: 当前 Edictum 会话实例
    - config: 会话级插件配置

    返回:
    - str: 平台会话关于信息
    """
    custom_text = str((config or {}).get("about_text") or "")
    if custom_text.strip():
        return custom_text
    session_type = session.session_id.split(":", 1)[0] or "edictum"
    return (
        "Satrap AI 助手\n"
        f"会话类型: {session_type}\n"
        "支持多轮对话和多会话切换\n"
        "输入 /help 查看全部可用命令"
    )


def build_commands(
    session: Session | AsyncSession,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:
    """
    按会话同步形态构建已绑定当前实例的命令映射

    参数:
    - session: 当前 Edictum 会话实例
    - config: 会话级插件配置

    返回:
    - tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]: 同步和异步命令映射
    """
    if isinstance(session, AsyncSession):
        async def async_new_command() -> Any:
            """开始一个新对话"""
            return await cmd_new_async(session)

        async def async_history_command() -> Any:
            """查看当前用户的历史对话"""
            return await cmd_history_async(session)

        async def async_switch_command(target_id: str = "") -> Any:
            """按 Session ID 切换对话"""
            return await cmd_switch_async(session, target_id)

        async def async_about_command() -> Any:
            """查看 Satrap 平台会话信息"""
            return await cmd_about_async(text=_about_text(session, config))

        return {}, {
            "new": async_new_command,
            "history": async_history_command,
            "switch": async_switch_command,
            "about": async_about_command,
        }

    def new_command() -> Any:
        """开始一个新对话"""
        return cmd_new(session)

    def history_command() -> Any:
        """查看当前用户的历史对话"""
        return cmd_history(session)

    def switch_command(target_id: str = "") -> Any:
        """按 Session ID 切换对话"""
        return cmd_switch(session, target_id)

    def about_command() -> Any:
        """查看 Satrap 平台会话信息"""
        return cmd_about(_about_text(session, config))

    return {
        "new": new_command,
        "history": history_command,
        "switch": switch_command,
        "about": about_command,
    }, {}
