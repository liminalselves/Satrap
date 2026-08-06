import pytest

from satrap.core.framework import AsyncSession, CommandHandler, Session
from satrap.core.type import CommandAction
from satrap.expend.command.session_commands import (
    cmd_about,
    cmd_history,
    cmd_history_async,
    cmd_new,
    cmd_new_async,
    cmd_switch,
    cmd_switch_async,
)


class FakeUserManager:
    def __init__(self, sessions: list[str] | None = None):
        self.sessions = list(sessions or [])
        self.bound: list[tuple[str, str]] = []

    def get_user_session_ids(self, user_id: str):
        return list(self.sessions)

    def bind_session(self, user_id: str, session_id: str):
        self.bound.append((user_id, session_id))
        if session_id not in self.sessions:
            self.sessions.append(session_id)
        return True


def test_base_session_registers_only_framework_commands_by_default():
    session = Session("chat:misskey:user1")

    assert set(session.command_handler.commands) == {"help"}


def test_sync_session_command_handler_supports_custom_commands():
    handler = CommandHandler()
    handler.register_command("about", lambda: "custom", "自定义")
    session = Session("chat:misskey:user1", command_handler=handler)

    result, is_command = session.cmd_process("/about")

    assert is_command is True
    assert result == "custom"


def test_pre_registered_command_is_preserved():
    handler = CommandHandler()
    handler.register_command("about", lambda: "pre", "预置")
    session = Session("chat:misskey:user1", command_handler=handler)

    assert session.cmd_process("/about") == ("pre", True)


def test_sync_history_without_user_manager_returns_empty_message():
    session = Session("chat:misskey:user1")

    assert cmd_history(session) == "暂无其他对话"


def test_sync_new_command_creates_action_and_binds_user():
    session = Session("chat:misskey:user1:old")
    user_manager = FakeUserManager(["chat:misskey:user1:old"])
    session._user_manager = user_manager   # type: ignore[assignment]

    result = cmd_new(session)

    assert isinstance(result, CommandAction)
    assert result.action == "switch"
    assert result.target_session_id is not None
    assert result.target_session_id.startswith("chat:misskey:user1:")
    assert result.target_session_id != session.session_id
    assert result.message == "已开始新对话（旧对话可通过 /history 查看和切换）"
    assert user_manager.bound == [("user1", result.target_session_id)]


def test_sync_switch_validates_bound_contexts():
    session = Session("chat:misskey:user1")
    session._user_manager = FakeUserManager(   # type: ignore[assignment]
        ["chat:misskey:user1", "chat:misskey:user1:next"],
    )

    assert cmd_switch(session, "chat:misskey:user1:missing") == (
        "对话不存在: chat:misskey:user1:missing\n请通过 /history 查看可用对话"
    )
    result = cmd_switch(session, "chat:misskey:user1:next")

    assert isinstance(result, CommandAction)
    assert result.action == "switch_session"
    assert result.target_session_id == "chat:misskey:user1:next"


def test_about_command_returns_configured_text():
    assert cmd_about("关于 Satrap") == "关于 Satrap"


@pytest.mark.asyncio
async def test_async_commands_match_sync_command_contract():
    session = AsyncSession("chat:misskey:user1:old")
    user_manager = FakeUserManager(
        ["chat:misskey:user1:old", "chat:misskey:user1:next"],
    )
    session._user_manager = user_manager   # type: ignore[assignment]

    history = await cmd_history_async(session)
    new_action = await cmd_new_async(session)
    switch_action = await cmd_switch_async(session, "chat:misskey:user1:next")

    assert isinstance(history, str)
    assert "chat:misskey:user1:old" in history
    assert isinstance(new_action, CommandAction)
    assert new_action.target_session_id is not None
    assert new_action.target_session_id.startswith("chat:misskey:user1:")
    assert isinstance(switch_action, CommandAction)
    assert switch_action.target_session_id == "chat:misskey:user1:next"


@pytest.mark.asyncio
async def test_async_pre_registered_command_is_preserved():
    from satrap.core.framework import AsyncCommandHandler

    handler = AsyncCommandHandler()

    async def about():
        return "pre"

    handler.register_command("about", about, "预置")
    session = AsyncSession("chat:misskey:user1", command_handler=handler)

    assert await session.cmd_process("/about") == ("pre", True)
