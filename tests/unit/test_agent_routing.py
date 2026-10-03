from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from satrap.core.config.agent_routing import AgentRouteStore, resolve_agent_binding, validate_session_bindings
from satrap.core.config.conversation_catalog import _route_metadata
from satrap.core.config.document import validate_platforms
from satrap.core.config.group_session import resolve_group_session
from satrap.core.conversation import ConversationRoute
from satrap.core.components import Plain
from satrap.core.framework.Base import Session
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.framework.providers import BindingState, BindingStatus
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.UserManager import UserManager
from satrap.core.pipeline.scheduler import PipelineScheduler
from satrap.core.pipeline.wake_window import WakeWindow
from satrap.core.platform import PlatformAdapter, PlatformAdapterManager, PlatformAdapterRegistry, PlatformConfig
from satrap.core.platform.event import MessageChain, MessageEvent, PlatformMetadata
from satrap.core.platform.receipt import SendReceipt
from satrap.core.type import MessageMember, PlatformMessage, PlatformMessageType


class Adapter(PlatformAdapter):
    adapter_type = "routing-test"

    async def run(self) -> None:
        pass

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)


class Echo(Session):
    def run(self, message: str) -> str:
        return message


def _bindings(name: str = "shared") -> dict[str, dict[str, str]]:
    return {kind: {"mode": "value", "provider": "session_class", "config_name": name} for kind in ("private", "group")}


def _event(adapter: Adapter, *, private: bool = False, group: str = "g1", actor: str = "u1") -> MessageEvent:
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE if private else PlatformMessageType.GROUP_MESSAGE
    message.self_id = "bot"
    message.session_id = actor if private else group
    message.group_id = "" if private else group
    message.message_id = "msg1"
    message.sender = MessageMember(user_id=actor, nickname="测试成员")
    message.message = [Plain("hello")]
    message.message_str = "hello"
    message.raw_message = {}
    return MessageEvent("hello", message, adapter.meta(), message.session_id, adapter,
                        adapter.get_session_provider(), adapter.get_session_type())


def _route(event: MessageEvent) -> ConversationRoute:
    return ConversationRoute(user_id=event.get_sender_id(), platform=event.get_platform_id(),
                             session_type=event.session_type, provider=event.session_provider,
                             scope=event.agent_context_scope, self_id=event.get_self_id(), group_id=event.get_group_id(),
                             conversation_kind=event.conversation_kind, conversation_id=event.conversation_id,
                             binding_generation=event.agent_route_generation)


@pytest.mark.parametrize("value", [[], {"group": {"mode": "inherit", "config_name": "x"}},
                                  {"group": {"mode": "value", "provider": "edictum"}},
                                  {"群": {"mode": "inherit"}},
                                  {"group": {"mode": "value", "provider": "edictum", "config_name": " "}}])
def test_invalid_binding_contract_is_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        validate_session_bindings(value)


def test_group_override_and_inherited_type_binding() -> None:
    platform = {"session_type": "default", "session_bindings": _bindings("group-default")}
    effective, sources = resolve_group_session(platform, {})
    assert effective["binding"] == {"provider": "session_class", "config_name": "group-default"}
    assert sources["binding"] == "conversation_kind"
    assert effective["scope"] == "group_member" and sources["scope"] == "conversation_kind"
    effective, sources = resolve_group_session(platform, {"binding": {"mode": "value", "value": {
        "provider": "edictum", "config_name": "only-this-group",
    }}})
    assert effective["binding"] == {"provider": "edictum", "config_name": "only-this-group"}
    assert sources["binding"] == "group"
    assert resolve_agent_binding({"session_type": "base", "session_bindings": {"group": {"mode": "inherit"}}}, "group")[0]["config_name"] == "base"


def test_platform_document_preserves_and_validates_bindings() -> None:
    platform = {"id": "test", "type": "new-adapter", "session_bindings": {"custom": {"mode": "inherit"}}}
    assert validate_platforms([platform])[0]["session_bindings"] == platform["session_bindings"]
    platform["session_bindings"] = {"custom": {"mode": "value"}}
    with pytest.raises(ValueError):
        validate_platforms([platform])


def test_persistent_generations_do_not_restore_old_binding(tmp_path: Path) -> None:
    path = tmp_path / "platform.db"
    store = AgentRouteStore(path)
    assert store.revision("bot", "group", "g", ("a",), enabled=False) == 0
    assert not path.exists()
    assert store.revision("bot", "group", "g", ("a",), enabled=True) == 1
    assert AgentRouteStore(path).revision("bot", "group", "g", ("a",), enabled=True) == 1
    assert store.revision("bot", "group", "g", ("b",), enabled=True) == 2
    assert store.revision("bot", "group", "g", ("a",), enabled=True) == 3
    assert store.revision("bot", "group", "g", ("a",), enabled=False) == 3
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _: AgentRouteStore(path).revision("bot", "group", "g", ("c",), enabled=True), range(20)))
    assert values == [4] * 20


