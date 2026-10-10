"""冷适配器声明与跨入口 Agent 绑定引用保护"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from satrap.core.backend import control_server as control
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.agent_references import list_agent_references
from satrap.core.config.agent_routing import AgentRouteStore
from satrap.core.config.document import load_config_document, save_config_document
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.group_store import GroupConfigStore
from satrap.core.config.group_session import group_binding_chain
from satrap.core.config.edictum_service import EdictumConfigService
from satrap.core.conversation import ConversationRoute
from satrap.core.framework.BackGroundManager import ConfigInUseError, ConfigReferenceScanError
from satrap.core.framework.SessionClassManager import SessionClassConfigManager
from satrap.core.platform import PlatformAdapter, registry
from satrap.core.platform import catalog
from satrap.core.storage import StorageLayout


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
    scan = tmp_path / "sessions"
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
    service.create({"name": "bound", **({"class_path": "sessions.example.Session"} if provider == "session_class" else {"edictum_type": "simple"})})
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
