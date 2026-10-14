"""业务链路场景与集中边界契约"""
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
import argparse
import asyncio
from contextlib import closing
import json
from satrap.core.backend import control_server as control
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.agent_references import list_agent_references
from satrap.core.config.document import load_config_document, save_config_document
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.group_store import GroupConfigStore
from satrap.core.config.group_session import group_binding_chain
from satrap.core.config.edictum_service import EdictumConfigService
from satrap.core.framework.BackGroundManager import ConfigInUseError, ConfigReferenceScanError
from satrap.core.platform import registry, catalog
from satrap.core.storage import StorageLayout
from satrap.cli.cmd_session import _configured_adapter_ids
from satrap.core.platform import EventDispatcher

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
    assert group.session_type == "group-override"
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

class FutureAdapter(PlatformAdapter):
    """声明新增类型并拒绝实例化, 验证冷目录不启动平台"""

    conversation_kinds = {"private": "私聊", "group": "群聊", "topic": "话题"}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("声明目录不得创建适配器")

@pytest.fixture
def future_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry._mapping, "future_test", FutureAdapter)

def test_cold_catalog_supports_registered_future_types(future_adapter: None) -> None:
    item = next(item for item in catalog.adapter_catalog() if item["type"] == "future_test")
    assert item["conversation_kinds"] == FutureAdapter.conversation_kinds
    assert item["status"] == "available"

def test_cold_catalog_reports_import_failure_without_hiding_other_types(monkeypatch: pytest.MonkeyPatch, future_adapter: None) -> None:
    original = catalog.importlib.import_module
    monkeypatch.setattr(catalog.pkgutil, "iter_modules", lambda _: [SimpleNamespace(name="broken", ispkg=True)])
    monkeypatch.setattr(catalog.importlib.util, "find_spec", lambda _: object())

    def import_module(name: str) -> Any:
        """模拟单个适配器的依赖加载失败"""
        if name.endswith(".broken.adapter"):
            raise ImportError("missing optional dependency")
        return original(name)

    monkeypatch.setattr(catalog.importlib, "import_module", import_module)
    items = {item["type"]: item for item in catalog.adapter_catalog()}
    assert items["broken"]["status"] == "unavailable"
    assert items["future_test"]["status"] == "available"