def test_routes_isolate_chat_kinds_groups_and_members(tmp_path: Path) -> None:
    adapter = Adapter(PlatformConfig(id="test", session_type="shared", session_bindings=_bindings()))
    adapter.agent_route_store = AgentRouteStore(tmp_path / "platform.db")
    events = [_event(adapter, private=True), _event(adapter), _event(adapter, group="g2"), _event(adapter, actor="u2")]
    keys = [_route(event).key for event in events]
    assert len(set(keys)) == 4
    decoded = _route_metadata({"context_key": keys[0]})
    assert decoded["scope"] == "private" and decoded["conversation_kind"] == "private"
    assert decoded["conversation_id"] == "u1"
    adapter.config.settings["context_scope"] = "group"
    assert _route(_event(adapter)).key == _route(_event(adapter, actor="u2")).key
    assert _route(_event(adapter)).owner == ""


def test_distinct_agent_binding_and_group_override() -> None:
    bindings = _bindings()
    bindings["private"]["config_name"] = "private-agent"
    adapter = Adapter(PlatformConfig(session_type="base", session_bindings=bindings))
    assert _event(adapter, private=True).session_type == "private-agent"
    assert _event(adapter).session_type == "shared"
    adapter.group_route = lambda _: ({"binding": {"mode": "value", "value": {
        "provider": "edictum", "config_name": "group-override",
    }}}, 1)
    group = _event(adapter)
    assert group.session_type == "group-override" and group.agent_binding_source == "group"
    adapter.config.session_bindings["group"]["config_name"] = "new-default"
    assert group.agent_route_is_current()


def test_config_switch_without_messages_advances_only_affected_routes(tmp_path: Path) -> None:
    adapter = Adapter(PlatformConfig(id="test", session_type="shared", session_bindings=_bindings()))
    adapter.agent_route_store = AgentRouteStore(tmp_path / "platform.db")
    private = _event(adapter, private=True)
    group = _event(adapter)
    adapter.config.session_bindings["group"]["config_name"] = "different"
    assert adapter.apply_agent_routes() == 1
    adapter.config.session_bindings["group"]["config_name"] = "shared"
    assert adapter.apply_agent_routes() == 1
    fresh = _event(adapter)
    assert fresh.agent_route_generation == group.agent_route_generation + 2
    assert not group.agent_route_is_current()
    assert private.agent_route_is_current()
    restarted = Adapter(adapter.config)
    restarted.agent_route_store = AgentRouteStore(tmp_path / "platform.db")
    assert restarted.apply_agent_routes() == 0
    assert _route(_event(restarted)).key == _route(fresh).key


def test_config_switch_preserves_explicit_group_binding(tmp_path: Path) -> None:
    adapter = Adapter(PlatformConfig(id="test", session_type="shared", session_bindings=_bindings()))
    adapter.agent_route_store = AgentRouteStore(tmp_path / "platform.db")
    adapter.group_route = lambda _: ({"binding": {"mode": "value", "value": {
        "provider": "edictum", "config_name": "group-override",
    }}}, 1)
    original = _event(adapter)
    adapter.config.session_bindings["group"]["config_name"] = "new-default"
    assert adapter.apply_agent_routes() == 0
    assert original.agent_route_is_current()


def test_unknown_kind_and_missing_identity_fail_closed() -> None:
    adapter = Adapter(PlatformConfig(session_bindings=_bindings()))
    event = _event(adapter)
    event.platform_message.type = PlatformMessageType.OTHER_MESSAGE
    with pytest.raises(ValueError, match="对话类型"):
        _event_from_message = MessageEvent("hello", event.platform_message, adapter.meta(), "other", adapter)
    event.platform_message.type = PlatformMessageType.GROUP_MESSAGE
    event.platform_message.self_id = ""
    adapter.agent_route_store = AgentRouteStore(Path("unused.db"))
    with pytest.raises(ValueError, match="完整对话身份"):
        adapter.agent_route_state(event.platform_message)


def test_unconfigured_legacy_keeps_identity() -> None:
    adapter = Adapter(PlatformConfig(session_type="old"))
    event = _event(adapter)
    assert event.agent_route_generation == 0 and event.session_type == "old"
    assert ConversationRoute("u", "test", "old").key is None


def test_new_adapter_conversation_kind_needs_no_routing_branch(tmp_path: Path) -> None:
    class TopicAdapter(Adapter):
        conversation_kinds = {"topic": "主题讨论"}

        def conversation_kind(self, message: PlatformMessage) -> str:
            return "topic"

    adapter = TopicAdapter(PlatformConfig(id="new-platform", session_bindings={"topic": {
        "mode": "value", "provider": "edictum", "config_name": "topic-agent",
    }}))
    adapter.agent_route_store = AgentRouteStore(tmp_path / "platform.db")
    event = _event(adapter)
    assert event.conversation_kind == "topic" and event.session_type == "topic-agent"
    assert _route(event).scope == "conversation" and _route(event).owner == ""
    assert _route_metadata({"context_key": _route(event).key})["conversation_kind"] == "topic"


