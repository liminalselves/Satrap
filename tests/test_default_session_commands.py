import pytest

from satrap.core.framework import AsyncCommandHandler, AsyncSession, CommandHandler, Session


class FakeUserManager:
    def __init__(self, sessions=None):
        self.sessions = list(sessions or [])
        self.bound = []

    def get_user_session_ids(self, user_id: str):
        return list(self.sessions)

    def bind_session(self, user_id: str, session_id: str):
        self.bound.append((user_id, session_id))
        if session_id not in self.sessions:
            self.sessions.append(session_id)
        return True


class DemoSession(Session):
    def __init__(self, session_id: str):
        super().__init__(session_id)
        self.switched = []

    def on_session_switched(self, old_session_id: str, new_session_id: str) -> None:
        self.switched.append((old_session_id, new_session_id))


class DemoAsyncSession(AsyncSession):
    def __init__(self, session_id: str):
        super().__init__(session_id)
        self.switched = []

    async def on_session_switched(self, old_session_id: str, new_session_id: str) -> None:
        self.switched.append((old_session_id, new_session_id))


def test_sync_session_registers_default_commands():
    session = DemoSession("chat:misskey:user1")

    assert {"help", "new", "history", "switch", "about"}.issubset(
        session.command_handler.commands.keys()
    )


@pytest.mark.asyncio
async def test_async_session_registers_default_commands():
    session = DemoAsyncSession("chat:misskey:user1")

    assert {"help", "new", "history", "switch", "about"}.issubset(
        session.command_handler.commands.keys()
    )


def test_history_without_user_manager_returns_empty_message():
    session = DemoSession("chat:misskey:user1")

    result, is_cmd = session.cmd_process("/history")

    assert is_cmd is True
    assert result == "暂无其他对话"


def test_new_creates_context_from_base_session_and_binds_user():
    session = DemoSession("chat:misskey:user1:old")
    user_manager = FakeUserManager(["chat:misskey:user1:old"])
    session._user_manager = user_manager

    result, is_cmd = session.cmd_process("/new")

    assert is_cmd is True
    assert result == "已开始新对话（旧对话可通过 /history 查看和切换）"
    assert session.session_id.startswith("chat:misskey:user1:")
    assert session.session_id != "chat:misskey:user1:old"
    assert user_manager.bound == [("user1", session.session_id)]
    assert session.switched == [("chat:misskey:user1:old", session.session_id)]


def test_switch_only_allows_bound_contexts():
    session = DemoSession("chat:misskey:user1")
    session._user_manager = FakeUserManager(["chat:misskey:user1", "chat:misskey:user1:next"])

    result, is_cmd = session.cmd_process("/switch chat:misskey:user1:missing")
    assert is_cmd is True
    assert result == "对话不存在: chat:misskey:user1:missing\n请通过 /history 查看可用对话"

    result, is_cmd = session.cmd_process("/switch chat:misskey:user1:next")
    assert is_cmd is True
    assert result == "已切换至: chat:misskey:user1:next"
    assert session.session_id == "chat:misskey:user1:next"
    assert session.switched == [("chat:misskey:user1", "chat:misskey:user1:next")]


def test_custom_command_can_override_default_command():
    class CustomSession(Session):
        def __init__(self):
            super().__init__("chat:misskey:user1")
            self.command_handler.register_command("about", lambda: "custom", "自定义")

    session = CustomSession()

    result, is_cmd = session.cmd_process("/about")

    assert is_cmd is True
    assert result == "custom"


def test_pre_registered_command_is_not_overwritten():
    handler = CommandHandler()
    handler.register_command("about", lambda: "pre", "预置")

    session = Session("chat:misskey:user1", command_handler=handler)
    result, is_cmd = session.cmd_process("/about")

    assert is_cmd is True
    assert result == "pre"


@pytest.mark.asyncio
async def test_async_switch_calls_hook():
    session = DemoAsyncSession("chat:onebot:user1")
    session._user_manager = FakeUserManager(["chat:onebot:user1", "chat:onebot:user1:next"])

    result, is_cmd = await session.cmd_process("/switch chat:onebot:user1:next")

    assert is_cmd is True
    assert result == "已切换至: chat:onebot:user1:next"
    assert session.session_id == "chat:onebot:user1:next"
    assert session.switched == [("chat:onebot:user1", "chat:onebot:user1:next")]


@pytest.mark.asyncio
async def test_pre_registered_async_command_is_not_overwritten():
    handler = AsyncCommandHandler()

    async def about():
        return "pre"

    handler.register_command("about", about, "预置")
    session = AsyncSession("chat:misskey:user1", command_handler=handler)

    result, is_cmd = await session.cmd_process("/about")

    assert is_cmd is True
    assert result == "pre"
