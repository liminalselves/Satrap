"""group_admin 插件工具的身份, 开关, 群范围与执行链路"""
from unittest.mock import AsyncMock
from typing import Any, cast
import asyncio

from aiocqhttp.exceptions import ActionFailed
import pytest
import yaml

from satrap.expend.plugins.group_admin.tools import _DEFINITIONS, AsyncGroupAdminTool, get_tools
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.framework.Base import Session
from satrap.core.platform import (
    PlatformAdapterManager,
    PlatformAdapterRegistry,
    PlatformConfig,
    current_adapter_manager,
    set_current_adapter_manager,
)
from satrap.core.platform.onebot.adapter import OneBotAdapter


def _origin(chat_type: str = "GroupMessage", chat_id: str = "456", actor: str = "123") -> CallOrigin:
    return CallOrigin(
        adapter_id="ob", self_id="10000", chat_type=chat_type, chat_id=chat_id,
        actor_id=actor, source_message_id="77", request_id="r1",
    )


def _setup_adapter(**settings: object) -> OneBotAdapter:
    registry = PlatformAdapterRegistry()
    registry.register("onebot", OneBotAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    assert isinstance(adapter, OneBotAdapter)
    adapter._bot = AsyncMock()
    set_current_adapter_manager(manager)
    return adapter


@pytest.fixture(autouse=True)
def _clear_manager():
    yield
    set_current_adapter_manager(None)


def test_meta_declares_exactly_the_implemented_tools():
    from pathlib import Path
    meta_path = Path(__file__).parent.parent.parent / "satrap" / "expend" / "plugins" / "group_admin" / "meta.yaml"
    meta = cast(dict[str, Any], yaml.safe_load(meta_path.read_text(encoding="utf-8")))
    assert set(meta["tools"]) == set(_DEFINITIONS)


def test_meta_write_switch_parses_to_real_bool():
    from pathlib import Path
    from typing import Any, cast
    from satrap.edictum.plugin_config import parse_config_schema
    meta_path = Path(__file__).parent.parent.parent / "satrap" / "expend" / "plugins" / "group_admin" / "meta.yaml"
    schema = parse_config_schema(cast(dict[str, Any], yaml.safe_load(meta_path.read_text(encoding="utf-8"))))
    field = schema["write_tools_enabled"]
    assert field.type == "bool"
    assert field.validate(True) is True and field.validate(False) is False
    assert field.validate(None) is False


def _async_tools(config: dict[str, Any]) -> list[AsyncGroupAdminTool]:
    """构造异步工具实例, 模拟 async_simple 会话"""
    from satrap.edictum import AsyncSimpleSession

    class _FakeAsync(AsyncSimpleSession):
        def __init__(self) -> None:
            pass

    return [tool for tool in get_tools(_FakeAsync(), config) if isinstance(tool, AsyncGroupAdminTool)]


class TestToolConstruction:
    def test_sync_session_gets_sync_tools(self):
        tools = get_tools(cast(Session, object()), {})
        assert len(tools) == len(_DEFINITIONS) == 21
        assert not any(isinstance(t, type) for t in tools)
        ban = next(t for t in tools if t.tool_name == "group_admin_ban")
        assert ban.recovery_policy == "manual"
        info = next(t for t in tools if t.tool_name == "group_admin_list_groups")
        assert info.recovery_policy == "retry"
        definition = ban.get_tool_defined()
        assert definition["function"]["parameters"]["required"] == ["user_id"]
        assert definition["function"]["parameters"]["additionalProperties"] is False

    def test_async_session_gets_async_tools(self):
        tools = _async_tools({})
        assert len(tools) == 21
        assert all(asyncio.iscoroutinefunction(t.execute) for t in tools)


class TestPermissionGate:
    @pytest.mark.asyncio
    async def test_missing_origin_is_rejected(self):
        _setup_adapter()
        tool = next(t for t in get_tools(cast(Session, object()), {}) if t.tool_name == "group_admin_get_group_info")
        result = await asyncio.get_running_loop().run_in_executor(None, lambda: tool.execute())
        assert result["status"] == "error" and "来源身份" in result["error"]

    @pytest.mark.asyncio
    async def test_write_requires_explicit_enable(self):
        _setup_adapter()
        with bind_call_origin(_origin()):
            tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_kick")
            result = await tool.execute(user_id="321")
        assert result["status"] == "error" and "写操作" in result["error"]

    @pytest.mark.asyncio
    async def test_caller_and_group_allowlists(self):
        _setup_adapter()
        tool = next(t for t in _async_tools({"write_tools_enabled": True, "allowed_callers": "999"}) if t.tool_name == "group_admin_kick")
        with bind_call_origin(_origin(actor="123")):
            result = await tool.execute(user_id="321")
        assert result["status"] == "error" and "调用者" in result["error"]
        tool = next(t for t in _async_tools({"write_tools_enabled": True, "allowed_groups": "789"}) if t.tool_name == "group_admin_kick")
        with bind_call_origin(_origin()):
            result = await tool.execute(user_id="321")
        assert result["status"] == "error" and "目标群" in result["error"]

    @pytest.mark.asyncio
    async def test_private_context_requires_explicit_group(self):
        _setup_adapter()
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_group_info")
        with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
            result = await tool.execute()
        assert result["status"] == "error" and "group_id" in result["error"]


class TestExecution:
    @pytest.mark.asyncio
    async def test_ban_executes_with_current_group_and_parsed_params(self):
        adapter = _setup_adapter()
        adapter._bot.set_group_ban.return_value = {}
        config = {"write_tools_enabled": True}
        tool = next(t for t in _async_tools(config) if t.tool_name == "group_admin_ban")
        with bind_call_origin(_origin()):
            result = await tool.execute(user_id="321", duration=600)
        assert result == {"status": "ok"}
        adapter._bot.set_group_ban.assert_awaited_once_with(group_id=456, user_id=321, duration=600)

    @pytest.mark.asyncio
    async def test_read_tool_works_without_write_enable(self):
        adapter = _setup_adapter()
        adapter._bot.get_group_info.return_value = {"group_id": 456, "group_name": "测试群", "secret": "x"}
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_group_info")
        with bind_call_origin(_origin()):
            result = await tool.execute()
        assert result["status"] == "ok" and result["data"]["group_name"] == "测试群"
        assert "secret" not in result["data"]

    @pytest.mark.asyncio
    async def test_send_forward_tool_uses_shared_outbound_path(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _setup_adapter()
        adapter.bot_self_id = "10000"
        adapter._running = True
        adapter._bot.send_group_forward_msg.return_value = {"message_id": 555}
        calls: list[str] = []
        original_run = adapter._outbound.run

        async def spy_run(target: str, operation: Any) -> Any:
            calls.append(target)
            return await original_run(target, operation)

        monkeypatch.setattr(adapter._outbound, "run", spy_run)
        tool = next(t for t in _async_tools({"write_tools_enabled": True}) if t.tool_name == "group_admin_send_forward")
        with bind_call_origin(_origin()):
            result = await tool.execute(nodes=[{"content": "第一段"}, {"content": "第二段", "name": "助手"}])
        assert result["status"] == "ok"
        assert result["data"]["status"] == "success" and result["data"]["message_ids"] == ["555"]
        assert calls == ["group%456"]
        adapter._bot.send_group_forward_msg.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_forward_write_switch_and_group_allowlist(self):
        _setup_adapter()
        denied = next(t for t in _async_tools({}) if t.tool_name == "group_admin_send_forward")
        with bind_call_origin(_origin()):
            result = await denied.execute(nodes=[{"content": "x"}])
        assert result["status"] == "error" and "写操作" in result["error"]
        tool = next(t for t in _async_tools({"write_tools_enabled": True, "allowed_groups": "789"}) if t.tool_name == "group_admin_send_forward")
        with bind_call_origin(_origin()):
            result = await tool.execute(nodes=[{"content": "x"}])
        assert result["status"] == "error" and "目标群" in result["error"]

    @pytest.mark.asyncio
    async def test_get_message_tool_rejects_cross_group_read(self):
        adapter = _setup_adapter()
        adapter._bot.get_msg.return_value = {"message_type": "group", "group_id": 999}
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_message")
        with bind_call_origin(_origin()):
            result = await tool.execute(message_id="77")
        assert result["status"] == "error" and "已拒绝读取" in result["error"]

    @pytest.mark.asyncio
    async def test_get_forward_tool_reads_nodes_without_write_enable(self):
        adapter = _setup_adapter()
        adapter._bot.get_forward_msg.return_value = {
            "messages": [
                {"user_id": 123, "nickname": "甲", "time": 1, "content": [{"type": "text", "data": {"text": "第一条"}}]},
            ],
        }
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_get_forward")
        with bind_call_origin(_origin()):
            result = await tool.execute(forward_id="fwd-1")
        assert result["status"] == "ok"
        assert result["data"] == [{"name": "甲", "uin": "123", "time": 1, "text": "第一条"}]

    @pytest.mark.asyncio
    async def test_sync_tool_bridges_to_platform_loop(self):
        adapter = _setup_adapter()
        adapter._loop = asyncio.get_running_loop()
        adapter._bot.set_group_kick.return_value = {}
        config = {"write_tools_enabled": True}
        tool = next(t for t in get_tools(cast(Session, object()), config) if t.tool_name == "group_admin_kick")

        def run() -> dict[str, object]:
            with bind_call_origin(_origin()):
                return tool.execute(user_id="321")

        result = await asyncio.to_thread(run)
        assert result == {"status": "ok"}
        adapter._bot.set_group_kick.assert_awaited_once_with(group_id=456, user_id=321, reject_add_request=False)

    @pytest.mark.asyncio
    async def test_sync_tool_without_loop_reports_error(self):
        _setup_adapter()
        config = {"write_tools_enabled": True}
        tool = next(t for t in get_tools(cast(Session, object()), config) if t.tool_name == "group_admin_kick")
        with bind_call_origin(_origin()):
            result = await asyncio.to_thread(tool.execute, user_id="321")
        assert result["status"] == "error" and "事件循环" in result["error"]

    @pytest.mark.asyncio
    async def test_group_request_requires_matching_sub_type(self):
        adapter = _setup_adapter()
        adapter._bot.set_group_add_request.return_value = {}
        adapter.request_flags.register("group", "f1", group_id="456", sub_type="add", user_id="1")
        config = {"write_tools_enabled": True}
        tool = next(t for t in _async_tools(config) if t.tool_name == "group_admin_handle_group_request")
        with bind_call_origin(_origin(chat_type="FriendMessage", chat_id="123")):
            missing = await tool.execute(flag="f1", sub_type="add", approve=True)
            bad = await tool.execute(flag="f1", sub_type="other", approve=True, group_id="456")
            ok = await tool.execute(flag="f1", sub_type="add", approve=False, reason="拒绝", group_id="456")
        assert missing["status"] == "error" and "group_id" in missing["error"]
        assert bad["status"] == "error" and "sub_type" in bad["error"]
        assert ok == {"status": "ok"}
        adapter._bot.set_group_add_request.assert_awaited_once_with(flag="f1", sub_type="add", approve=False, reason="拒绝")

    @pytest.mark.asyncio
    async def test_unknown_platform_returns_unsupported(self):
        from satrap.core.platform import PlatformAdapter
        from satrap.core.platform.event import PlatformMetadata

        class _Bare(PlatformAdapter):
            adapter_type = "bare"

            async def run(self) -> None:
                return None

            def meta(self) -> PlatformMetadata:
                return PlatformMetadata(name="bare", id="ob")

        registry = PlatformAdapterRegistry()
        registry.register("bare", _Bare)
        manager = PlatformAdapterManager(registry=registry)
        assert manager.add_adapter(PlatformConfig(id="ob", type="bare")) is not None
        set_current_adapter_manager(manager)
        tool = next(t for t in _async_tools({}) if t.tool_name == "group_admin_list_groups")
        with bind_call_origin(_origin()):
            result = await tool.execute()
        assert result["status"] == "unsupported"


class TestLoggingAndTimeout:
    @pytest.mark.asyncio
    async def test_write_action_audit_and_failure_logged(self, caplog: pytest.LogCaptureFixture):
        import logging
        adapter = _setup_adapter()
        adapter._bot.set_group_kick.return_value = {}
        tool = next(t for t in _async_tools({"write_tools_enabled": True}) if t.tool_name == "group_admin_kick")
        with caplog.at_level(logging.DEBUG), bind_call_origin(_origin()):
            assert (await tool.execute(user_id="321"))["status"] == "ok"
            adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 1200})
            assert (await tool.execute(user_id="321"))["status"] == "error"
            denied = await next(t for t in _async_tools({}) if t.tool_name == "group_admin_kick").execute(user_id="321")
        assert denied["status"] == "error"
        messages = [r.getMessage() for r in caplog.records if not r.name.endswith("_file")]
        assert any("写动作完成 tool=group_admin_kick actor=123 chat=456" in m for m in messages)
        assert any("动作失败 tool=group_admin_kick" in m and "AdminActionRejected" in m for m in messages)
        assert any("权限拒绝 tool=group_admin_kick" in m for m in messages)

    @pytest.mark.asyncio
    async def test_sync_tool_timeout_cancels_and_reports_unconfirmed(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
        import logging
        from satrap.expend.plugins.group_admin import tools as module
        adapter = _setup_adapter()
        adapter._loop = asyncio.get_running_loop()
        started = asyncio.Event()

        async def slow(**kwargs: object) -> dict[str, object]:
            started.set()
            await asyncio.sleep(30)
            return {}

        adapter._bot.set_group_kick = slow
        original = module.asyncio.run_coroutine_threadsafe

        def fast_timeout(coro: Any, loop: Any) -> Any:
            future: Any = original(coro, loop)
            real_result = future.result
            future.result = lambda timeout=None: real_result(timeout=0.05)
            return future

        monkeypatch.setattr(module.asyncio, "run_coroutine_threadsafe", fast_timeout)
        tool = next(t for t in get_tools(cast(Session, object()), {"write_tools_enabled": True}) if t.tool_name == "group_admin_kick")

        def run() -> Any:
            with bind_call_origin(_origin()):
                return tool.execute(user_id="321")

        with caplog.at_level(logging.WARNING):
            result = await asyncio.to_thread(run)
        assert result["status"] == "unconfirmed"
        assert any("动作超时已取消 tool=group_admin_kick" in r.getMessage() for r in caplog.records)