@pytest.mark.asyncio
async def test_control_platforms_returns_cold_declarations_and_preserves_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, future_adapter: None) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    platform = {"id": "bot", "type": "future_test", "settings": {}, "session_bindings": {
        "private": {"mode": "value", "provider": "edictum", "config_name": "personal"},
        "group": {"mode": "inherit"},
        "topic": {"mode": "value", "provider": "session_class", "config_name": "topic-flow"},
    }}
    save_config_document(path, {"default_session_type": "default-flow", "platforms": [platform]})
    response = await control._route_config_document(control._RouteContext("GET", "/config/platforms", "/config/platforms", asyncio.StreamReader(), b""))
    assert response is not None and response[0] == 200
    data = response[1]
    assert data["default_session_type"] == "default-flow"
    assert next(item for item in cast(list[dict[str, Any]], data["adapter_types"]) if item["type"] == "future_test")["conversation_kinds"]["topic"] == "话题"
    platform["settings"] = {"extension": "kept"}
    body = json.dumps(platform).encode("utf-8")
    reader = asyncio.StreamReader()
    reader.feed_data(body)
    reader.feed_eof()
    route = "/config/platforms/bot"
    response = await control._route_config_document(control._RouteContext("PUT", route, f"{route}?expected_revision={data['revision']}", reader,
        f"PUT {route} HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode("utf-8")))
    assert response is not None and response[0] == 200
    assert load_config_document(path)["platforms"][0]["session_bindings"] == platform["session_bindings"]

def test_all_binding_levels_and_provider_boundaries(tmp_path: Path) -> None:
    layout = StorageLayout(tmp_path)
    store = GroupConfigStore(layout.platform_db("bot"))
    store.adopt_legacy("100", {"group_management_version": 1})
    store.patch_group("100", "123", "session", {"binding": {"mode": "value", "value": {
        "provider": "edictum", "config_name": "shared",
    }}}, expected_revision=0)
    platforms = [{"id": "bot", "type": "future_test", "enable": False,
                  "session_provider": "edictum", "session_type": "shared", "session_bindings": {
        "topic": {"mode": "value", "provider": "edictum", "config_name": "shared"},
        "group": {"mode": "inherit"},
        "private": {"mode": "value", "provider": "session_class", "config_name": "shared"},
    }}]
    refs = list_agent_references("edictum", "shared", platforms=platforms, layout=layout)
    assert {item["kind"] for item in refs} == {"platform_binding", "conversation_kind_binding", "group_binding"}
    assert len(refs) == 3
    classes = list_agent_references("session_class", "shared", platforms=platforms, layout=layout)
    assert len(classes) == 1 and classes[0]["conversation_kind"] == "private"
    assert list_agent_references("edictum", "unused", platforms=platforms, layout=layout) == []

def test_implicit_platform_defaults_are_resolved(tmp_path: Path) -> None:
    kwargs = {"platforms": [{"id": "bot", "type": "future_test"}], "layout": StorageLayout(tmp_path), "default_session_type": "fallback"}
    assert list_agent_references("session_class", "fallback", **kwargs)
    assert not list_agent_references("session_class", "fallback", **kwargs, session_classes={"future_test": {}})
    assert list_agent_references("session_class", "future_test", **kwargs, session_classes={"future_test": {}})
    assert list_agent_references("edictum", "fallback", platforms=[{"id": "bot", "type": "future_test", "session_provider": "edictum"}],
                                 layout=StorageLayout(tmp_path), default_session_type="fallback")

@pytest.mark.parametrize("provider", ["session_class", "edictum"])
@pytest.mark.parametrize("entry", ["control", "backend", "cli"])
@pytest.mark.asyncio
async def test_reference_guard_across_control_backend_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, entry: str) -> None:
    monkeypatch.setattr(control, "_check_backend_health", lambda: {"running": False})
    scan = tmp_path / "reference_guard_sessions"
    scan.mkdir()
    (scan / "example.py").write_text("class Session:\n    pass\n", encoding="utf-8")
    config = BackendConfig(data_root=str(tmp_path / "data"), session_scan_paths=[str(scan)],
        session_class_config_path=str(tmp_path / "classes.json"), edictum_config_path=str(tmp_path / "edictum.json"),
        platforms=[{"id": "bot", "type": "future_test", "session_bindings": {
            "private": {"mode": "value", "provider": provider, "config_name": "bound"},
        }}])
    document = {"data_root": config.data_root, "session_class_config_path": config.session_class_config_path,
                "session_scan_paths": config.session_scan_paths, "edictum_config_path": config.edictum_config_path, "platforms": config.platforms}
    path = tmp_path / "config.json"
    save_config_document(path, document)
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    service = control._session_class_config_service() if provider == "session_class" else control._edictum_config_service()
    service.create({"name": "bound", **({"class_path": "reference_guard_sessions.example.Session"} if provider == "session_class" else {"edictum_type": "simple"})})
    if entry == "backend":
        backend = BackendManager(config)
        if provider == "session_class":
            backend._session_cls_cfg = cast(SessionClassConfigManager, service.manager)
        else:
            typed = cast(EdictumConfigService, service)
            backend._edictum_cfg = typed.manager
            backend._edictum_types = typed.type_registry
        service.reference_checker = lambda name: backend.group_resource_references(provider, name)
        server = BackendHTTPServer(backend)
        route = "/api/config/session-classes/bound" if provider == "session_class" else "/api/config/edictum/sessions/bound"
        for method, body in (("DELETE", b""), ("PATCH", b'{"name":"renamed"}')):
            response = await server._route(method, route, body)
            assert response[0] == 409 and response[1]["code"] == "config_in_use"
            assert response[1]["references"][0]["conversation_kind"] == "private"
    elif entry == "control":
        route = "/config/session-classes/bound" if provider == "session_class" else "/config/edictum/sessions/bound"
        handler = control._route_session_class_details if provider == "session_class" else control._route_edictum_mutations
        for method, body in (("DELETE", b""), ("PATCH", b'{"name":"renamed"}')):
            reader = asyncio.StreamReader()
            reader.feed_data(body)
            reader.feed_eof()
            context = control._RouteContext(method, route, route, reader,
                f"{method} {route} HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode("utf-8"))
            response = await handler(context)
            assert response is not None and response[0] == 409 and response[1]["code"] == "config_in_use"
    elif entry == "cli":
        from satrap.cli import cmd_edictum, cmd_session

        if provider == "edictum":
            monkeypatch.setattr(cmd_edictum, "load_cli_config", lambda _: config)
            service = cmd_edictum._edictum_service(argparse.Namespace())
        else:
            monkeypatch.setattr(cmd_session, "load_cli_config", lambda _: config)
            monkeypatch.setattr(cmd_session, "_client_or_fallback", lambda _: service.manager)
            with pytest.raises(ConfigInUseError):
                cmd_session.cmd_session_unregister(argparse.Namespace(name="bound"))
            assert service.get("bound") is not None
            return
    with pytest.raises(ConfigInUseError) as caught:
        service.delete("bound")
    assert caught.value.references[0]["kind"] == "conversation_kind_binding"
    with pytest.raises(ConfigInUseError):
        service.update("bound", {"name": "renamed"})
    assert service.get("bound") is not None

def test_active_route_remains_protected_after_failed_application(tmp_path: Path) -> None:
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[{
        "id": "bot", "type": "future_test", "session_provider": "edictum", "session_type": "new",
    }]))
    backend._platform_active_configs["bot"] = {"id": "bot", "type": "future_test", "session_provider": "edictum", "session_type": "old"}
    assert backend.group_resource_references("edictum", "old")
    assert backend.group_resource_references("edictum", "new")