def test_backend_rejects_unknown_kind_and_disabled_explicit_agent() -> None:
    backend = BackendManager(BackendConfig())
    registry = PlatformAdapterRegistry()
    registry.register("routing-test", Adapter)
    backend._adapter_mgr = PlatformAdapterManager(registry=registry)
    manager = cast(Any, SimpleNamespace(provider_registry=SimpleNamespace(
        binding_status=lambda *_: BindingStatus(BindingState.DISABLED, "已禁用"),
    )))
    with pytest.raises(ValueError, match="未声明对话类型"):
        backend._require_agent_bindings(manager, "routing-test", {"topic": {"mode": "inherit"}})
    with pytest.raises(ValueError, match="绑定不可用"):
        backend._require_agent_bindings(manager, "routing-test", _bindings())


@pytest.mark.asyncio
async def test_stale_queue_window_and_reply_are_rejected() -> None:
    adapter = Adapter(PlatformConfig(session_type="shared", session_bindings=_bindings()))
    adapter.send_message = AsyncMock(return_value=SendReceipt("success", ("out1",)))
    old = _event(adapter)
    old_key = WakeWindow.key(old)
    adapter.config.session_bindings["group"]["config_name"] = "different"
    assert not old.agent_route_is_current()
    await old.send(MessageChain.from_text("不应发送"))
    adapter.send_message.assert_not_awaited()
    adapter.config.session_bindings["group"]["config_name"] = "shared"
    fresh = _event(adapter)
    assert WakeWindow.key(fresh) != old_key
    await fresh.send(MessageChain.from_text("新轮次"))
    adapter.send_message.assert_awaited_once()


def test_automatic_policy_remains_current_under_typed_scope() -> None:
    adapter = Adapter(PlatformConfig(session_bindings=_bindings(), settings={"wake_mode": "frequency"}))
    event = _event(adapter)
    assert event.policy_settings["context_scope"] == "group_member"
    assert PipelineScheduler._automatic_policy_current(event)
    adapter.config.session_bindings["group"]["config_name"] = "new"
    assert not PipelineScheduler._automatic_policy_current(event)


@pytest.mark.asyncio
async def test_stream_stops_when_agent_changes_between_chunks() -> None:
    adapter = Adapter(PlatformConfig(session_bindings=_bindings()))
    event = _event(adapter)
    sent: list[str] = []

    async def send_stream(session_id: str, generator: Any, **kwargs: Any) -> SendReceipt:
        async for chain in generator:
            sent.append(chain.components[0].text)
        return SendReceipt("success", ("out1",))

    async def chunks():
        yield MessageChain.from_text("第一块")
        adapter.config.session_bindings["group"]["config_name"] = "different"
        yield MessageChain.from_text("不应发送的第二块")

    adapter.send_stream = send_stream
    await event.send_streaming(chunks())
    assert sent == ["第一块"]


@pytest.mark.asyncio
async def test_real_pipeline_creates_separate_persistent_sessions(tmp_path: Path) -> None:
    classes = SessionClassConfigManager(storage_path=tmp_path / "classes.json")
    classes.register("shared", Echo)
    manager = SessionManager(default_session_type="shared", db_path=tmp_path / "platform.db")
    manager.register_session_type("shared", Echo)
    manager.class_cfg_mgr = classes
    users = UserManager(manager, db_path=tmp_path / "platform.db")
    adapter = Adapter(PlatformConfig(id="test", session_type="shared", session_bindings=_bindings(),
                                    settings={"wake_words": ["hello"]}))
    adapter.agent_route_store = AgentRouteStore(tmp_path / "platform.db")
    adapter.send_message = AsyncMock(return_value=SendReceipt("success", ("out1",)))
    scheduler = PipelineScheduler(manager, user_manager=users)
    events = [_event(adapter, private=True), _event(adapter), _event(adapter, group="g2")]
    try:
        for event in events:
            await scheduler.execute(event)
        assert adapter.send_message.await_count == 3
        identities = []
        for event in events:
            row = users.store.get_context_session("u1", "test", "shared", "session_class", context_key=_route(event).key)
            assert row is not None
            identities.append(row.session_id)
        assert len(set(identities)) == 3
        restarted_users = UserManager(manager, db_path=tmp_path / "platform.db")
        assert restarted_users.resolve_session("u1", "test", "shared", classes, route=_route(events[0])) == identities[0]
    finally:
        await scheduler.wake_timers.close()
        for session_id in list(manager.pool.list_entries()):
            await manager.unload_session_async(session_id)
