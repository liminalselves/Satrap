from copy import deepcopy
from types import SimpleNamespace
import asyncio
import json
import urllib.error

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.administrator_groups import administrator_revision
from satrap.core.config.administrator_settings import (
    administrator_settings_snapshot, prepare_administrator_groups, save_administrator_groups,
)
from satrap.core.config.document import ConfigRevisionConflict, config_document_revision, load_config_document, save_config_document
from satrap.core.platform import PlatformAdapter


def draft(user="123"):
    return [{"id": "admins", "name": "管理员", "enabled": True,
             "members": [{"platform_id": "ob", "user_id": user}],
             "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": []}}]


def document():
    return {"platforms": [{"id": "ob", "type": "onebot", "instance_id": "a" * 32}], "extra": "保留原字段"}


def test_adapter_identity_rules_are_generic_and_platform_specific():
    assert PlatformAdapter.normalize_user_identifier(" user@example.test ") == "user@example.test"
    assert prepare_administrator_groups(document(), draft("00123"))[0]["members"][0]["user_id"] == "123"
    with pytest.raises(ValueError, match="QQ"):
        prepare_administrator_groups(document(), draft("nickname"))


def test_section_save_preserves_other_configuration_and_rejects_conflicts(tmp_path):
    path = tmp_path / "config.json"
    old = save_config_document(path, document())
    result = save_administrator_groups(path, draft(), config_document_revision(old))
    assert result["extra"] == old["extra"] and result["platforms"] == old["platforms"]
    assert result["administrator_groups"][0]["members"][0]["platform_instance_id"] == "a" * 32
    with pytest.raises(ConfigRevisionConflict):
        save_administrator_groups(path, [], config_document_revision(old))
    assert load_config_document(path) == result


def test_stale_platform_binding_needs_explicit_rebind_not_forged_instance():
    current = document()
    current["administrator_groups"] = prepare_administrator_groups(current, draft())
    current["platforms"][0]["instance_id"] = "b" * 32
    forged = deepcopy(current["administrator_groups"])
    forged[0]["members"][0]["platform_instance_id"] = "b" * 32
    kept = prepare_administrator_groups(current, forged)
    assert kept[0]["members"][0]["platform_instance_id"] == "a" * 32
    snapshot = administrator_settings_snapshot(current)
    assert len(snapshot["invalid_members"]) == 1
    rebound = prepare_administrator_groups(current, forged, [{"group_id": "admins", "platform_id": "ob", "user_id": "123"}])
    assert rebound[0]["members"][0]["platform_instance_id"] == "b" * 32


@pytest.mark.asyncio
async def test_runtime_apply_reads_saved_section_and_does_not_accept_arbitrary_body(tmp_path):
    path = tmp_path / "config.json"
    saved = save_config_document(path, document())
    config = BackendConfig.from_dict(saved)
    config.source_path = str(path)
    backend = BackendManager(config)
    server = BackendHTTPServer(backend)
    before = backend.administrator_runtime_status()
    saved = save_administrator_groups(path, draft(), config_document_revision(saved))
    revision = administrator_revision(saved["administrator_groups"])
    assert backend.administrator_runtime_status()["section_revision"] == before["section_revision"]
    status, payload = await server._route("POST", "/api/administrators/apply", json.dumps({"section_revision": revision}).encode())
    assert status == 200 and payload["section_revision"] == revision
    assert payload["runtime_id"] == before["runtime_id"] and payload["pid"] == before["pid"]
    status, _ = await server._route("POST", "/api/administrators/apply", json.dumps({"section_revision": revision, "groups": []}).encode())
    assert status == 400
    save_administrator_groups(path, [], config_document_revision(saved))
    status, _ = await server._route("POST", "/api/administrators/apply", json.dumps({"section_revision": revision}).encode())
    assert status == 409
    assert backend.administrator_runtime_status()["section_revision"] == revision


def test_runtime_apply_rejects_unapplied_platform_replacement(tmp_path):
    path = tmp_path / "config.json"
    saved = save_config_document(path, document())
    config = BackendConfig.from_dict(saved)
    config.source_path = str(path)
    backend = BackendManager(config)
    changed = deepcopy(saved)
    changed["platforms"][0]["instance_id"] = "b" * 32
    changed = save_config_document(path, changed)
    with pytest.raises(ConfigRevisionConflict, match="平台实例"):
        backend.apply_saved_administrator_groups(administrator_revision([]))