def test_control_checks_active_binding_and_refuses_incomplete_runtime_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.json"
    save_config_document(path, {"data_root": str(tmp_path), "edictum_config_path": str(tmp_path / "edictum.json"), "platforms": []})
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    monkeypatch.setattr(control, "_check_backend_health", lambda: {"running": True, "adapters": {
        "bot": {"config_type": "future_test", "session_provider": "edictum", "session_type": "old", "session_bindings": {}},
    }})
    service = control._edictum_config_service()
    service.create({"name": "old", "edictum_type": "simple"})
    with pytest.raises(ConfigInUseError):
        service.delete("old")
    monkeypatch.setattr(control, "_check_backend_health", lambda: {"running": True})
    with pytest.raises(ConfigReferenceScanError):
        service.delete("old")
    assert service.get("old") is not None

def test_group_binding_chain_exposes_effective_inheritance() -> None:
    platform = {"session_provider": "edictum", "session_type": "base", "session_bindings": {
        "group": {"mode": "value", "provider": "edictum", "config_name": "group-agent"},
    }}
    layers = group_binding_chain(platform, {})
    assert [(layer["mode"], layer["config_name"]) for layer in layers] == [("value", "base"), ("value", "group-agent"), ("inherit", "group-agent")]
    layers = group_binding_chain(platform, {"binding": {"mode": "value", "value": {"provider": "session_class", "config_name": "local-flow"}}})
    assert layers[-1] == {"source": "group", "mode": "value", "provider": "session_class", "config_name": "local-flow"}

def test_corrupt_binding_scan_refuses_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control, "_check_backend_health", lambda: {"running": False})
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"edictum_config_path": str(tmp_path / "edictum.json"), "platforms": [
        {"id": "bot", "type": "future_test", "session_bindings": {"private": {"mode": "value"}}},
    ]}), encoding="utf-8")
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    service = control._edictum_config_service()
    service.create({"name": "bound", "edictum_type": "simple"})
    with pytest.raises(ConfigReferenceScanError):
        service.delete("bound")
    assert service.get("bound") is not None