@pytest.mark.asyncio
async def test_stale_general_configuration_cannot_overwrite_new_admin_groups(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control
    path = tmp_path / "config.json"
    initial = save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    saved = save_administrator_groups(path, draft(), config_document_revision(initial))
    request = context(control, "PUT", "/config", initial)
    request = control._RouteContext(request.method, request.path, f"/config?expected_revision={config_document_revision(initial)}", request.reader, request.raw_request)
    result = await control._route_config_document(request)
    assert result[0] == 409 and load_config_document(path) == saved


def test_invalid_plugin_declaration_is_visible_but_other_catalog_entries_remain(tmp_path):
    from satrap.edictum.plugin_catalog import PluginCatalog
    for name, contents in [("valid", "name: valid\n"), ("invalid", "name: invalid\npermission_schema_version: 1\nmanagement_permissions: {}\n")]:
        folder = tmp_path / "plugins" / name
        folder.mkdir(parents=True)
        (folder / "meta.yaml").write_text(contents, encoding="utf-8")
    snapshot = administrator_settings_snapshot(document(), PluginCatalog(tmp_path / "plugins", tmp_path / "user"))
    assert [plugin["name"] for plugin in snapshot["plugins"]] == ["valid"]
    assert snapshot["plugin_errors"][0]["name"] == "invalid"


def context(control, method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    headers = f"{method} {path} HTTP/1.1\r\nContent-Length: {len(data)}\r\n\r\n".encode()
    return control._RouteContext(method, path, path, reader, headers)


@pytest.mark.asyncio
async def test_control_save_preview_retry_and_conflict_without_restart(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control
    path = tmp_path / "config.json"
    save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    calls = []
    def runtime(revision, *, apply=False):
        calls.append((revision, apply))
        return {"status": "applied", "section_revision": revision}
    monkeypatch.setattr(control, "_administrator_runtime_request", runtime)
    root = "/config/administrator-groups"
    response = await control._route_administrator_settings(context(control, "GET", root))
    assert response[0] == 200 and response[1]["groups"] == []
    original = load_config_document(path)
    preview = await control._route_administrator_settings(context(control, "POST", root + "/preview", {"groups": draft()}))
    assert preview[0] == 200 and preview[1]["members"][0]["plugins"][0]["name"] == "group_admin"
    assert load_config_document(path) == original
    payload = {"groups": draft(), "expected_revision": response[1]["revision"]}
    saved = await control._route_administrator_settings(context(control, "PUT", root, payload))
    assert saved[0] == 200 and saved[1]["runtime"]["status"] == "applied" and calls[-1][1]
    assert load_config_document(path)["extra"] == "保留原字段"
    conflict = await control._route_administrator_settings(context(control, "PUT", root, {**payload, "groups": []}))
    assert conflict[0] == 409 and load_config_document(path)["administrator_groups"]
    retry = await control._route_administrator_settings(context(control, "POST", root + "/apply", {"section_revision": saved[1]["section_revision"]}))
    assert retry[0] == 200 and retry[1]["runtime"]["status"] == "applied"


@pytest.mark.parametrize("result, expected", [("same", "applied"), ("wrong_revision", "unconfirmed"), ("wrong_path", "unconfirmed"), ("refused", "next_start"), ("timeout", "unconfirmed")])
def test_control_never_confirms_save_as_runtime_application(tmp_path, monkeypatch, result, expected):
    from satrap.core.backend import control_server as control
    path = tmp_path / "config.json"
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    monkeypatch.setattr(control, "_configured_backend_address", lambda: ("127.0.0.1", 19870))
    monkeypatch.setattr(control, "_authenticated_request", lambda url, method: SimpleNamespace(data=None, add_header=lambda *args: None))
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self, limit):
            return json.dumps({"ok": True, "runtime_id": "running", "section_revision": "different" if result == "wrong_revision" else "saved",
                               "config_path": str(path.with_name("other.json") if result == "wrong_path" else path)}).encode()
    def urlopen(*args, **kwargs):
        if result == "refused":
            raise urllib.error.URLError(ConnectionRefusedError())
        if result == "timeout":
            raise TimeoutError()
        return Response()
    monkeypatch.setattr(control.urllib.request, "urlopen", urlopen)
    assert control._administrator_runtime_request("saved", apply=True)["status"] == expected


@pytest.mark.asyncio
async def test_control_fault_is_logged_and_later_request_still_works(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control
    path = tmp_path / "config.json"
    save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    logged = []
    monkeypatch.setattr(control.logger, "error", logged.append)
    def fault(*args, **kwargs):
        raise RuntimeError("runtime fault injection")
    monkeypatch.setattr(control, "_administrator_runtime_request", fault)
    root = "/config/administrator-groups"
    failed = await control._route_administrator_settings(context(control, "GET", root))
    assert failed[0] == 500 and "runtime fault injection" in logged[-1]
    monkeypatch.setattr(control, "_administrator_runtime_request", lambda *args, **kwargs: {"status": "next_start"})
    recovered = await control._route_administrator_settings(context(control, "GET", root))
    assert recovered[0] == 200