def test_group_summary_only_counts_current_agent_generation(tmp_path: Path) -> None:
    store = GroupDirectoryStore(tmp_path / "platform.db")
    routes = AgentRouteStore(store.database)
    routes.revision("100", "group", "123", ("future_test", "edictum", "agent", "group", 0, "platform", False), enabled=True)
    routes.revision("100", "group", "123", ("future_test", "edictum", "other", "group", 0, "platform", False), enabled=True)
    with closing(store._connect()) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS context_sessions (context_key TEXT, session_id TEXT, platform TEXT)")
        for generation in (1, 2):
            route = ConversationRoute("7", "bot", "agent", "edictum", "group", "100", "123",
                                      conversation_kind="group", conversation_id="123", binding_generation=generation)
            connection.execute("INSERT INTO context_sessions (context_key, session_id, platform) VALUES (?, ?, ?)",
                               (route.key, f"session-{generation}", "bot"))
    summary = store.scoped_session_summary("bot", "100", "123")
    assert summary["known_scoped_count"] == 2
    assert summary["current_route_count"] == 1
    assert summary["current_session_ids"] == ["session-2"]

class _EchoSession(Session):
    """测试用同步会话"""

    def run(self, message: str) -> str:
        return message

class _DummyAdapter(PlatformAdapter):
    adapter_type = "dummy"

    async def run(self) -> None:
        return None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(name=self.config.id, id=self.config.id)

class _CleanupAdapter(_DummyAdapter):
    """记录 terminate 是否被调用的适配器"""

    terminated = False

    async def terminate(self) -> None:
        """记录客户端清理并执行基类终止流程"""
        self.terminated = True
        await super().terminate()

def _session_class_mgr(tmp_path: Path, params: dict[str, Any] | None = None) -> SessionClassConfigManager:
    mgr = SessionClassConfigManager(storage_path=tmp_path / "session_classes.json")
    mgr.register("dummy", _EchoSession)
    if params:
        mgr.set_config("dummy", params)
    return mgr

def _session_manager(tmp_path: Path, scm: SessionClassConfigManager) -> SessionManager:
    sm = SessionManager(default_session_type="dummy", db_path=tmp_path / "sessions.db")
    sm.register_session_type("dummy", _EchoSession)
    sm.class_cfg_mgr = scm
    return sm

def _message_event(adapter_id: str = "misskey1") -> MessageEvent:
    message = PlatformMessage()
    message.type = PlatformMessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.session_id = "chat%user-1"
    message.message_id = "msg-1"
    message.sender = MessageMember(user_id="user-1", nickname="User")
    message.sender.user_id = "user-1"
    message.sender.nickname = "User"
    message.message = []
    message.message_str = "hello"
    message.raw_message = {}
    event = MessageEvent(
        message_str="hello",
        platform_message=message,
        platform_meta=PlatformMetadata(name=adapter_id, id=adapter_id),
        session_id=message.session_id,
        adapter=None,
        session_type="dummy",
    )
    return event

def test_same_adapter_type_can_register_multiple_instances():
    """同一 adapter type 可以注册多个不同实例 ID"""
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    mgr = PlatformAdapterManager(registry=registry)

    first = mgr.add_adapter(PlatformConfig(id="dummy1", type="dummy"))
    second = mgr.add_adapter(PlatformConfig(id="dummy2", type="dummy"))

    assert first is not None
    assert second is not None
    assert mgr.list_adapters() == ["dummy1", "dummy2"]

@pytest.mark.asyncio
async def test_event_dispatcher_processes_platforms_concurrently():
    """一个平台的慢事件不会阻塞另一个平台, 同平台仍由单工作器保序"""
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    manager = PlatformAdapterManager(registry=registry)
    first = manager.add_adapter(PlatformConfig(id="first", type="dummy"))
    second = manager.add_adapter(PlatformConfig(id="second", type="dummy"))
    assert first is not None and second is not None

    first_started = asyncio.Event()
    second_done = asyncio.Event()
    release_first = asyncio.Event()

    class _Scheduler:
        async def execute(self, event: MessageEvent) -> None:
            """按平台标识控制测试事件时序"""
            if event.platform_meta.id == "first":
                first_started.set()
                await release_first.wait()
            else:
                second_done.set()

    dispatcher = EventDispatcher(manager, cast(PipelineScheduler, _Scheduler()))
    task = asyncio.create_task(dispatcher.dispatch_loop())
    await first._event_queue.put(_message_event("first"))
    await first_started.wait()
    await second._event_queue.put(_message_event("second"))
    await asyncio.wait_for(second_done.wait(), timeout=1)
    release_first.set()
    await first._event_queue.join()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

@pytest.mark.asyncio
async def test_manager_stop_all_terminates_adapter_resources():
    """管理器停止平台时必须执行完整的客户端清理"""
    registry = PlatformAdapterRegistry()
    registry.register("cleanup", _CleanupAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="cleanup1", type="cleanup"))
    assert isinstance(adapter, _CleanupAdapter)

    await manager.start_all()
    await manager.stop_all()

    assert adapter.terminated is True
    assert adapter.started is False

def test_user_manager_routes_same_user_to_different_adapter_sessions(tmp_path: Path):
    """同一用户在不同 adapter_id 下应拥有不同上下文"""
    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")

    first = um.resolve_session("user-1", "misskey1", "dummy", scm)
    second = um.resolve_session("user-1", "misskey2", "dummy", scm)

    assert first.startswith("dummy:misskey1:user-1:")
    assert second.startswith("dummy:misskey2:user-1:")
    assert first != second

@pytest.mark.parametrize("configured", ["misskey2", "missing"])
def test_pipeline_binds_source_adapter_over_class_config(tmp_path: Path, configured: str):
    """类级 adapter_id 不覆盖事件来源适配器, 自动创建会话时写入来源实例参数"""
    scm = _session_class_mgr(tmp_path, {"adapter_id": configured})
    sm = _session_manager(tmp_path, scm)
    scheduler = PipelineScheduler(sm)

    platform_id, extra = scheduler._resolve_route_adapter(_message_event("misskey1"))

    assert platform_id == "misskey1"
    assert extra == {"adapter_id": "misskey1"}

@pytest.mark.asyncio
async def test_backend_awaits_platform_start_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台初始化应在当前事件循环中等待 start_all 且只调用一次"""
    calls = 0

    async def start_all(_manager: PlatformAdapterManager) -> None:
        """
        参数:
        - _manager: 平台适配器管理器
        """
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)

    monkeypatch.setattr(PlatformAdapterManager, "start_all", start_all)
    backend = BackendManager(BackendConfig(platforms=[]))

    try:
        await backend._init_platforms()

        assert calls == 1
    finally:
        # _init_platforms 登记进程级通知 hub, 必须 stop 清理避免泄漏到其他用例
        await backend.stop()

@pytest.mark.asyncio
async def test_backend_propagates_platform_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台启动异常应直接传递给后端启动流程"""

    async def start_all(_manager: PlatformAdapterManager) -> None:
        """
        参数:
        - _manager: 平台适配器管理器
        """
        raise RuntimeError("platform start failed")

    monkeypatch.setattr(PlatformAdapterManager, "start_all", start_all)
    backend = BackendManager(BackendConfig(platforms=[]))

    try:
        with pytest.raises(RuntimeError, match="platform start failed"):
            await backend._init_platforms()
    finally:
        # 启动失败前 hub 已登记, 由 stop 负责清除
        await backend.stop()

def test_backend_resolves_explicit_same_name_and_default_session_types(tmp_path: Path):
    """平台会话类按显式配置, 同名兼容和全局默认的顺序解析; CLI 可从配置读出 adapter 候选"""
    backend = BackendManager(BackendConfig(default_session_type="fallback"))
    manager = SessionClassConfigManager(storage_path=tmp_path / "session_classes.json")
    manager.register("misskey", _EchoSession)
    backend._session_cls_cfg = manager

    assert backend._resolve_platform_session_type("misskey", "assistant") == "assistant"
    assert backend._resolve_platform_session_type("misskey", "") == "misskey"
    assert backend._resolve_platform_session_type("onebot", "") == "fallback"

    configured = BackendConfig(platforms=[
        {"id": "misskey1", "type": "misskey", "settings": {}},
        {"id": "misskey2", "type": "misskey", "settings": {}},
    ])
    assert _configured_adapter_ids(configured) == {"misskey1", "misskey2"}

def test_backend_isolates_same_session_id_between_platform_databases(tmp_path: Path):
    """两个平台可拥有同名会话, 其数据库和私有目录必须完全隔离"""
    backend = BackendManager(
        BackendConfig(
            data_root=str(tmp_path / "data"),
            model_config_path=str(tmp_path / "models.json"),
            session_class_config_path=str(tmp_path / "session-classes.json"),
            edictum_config_path=str(tmp_path / "edictum.json"),
        )
    )
    backend._init_model_config()
    backend._init_session_class_config()
    backend._init_edictum_config()
    first_manager, _ = backend._ensure_platform_runtime("onebot-main")
    second_manager, _ = backend._ensure_platform_runtime("misskey-main")

    first_manager.register_session(_EchoSession, "echo", session_id="same-id")
    second_manager.register_session(_EchoSession, "echo", session_id="same-id")

    assert first_manager.store.db_path != second_manager.store.db_path
    assert first_manager.store.get("same-id") is not None
    assert second_manager.store.get("same-id") is not None
    first_root = backend._storage.session_root("onebot-main", "same-id")
    second_root = backend._storage.session_root("misskey-main", "same-id")
    assert not first_root.exists() and not second_root.exists()
    assert first_root != second_root
    (first_root / "uploads").mkdir(parents=True)
    (first_root / "uploads" / "document.txt").write_text("私有文档", encoding="utf-8")
    assert not second_root.exists()

@pytest.mark.asyncio
async def test_dispatcher_same_platform_parallel_groups_and_order():
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="bot", type="dummy", settings={"event_concurrency": 2}))
    assert adapter is not None
    entered = asyncio.Event()
    other_done = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Scheduler:
        async def execute(self, event):
            calls.append(event.message_str)
            if event.message_str == "a1":
                entered.set()
                await release.wait()
            if event.message_str == "b1":
                other_done.set()

    dispatcher = EventDispatcher(manager, cast(PipelineScheduler, Scheduler()))
    task = asyncio.create_task(dispatcher.dispatch_loop())
    try:
        for group, text in [("a", "a1"), ("a", "a2"), ("b", "b1")]:
            event = _message_event("bot")
            event.session_id = group
            event.message_str = text
            adapter.commit_event(event)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(other_done.wait(), 1)
        assert calls == ["a1", "b1"]
        release.set()
        await asyncio.wait_for(adapter._event_queue.join(), 1)
        assert calls == ["a1", "b1", "a2"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_dispatcher_expiry_overload_and_cleanup(tmp_path):
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="bot", type="dummy", settings={"event_queue_capacity": 1, "event_queue_ttl": 1}))
    assert adapter is not None
    expired = _message_event("bot")
    adapter.commit_event(expired)
    expired.queued_at -= 10
    dropped = _message_event("bot")
    asset = tmp_path / "pending.txt"
    asset.write_text("temporary", encoding="utf-8")
    dropped.track_temporary_local_file(str(asset))
    adapter.commit_event(dropped)
    assert adapter.dropped_events == 1
    assert not asset.exists()
    from unittest.mock import AsyncMock
    scheduler = AsyncMock()
    dispatcher = EventDispatcher(manager, scheduler)
    task = asyncio.create_task(dispatcher.dispatch_loop())
    try:
        await asyncio.wait_for(adapter._event_queue.join(), 1)
        scheduler.execute.assert_not_awaited()
        assert adapter.expired_events == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(adapter._event_queue.join(), 1)

@pytest.mark.asyncio
async def test_dispatcher_cancel_cleans_running_and_pending(tmp_path):
    registry = PlatformAdapterRegistry()
    registry.register("dummy", _DummyAdapter)
    manager = PlatformAdapterManager(registry=registry)
    adapter = manager.add_adapter(PlatformConfig(id="bot", type="dummy", settings={"event_concurrency": 1}))
    assert adapter is not None
    started = asyncio.Event()

    class Scheduler:
        async def execute(self, event):
            started.set()
            await asyncio.Future()

    assets = []
    for index in range(4):
        event = _message_event("bot")
        asset = tmp_path / f"{index}.txt"
        asset.write_text("temporary", encoding="utf-8")
        event.track_temporary_local_file(str(asset))
        assets.append(asset)
        adapter.commit_event(event)
    task = asyncio.create_task(EventDispatcher(manager, cast(PipelineScheduler, Scheduler())).dispatch_loop())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(adapter._event_queue.join(), 1)
    assert all(not asset.exists() for asset in assets)

@pytest.mark.parametrize("scope", ["group_member", "group"])
def test_scoped_routes_isolate_groups_accounts_and_preserve_legacy(tmp_path, scope):
    from satrap.core.conversation import ConversationRoute

    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")
    legacy = um.resolve_session("123", "bot", "dummy", scm)

    def resolve(user="123", group="456", account="10000", selected=scope):
        route = ConversationRoute(user, "bot", "dummy", scope=selected, self_id=account, group_id=group)
        session_id = um.resolve_session(user, "bot", "dummy", scm, route=route)
        return route, session_id

    route, first = resolve()
    assert first != legacy
    assert resolve()[1] == first
    assert resolve(group="789")[1] != first
    assert resolve(account="20000")[1] != first
    other = resolve(user="321")[1]
    assert (other == first) == (scope == "group")
    assert um.resolve_session("123", "bot", "dummy", scm) == legacy
    assert resolve(selected="group" if scope == "group_member" else "group_member")[1] != first
    restored = UserManager(sm, db_path=tmp_path / "users.db")
    assert restored.resolve_session("123", "bot", "dummy", scm, route=route) == first
    record = restored.store.get_context_session("123", "bot", "dummy", context_key=route.key)
    assert record is not None and record.user_id == ("" if scope == "group" else "123")
    if scope == "group":
        assert first not in restored.store.list_user_sessions("123")
        assert first not in restored.store.list_user_sessions("321")

@pytest.mark.asyncio
async def test_scheduler_routes_group_members_without_changing_actor(tmp_path):
    from satrap.core.platform.onebot.adapter import OneBotAdapter
    from satrap.core.type import UserCall

    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")
    calls: list[UserCall] = []

    class RecordingManager:
        class_cfg_mgr = scm
        # 事件绑定 dummy 会话类: 直接用真实注册表判定, 保持与生产同一状态源
        provider_registry = sm.provider_registry

        async def handle_call_async(self, call):
            calls.append(call)
            return ""

    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", session_type="dummy", settings={"context_scope": "group_member"}))
    scheduler = PipelineScheduler(cast(SessionManager, RecordingManager()), user_manager=um)
    for group in [456, 789, 456]:
        await adapter._handle_group_message({
            "self_id": 10000, "user_id": 123, "group_id": group,
            "message_type": "group", "message": [{"type": "at", "data": {"qq": "10000"}}],
        })
        await scheduler.execute(adapter._event_queue.get_nowait())
    assert len(calls) == 3
    assert calls[0].session_id == calls[2].session_id != calls[1].session_id
    assert all(call.route is not None and call.route.user_id == "123" for call in calls)

@pytest.mark.asyncio
async def test_scoped_command_switch_updates_original_route(tmp_path, monkeypatch):
    from satrap.core.conversation import ConversationRoute
    from satrap.core.type import CommandAction, UserCall

    scm = _session_class_mgr(tmp_path)
    sm = _session_manager(tmp_path, scm)
    um = UserManager(sm, db_path=tmp_path / "users.db")
    sm._user_mgr = um
    route = ConversationRoute("123", "bot", "dummy", scope="group_member", self_id="10000", group_id="456")
    initial = um.resolve_session("123", "bot", "dummy", scm, route=route)
    target = um.resolve_session("123", "bot", "dummy", scm)
    monkeypatch.setattr(sm, "_invoke_sync_entry", lambda *args: CommandAction("switch_session", target, "switched"))
    result = await sm.handle_call_async(UserCall(session_id=initial, message="switch", route=route))
    assert result == "switched"
    assert um.resolve_session("123", "bot", "dummy", scm, route=route) == target
    untouched = ConversationRoute("123", "bot", "dummy", scope="group_member", self_id="10000", group_id="789")
    assert um.resolve_session("123", "bot", "dummy", scm, route=untouched) != target
