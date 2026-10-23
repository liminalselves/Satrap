"""业务链路场景与集中边界契约"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
import asyncio
import json
import urllib.error
import pytest
from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.backend.http_api import BackendHTTPServer
from satrap.core.config.administrator_groups import administrator_revision
from satrap.core.config.administrator_settings import administrator_settings_snapshot, prepare_administrator_groups
from satrap.core.config.administrator_settings import preview_administrator_groups, save_administrator_groups
from satrap.core.config.document import ConfigRevisionConflict, config_document_revision, load_config_document
from satrap.core.config.document import save_config_document
from satrap.core.platform import PlatformAdapter
from support.administrator_http import context, document
from pathlib import Path
from typing import TypedDict
from satrap.core.config.document import config_exists, create_default_config, configured_platform_types
from satrap.core.config.document import delete_platform, find_config_path, merge_masked_secrets, parse_platforms_text
from satrap.core.config.document import parse_raw_config, redact_config_document, upsert_platform, update_common_fields
from satrap.cli.client import DaemonClient
from satrap.core.config.loader import ConfigLoader
from unittest.mock import AsyncMock

def draft(user="123"):
    return [{"id": "admins", "name": "管理员", "enabled": True,
             "members": [{"platform_id": "ob", "user_id": user}],
             "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": []}}]

def legacy_group():
    return {"id": "legacy", "name": "L", "enabled": True, "protect": True,
            "members": [{"platform_id": "ob", "user_id": "123", "platform_instance_id": "a" * 32}],
            "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": ["friend_manager"]}}

def pending_document():
    # 待迁移形态: 该成员已存在停用例外, 启用状态冲突使旧排除保留在组内
    return {"platforms": [{"id": "ob", "type": "onebot", "instance_id": "a" * 32}],
            "administrator_groups": [legacy_group()],
            "administrator_overrides": [{"id": "e1", "enabled": False, "protect": True, "platform_id": "ob",
                "platform_instance_id": "a" * 32, "user_id": "123", "allow": [], "deny": ["group_chat"]}]}

def test_adapter_identity_rules_are_generic_and_platform_specific():
    assert PlatformAdapter.normalize_user_identifier(" user@example.test ") == "user@example.test"
    assert prepare_administrator_groups(document(), draft("00123"))[0]["members"][0]["user_id"] == "123"
    with pytest.raises(ValueError, match="QQ"):
        prepare_administrator_groups(document(), draft("nickname"))

def test_section_save_preserves_other_configuration_and_rejects_conflicts(tmp_path):
    path = tmp_path / "config.json"
    old = save_config_document(path, document())
    result = save_administrator_groups(path, draft(), overrides=[], expected_revision=config_document_revision(old))
    assert result["extra"] == old["extra"] and result["platforms"] == old["platforms"]
    assert result["administrator_groups"][0]["members"][0]["platform_instance_id"] == "a" * 32
    with pytest.raises(ConfigRevisionConflict):
        save_administrator_groups(path, [], overrides=[], expected_revision=config_document_revision(old))
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
    saved = save_administrator_groups(path, draft(), overrides=[], expected_revision=config_document_revision(saved))
    revision = administrator_revision(saved["administrator_groups"], saved.get("administrator_overrides", []))
    assert backend.administrator_runtime_status()["section_revision"] == before["section_revision"]
    status, payload = await server._route("POST", "/api/administrators/apply", json.dumps({"section_revision": revision}).encode())
    assert status == 200 and payload["section_revision"] == revision
    assert payload["runtime_id"] == before["runtime_id"] and payload["pid"] == before["pid"]
    status, _ = await server._route("POST", "/api/administrators/apply", json.dumps({"section_revision": revision, "groups": []}).encode())
    assert status == 400
    save_administrator_groups(path, [], overrides=[], expected_revision=config_document_revision(saved))
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
        backend.apply_saved_administrator_groups(administrator_revision([], []))

@pytest.mark.asyncio
async def test_stale_general_configuration_cannot_overwrite_new_admin_groups(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control
    path = tmp_path / "config.json"
    initial = save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    saved = save_administrator_groups(path, draft(), overrides=[], expected_revision=config_document_revision(initial))
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

def test_snapshot_marks_plugin_enabled_only_when_enabled_set_given(tmp_path):
    from satrap.edictum.plugin_catalog import PluginCatalog
    for name in ["alpha", "beta"]:
        folder = tmp_path / "plugins" / name
        folder.mkdir(parents=True)
        (folder / "meta.yaml").write_text(f"name: {name}\n", encoding="utf-8")
    catalog = PluginCatalog(tmp_path / "plugins", tmp_path / "user")
    plain = administrator_settings_snapshot(document(), catalog)
    assert all("enabled" not in plugin for plugin in plain["plugins"])   # 未提供集合时不标注, 兼容其他调用方
    marked = {plugin["name"]: plugin["enabled"] for plugin in administrator_settings_snapshot(document(), catalog, enabled_plugins={"alpha"})["plugins"]}
    assert marked == {"alpha": True, "beta": False}
    assert all("loaded_platforms" not in plugin for plugin in plain["plugins"])   # 未提供映射时不标注
    loaded = {plugin["name"]: plugin["loaded_platforms"]
              for plugin in administrator_settings_snapshot(document(), catalog, platform_plugins={"qq": {"alpha"}, "tg": {"alpha", "beta"}})["plugins"]}
    assert loaded == {"alpha": ["qq", "tg"], "beta": ["tg"]}

def test_migration_notice_and_invalid_override_reporting(tmp_path):
    current = document()
    current["administrator_groups"] = [{"id": "admins", "name": "管理员", "enabled": True,
        "members": [{"platform_id": "ob", "user_id": "123", "platform_instance_id": "a" * 32}],
        "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": ["friend_manager"]}}]
    current["administrator_overrides"] = [{"id": "gone", "enabled": True, "protect": False, "platform_id": "ob",
        "platform_instance_id": "b" * 32, "user_id": "999", "allow": [], "deny": ["group_admin"]}]
    snapshot = administrator_settings_snapshot(current)
    assert snapshot["migrated_from_legacy"] is True
    assert snapshot["groups"][0]["plugin_scope"]["excluded"] == []
    assert [item["deny"] for item in snapshot["overrides"]] == [["group_admin"], ["friend_manager"]]
    assert [item["override_id"] for item in snapshot["invalid_overrides"]] == ["gone"]
    current["administrator_groups"][0]["plugin_scope"]["excluded"] = []
    assert administrator_settings_snapshot(current)["migrated_from_legacy"] is False

def test_preview_lists_override_only_identity_with_sources(tmp_path):
    from satrap.core.config.administrator_settings import preview_administrator_groups

    config = document()
    draft_overrides = [{"id": "solo", "enabled": True, "protect": False, "platform_id": "ob", "user_id": "999",
                        "allow": ["group_admin"], "deny": []}]
    preview = preview_administrator_groups(config, [], overrides=draft_overrides)
    assert [item["override_id"] for item in preview["members"]] == ["solo"]
    assert preview["members"][0]["plugins"][0]["name"] == "group_admin"
    assert preview["members"][0]["plugins"][0]["allowed"] is True
    assert preview["members"][0]["plugins"][0]["sources"]["allow"] == ["override:solo"]
    assert preview["section_revision"] == administrator_revision(preview["groups"], preview["overrides"])

def test_preview_reports_denied_plugin_with_both_source_sides():
    from satrap.core.config.administrator_settings import preview_administrator_groups

    config = document()
    groups = [{"id": "admins", "name": "管理员", "enabled": True, "protect": True,
               "members": [{"platform_id": "ob", "user_id": "123", "platform_instance_id": "a" * 32}],
               "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": []}}]
    overrides = [{"id": "deny-all", "enabled": True, "protect": False, "platform_id": "ob",
                  "platform_instance_id": "a" * 32, "user_id": "123", "allow": [], "deny": ["group_admin"]}]
    preview = preview_administrator_groups(config, groups, overrides=overrides)
    plugin = preview["members"][0]["plugins"][0]
    assert plugin["name"] == "group_admin" and plugin["allowed"] is False   # 被否决插件不再静默消失
    assert plugin["sources"]["allow"] == ["group:admins"] and plugin["sources"]["deny"] == ["override:deny-all"]

def test_preview_omits_plugins_without_any_source():
    from satrap.core.config.administrator_settings import preview_administrator_groups

    preview = preview_administrator_groups(document(), [{
        "id": "admins", "name": "管理员", "enabled": True, "protect": True,
        "members": [{"platform_id": "ob", "user_id": "123", "platform_instance_id": "a" * 32}],
        "plugin_scope": {"mode": "selected", "included": ["unrelated_plugin"], "excluded": []}}], overrides=[])
    assert [item["name"] for item in preview["members"][0]["plugins"]] == []   # 无来源不虚报无效项
    assert preview["members"][0]["plugins"] == []

def test_snapshot_reports_migration_pending_on_enabled_state_conflict():
    current = document()
    current["platforms"] = [{"id": "ob", "type": "onebot", "instance_id": "a" * 32}]
    current["administrator_groups"] = [{"id": "legacy", "name": "L", "enabled": True, "protect": True,
        "members": [{"platform_id": "ob", "user_id": "123", "platform_instance_id": "a" * 32}],
        "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": ["friend_manager"]}}]
    current["administrator_overrides"] = [{"id": "e1", "enabled": False, "protect": True, "platform_id": "ob",
        "platform_instance_id": "a" * 32, "user_id": "123", "allow": [], "deny": ["group_chat"]}]
    snapshot = administrator_settings_snapshot(current)
    assert snapshot["migration_pending"] == ["legacy"]
    assert snapshot["groups"][0]["plugin_scope"]["excluded"] == ["friend_manager"]   # 未迁移, 原样保留
    assert snapshot["migrated_from_legacy"] is False
    current["administrator_overrides"][0]["enabled"] = True
    recovered = administrator_settings_snapshot(current)
    assert recovered["migration_pending"] == []      # 启用状态对齐后即可迁移
    assert recovered["migrated_from_legacy"] is True

@pytest.mark.asyncio
async def test_rule6_section_endpoint_round_trips_converges_and_rejects_added_exclusion(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control

    path = tmp_path / "config.json"
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    save_config_document(path, pending_document())
    monkeypatch.setattr(control, "_administrator_runtime_request", lambda revision, *, apply=False: {"status": "applied", "section_revision": revision})
    root = "/config/administrator-groups"
    pending = load_config_document(path)
    overrides = pending["administrator_overrides"]
    snapshot = administrator_settings_snapshot(pending)
    assert snapshot["migration_pending"] == ["legacy"]
    # (1) 待迁移组带着旧排除原样往返, 保存 200 且语义不变
    payload = {"groups": [legacy_group()], "overrides": overrides, "expected_revision": snapshot["revision"]}
    saved = await control._route_administrator_settings(context(control, "PUT", root, payload))
    assert saved is not None and saved[0] == 200, saved
    assert load_config_document(path)["administrator_groups"][0]["plugin_scope"]["excluded"] == ["friend_manager"]
    # (2) 新增排除项 → 400 且点名新增项
    added_group = legacy_group()
    added_group["plugin_scope"]["excluded"] = ["friend_manager", "other"]
    added = await control._route_administrator_settings(context(control, "PUT", root, {"groups": [added_group], "overrides": overrides, "expected_revision": saved[1]["revision"]}))
    assert added is not None and added[0] == 400 and "不能新增排除插件: ['other']" in str(added[1]["error"])
    # (3) 改名组携带 excluded 视为空 previous → 400
    renamed_group = legacy_group()
    renamed_group["id"] = "renamed"
    renamed = await control._route_administrator_settings(context(control, "PUT", root, {"groups": [renamed_group], "overrides": overrides, "expected_revision": saved[1]["revision"]}))
    assert renamed is not None and renamed[0] == 400 and "不能新增排除插件" in str(renamed[1]["error"])
    # (4) 逐项点不表态收敛旧排除 (子集) → 200
    trimmed_group = legacy_group()
    trimmed_group["plugin_scope"]["excluded"] = []
    trimmed = await control._route_administrator_settings(context(control, "PUT", root, {"groups": [trimmed_group], "overrides": overrides, "expected_revision": saved[1]["revision"]}))
    assert trimmed is not None and trimmed[0] == 200, trimmed
    assert load_config_document(path)["administrator_groups"][0]["plugin_scope"]["excluded"] == []
    # (5) preview 同一判定, 对称生效
    save_config_document(path, pending_document())
    preview_ok = await control._route_administrator_settings(context(control, "POST", root + "/preview", {"groups": [legacy_group()], "overrides": overrides}))
    assert preview_ok is not None and preview_ok[0] == 200, preview_ok
    preview_added = await control._route_administrator_settings(context(control, "POST", root + "/preview", {"groups": [added_group], "overrides": overrides}))
    assert preview_added is not None and preview_added[0] == 400 and "不能新增排除插件: ['other']" in str(preview_added[1]["error"])
    preview_renamed = await control._route_administrator_settings(context(control, "POST", root + "/preview", {"groups": [renamed_group], "overrides": overrides}))
    assert preview_renamed is not None and preview_renamed[0] == 400 and "不能新增排除插件" in str(preview_renamed[1]["error"])

@pytest.mark.asyncio
async def test_general_config_exclusion_scope_round_trip_and_rejection(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control

    path = tmp_path / "config.json"
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    # 待迁移形态: 启用组带旧排除 + 该成员已存在停用例外 → 存盘保留旧排除, 不阻断 round-trip
    save_config_document(path, {
        "platforms": [{"id": "ob", "type": "onebot", "instance_id": "a" * 32}],
        "administrator_groups": [{"id": "legacy", "name": "L", "enabled": True, "protect": True,
            "members": [{"platform_id": "ob", "user_id": "123", "platform_instance_id": "a" * 32}],
            "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": ["friend_manager"]}}],
        "administrator_overrides": [{"id": "e1", "enabled": False, "protect": True, "platform_id": "ob",
            "platform_instance_id": "a" * 32, "user_id": "123", "allow": [], "deny": ["group_chat"]}]})
    current = load_config_document(path)
    assert current["administrator_groups"][0]["plugin_scope"]["excluded"] == ["friend_manager"]
    status, payload = await route_config(control, "PUT", "/config", current, query=f"?expected_revision={config_document_revision(current)}")
    assert status == 200, payload                    # 存量旧排除可原样往返
    assert load_config_document(path)["administrator_groups"][0]["plugin_scope"]["excluded"] == ["friend_manager"]

    # 手工新增排除项 (无 previous) 必须被拒绝
    fresh_path = tmp_path / "fresh.json"
    monkeypatch.setattr(control, "CONFIG_PATH", fresh_path)
    save_config_document(fresh_path, {"platforms": [{"id": "ob", "type": "onebot", "instance_id": "a" * 32}]})
    fresh = load_config_document(fresh_path)
    fresh["administrator_groups"] = [{"id": "fresh", "name": "F", "enabled": True, "protect": True,
        "members": [{"platform_id": "ob", "user_id": "123", "platform_instance_id": "a" * 32}],
        "plugin_scope": {"mode": "selected", "included": ["group_admin"], "excluded": ["friend_manager"]}}]
    revision = config_document_revision(load_config_document(fresh_path))
    status, payload = await route_config(control, "PUT", "/config", fresh, query=f"?expected_revision={revision}")
    assert status == 400 and "不能新增排除插件" in str(payload["error"])

def test_save_and_preview_require_overrides_keyword(tmp_path):
    path = tmp_path / "config.json"
    save_config_document(path, document())
    # 旧式位置调用必须显式失败, 不能把修订串误绑到 overrides 上静默丢弃例外层
    legacy_save = cast(Any, save_administrator_groups)
    legacy_preview = cast(Any, preview_administrator_groups)
    with pytest.raises(TypeError):
        legacy_save(path, draft(), config_document_revision(document()))
    with pytest.raises(TypeError):
        legacy_preview(document(), draft(), config_document_revision(document()))

async def route_config(control, method, path, payload=None, query=""):
    """经控制端配置区段路由请求并确认命中"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    reader = asyncio.StreamReader()
    reader.feed_data(body)
    reader.feed_eof()
    headers = f"{method} {path}{query} HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    result = await control._route_config_document(control._RouteContext(method, path, f"{path}{query}", reader, headers))
    assert result is not None, "配置区段路由未命中"
    return result

@pytest.mark.asyncio
async def test_admin_endpoints_require_explicit_overrides_key(tmp_path, monkeypatch):
    from satrap.core.backend import control_server as control

    path = tmp_path / "config.json"
    save_config_document(path, document())
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    monkeypatch.setattr(control, "_administrator_runtime_request", lambda revision, *, apply=False: {"status": "applied", "section_revision": revision})
    root = "/config/administrator-groups"
    revision = administrator_settings_snapshot(load_config_document(path))["revision"]
    # 缺 overrides 键必须 400, 不能按"清空例外层"处理
    missing_put = await control._route_administrator_settings(context(control, "PUT", root, {"groups": draft(), "expected_revision": revision}))
    assert missing_put is not None and missing_put[0] == 400 and "overrides" in str(missing_put[1]["error"])
    missing_preview = await control._route_administrator_settings(context(control, "POST", root + "/preview", {"groups": draft()}))
    assert missing_preview is not None and missing_preview[0] == 400 and "overrides" in str(missing_preview[1]["error"])
    # 显式携带空列表是合法的显式清空
    explicit = await control._route_administrator_settings(context(control, "PUT", root, {"groups": draft(), "overrides": [], "expected_revision": revision}))
    assert explicit is not None and explicit[0] == 200 and load_config_document(path)["administrator_overrides"] == []

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
    preview = await control._route_administrator_settings(context(control, "POST", root + "/preview", {"groups": draft(), "overrides": []}))
    assert preview[0] == 200 and preview[1]["members"][0]["plugins"][0]["name"] == "group_admin"
    assert load_config_document(path) == original
    payload = {"groups": draft(), "overrides": [], "expected_revision": response[1]["revision"]}
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

class _PlatformSettings(TypedDict):
    """测试配置中的平台凭据字段"""

    api_token: str
    base_url: str

class _PlatformDocument(TypedDict):
    """测试配置中的平台条目"""

    id: str
    settings: _PlatformSettings

class _ConfigDocument(TypedDict):
    """凭据脱敏测试使用的配置文档"""

    max_tokens: int
    platforms: list[_PlatformDocument]

@pytest.mark.asyncio
async def test_reload_api_forwards_saved_revision_and_rejects_invalid_type():
    """重载接口传递保存修订, 非法输入不得触发重载"""
    backend = BackendManager()
    backend.reload_config = AsyncMock(return_value={"ok": True})
    server = BackendHTTPServer(backend)
    assert (await server._route("POST", "/api/config/reload", b'{"expected_config_revision":"saved"}'))[0] == 200
    backend.reload_config.assert_awaited_once_with("saved")
    backend.reload_config.reset_mock()
    assert (await server._route("POST", "/api/config/reload", b'{"expected_config_revision":42}'))[0] == 400
    backend.reload_config.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_platform_revision_conflict_preserves_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str):
    """旧页面的新增, 更新和删除均不能覆盖后来保存的配置"""
    from satrap.core.backend import control_server as control

    path = tmp_path / "config.json"
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    original = {"id": "bot", "type": "onebot", "settings": {"access_token": "test-secret"}}
    save_config_document(path, {"platforms": [original]})
    read_context = control._RouteContext("GET", "/config/platforms", "/config/platforms", asyncio.StreamReader(), b"")
    response = await control._route_config_document(read_context)
    assert response is not None
    status, data = response
    assert status == 200
    revision = data["revision"]
    assert "test-secret" not in json.dumps(data)
    latest = save_config_document(path, {"platforms": [original], "extra": "new-value"})
    route = "/config/platforms" if method == "POST" else "/config/platforms/bot"
    payload = {"id": "new-bot" if method == "POST" else "bot", "type": "onebot", "settings": {}}
    body = json.dumps(payload).encode("utf-8")
    reader = asyncio.StreamReader()
    reader.feed_data(body)
    reader.feed_eof()
    context = control._RouteContext(method, route, f"{route}?expected_revision={revision}", reader,
                                    f"{method} {route} HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    response = await control._route_config_document(context)
    assert response is not None
    assert response[0] == 409
    assert response[1]["code"] == "config_revision_conflict"
    assert load_config_document(path) == latest

def test_daemon_client_shutdown_uses_shutdown_route(monkeypatch: pytest.MonkeyPatch):
    """DaemonClient.shutdown 应请求后端 shutdown 路由"""
    calls: list[Any] = []

    def fake_request(method: str, path: str, body: dict[str, Any] | None = None):
        calls.append((method, path, body))
        return {"ok": True}

    client = DaemonClient()
    monkeypatch.setattr(client, "_request", fake_request)

    assert client.shutdown() == {"ok": True}
    assert calls == [("POST", "/api/shutdown", None)]

@pytest.mark.asyncio
async def test_http_shutdown_route_sets_backend_event():
    """shutdown API 应触发 BackendManager 的关闭事件"""
    backend = BackendManager()
    event = asyncio.Event()
    backend.set_shutdown_event(event)
    server = BackendHTTPServer(backend)

    status, data = await server._route("POST", "/api/shutdown", b"")
    await asyncio.sleep(0)

    assert status == 200
    assert data == {"ok": True}
    assert event.is_set()

@pytest.mark.parametrize("suffix,port,platform_id", [(".yaml", 19870, "misskey1"), (".json", 19871, "misskey1")])
def test_config_editor_roundtrip(tmp_path: Path, suffix: str, port: int, platform_id: str):
    """配置编辑器应能按 YAML/JSON 后缀读写配置"""
    path = tmp_path / f"config{suffix}"
    data: dict[str, Any] = {
        "api": {"host": "127.0.0.1", "port": port},
        "platforms": [{"id": platform_id, "type": "misskey", "settings": {}}],
    }

    config = save_config_document(path, data)
    loaded = load_config_document(path)

    assert isinstance(config, dict)
    assert loaded["api"]["port"] == port
    assert loaded["platforms"][0]["id"] == platform_id

def test_parse_raw_config_rejects_non_object(tmp_path: Path):
    """原始配置根节点必须是对象"""
    path = tmp_path / "config.json"

    try:
        parse_raw_config(path, "[]")
    except ValueError as e:
        assert "根节点" in str(e)
    else:
        raise AssertionError("应拒绝非对象配置")

def test_update_common_fields_parse_platforms_and_configured_types():
    """常用字段保存解析 platforms, 平台类型汇总来自配置和后端运行态"""
    platforms = parse_platforms_text('[{"id": "misskey1", "type": "misskey", "settings": {}}]')
    data = update_common_fields(
        {},
        api_host="127.0.0.1",
        api_port=19870,
        default_session_type="misskey",
        max_sessions=100,
        idle_timeout=60,
        llm_timeout=30.0,
        rate_limit=1.5,
        rate_burst=3,
        platforms=platforms,
    )

    assert data["api"]["port"] == 19870
    assert data["platforms"][0]["id"] == "misskey1"

    config_data: dict[str, Any] = {"platforms": [{"id": "misskey1", "type": "misskey", "settings": {}}]}
    health: dict[str, Any] = {"adapters": {"qq1": {"config_type": "qq"}}}
    assert configured_platform_types(config_data, health) == ["misskey", "qq"]

def test_find_config_path_defaults_and_prefers_root(tmp_path: Path):
    """没有配置文件时默认指向 .satrap/config.yaml, 根目录配置优先"""
    assert find_config_path(tmp_path) == tmp_path / ".satrap" / "config.yaml"
    root_config = tmp_path / "config.yaml"
    nested_config = tmp_path / ".satrap" / "config.yaml"
    nested_config.parent.mkdir()
    root_config.write_text("platforms: []\n", encoding="utf-8")
    nested_config.write_text("api:\n  port: 19999\nplatforms: []\n", encoding="utf-8")

    assert find_config_path(tmp_path) == root_config

def test_default_config_creation_and_autodetect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """无配置时创建默认 .satrap/config.yaml, autodetect 创建默认且保留根目录优先级"""
    assert not config_exists(tmp_path)
    path = tmp_path / ".satrap" / "config.yaml"
    config = create_default_config(path)
    assert path.exists()
    assert config["api"]["host"] == "127.0.0.1"
    assert load_config_document(path)["platforms"] == []

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    loaded = ConfigLoader.autodetect()
    assert (fresh / ".satrap" / "config.yaml").exists()
    assert loaded.api_port == 19870

    rooted = tmp_path / "rooted"
    rooted.mkdir()
    (rooted / "config.yaml").write_text("api:\n  port: 19999\nplatforms: []\n", encoding="utf-8")
    monkeypatch.chdir(rooted)
    rooted_config = ConfigLoader.autodetect()
    assert rooted_config.api_port == 19999
    assert not (rooted / ".satrap" / "config.yaml").exists()

def test_platform_upsert_add_update_delete_and_duplicate_id():
    """平台快捷编辑支持新增/修改/删除, 且不覆盖另一个已有平台 id"""
    platforms: list[dict[str, Any]] = []
    platforms = upsert_platform(
        platforms,
        {
            "id": "misskey_main",
            "type": "misskey",
            "settings": {
                "base_url": "https://misskey.example/",
                "api_token": "token",
                "chat_enabled": True,
                "room_enabled": False,
            },
        },
        original_id=None,
    )
    platforms = upsert_platform(
        platforms,
        {
            "id": "onebot_main",
            "type": "onebot",
            "settings": {
                "host": "127.0.0.1",
                "port": 6700,
                "access_token": "token",
                "secret": "",
                "enable_private": True,
                "enable_group": True,
            },
        },
        original_id=None,
    )
    platforms = upsert_platform(
        platforms,
        {"id": "misskey_prod", "type": "misskey", "settings": {"base_url": "https://misskey.prod/", "api_token": "token2"}},
        original_id="misskey_main",
    )

    assert [item["id"] for item in platforms] == ["misskey_prod", "onebot_main"]
    assert platforms[0]["settings"]["base_url"] == "https://misskey.prod/"

    platforms = delete_platform(platforms, "onebot_main")
    assert [item["id"] for item in platforms] == ["misskey_prod"]

    try:
        upsert_platform(platforms, {"id": "misskey_prod", "type": "misskey", "settings": {}}, original_id="other")
    except ValueError as e:
        assert "已存在" in str(e)
    else:
        raise AssertionError("应拒绝重复平台 id")

def test_config_secret_redaction_and_masked_value_preservation() -> None:
    """配置凭据读取时脱敏, 掩码回写时保留原值, 空字符串可明确清空"""
    current: _ConfigDocument = {
        "max_tokens": 4096,
        "platforms": [{
            "id": "misskey",
            "settings": {"api_token": "real-token", "base_url": "https://old.example"},
        }],
    }
    redacted = redact_config_document(current)
    assert isinstance(redacted, dict)
    redacted_config = cast(_ConfigDocument, redacted)
    assert redacted_config["max_tokens"] == 4096
    assert redacted_config["platforms"][0]["settings"]["api_token"] == "********"

    submitted: _ConfigDocument = {
        "max_tokens": 8192,
        "platforms": [{
            "id": "misskey",
            "settings": {"api_token": "********", "base_url": "https://new.example"},
        }],
    }
    merged = merge_masked_secrets(current, submitted)
    assert isinstance(merged, dict)
    merged_config = cast(_ConfigDocument, merged)
    assert merged_config["platforms"][0]["settings"]["api_token"] == "real-token"
    assert merged_config["platforms"][0]["settings"]["base_url"] == "https://new.example"

    cleared_payload: dict[str, object] = {
        "platforms": [{"id": "misskey", "settings": {"api_token": ""}}],
    }
    cleared = merge_masked_secrets(current, cleared_payload)
    assert isinstance(cleared, dict)
    cleared_config = cast(_ConfigDocument, cleared)
    assert cleared_config["platforms"][0]["settings"]["api_token"] == ""

def test_save_config_document_validates_platforms(tmp_path: Path):
    """保存配置前应校验 platforms 结构"""
    path = tmp_path / "satrap" / "config.yaml"
    invalid_data: dict[str, Any] = {"platforms": [{"id": "", "type": "misskey", "settings": {}}]}

    try:
        save_config_document(path, invalid_data)
    except ValueError as e:
        assert "id 不能为空" in str(e)
    else:
        raise AssertionError("应拒绝空平台 id")


@pytest.mark.asyncio
async def test_chat_administrator_configuration_save_preview_apply_and_rebind(tmp_path, monkeypatch):
    """同一设置流程保存群授权, 规范群号, 预览, 应用及显式重绑, 拒绝伪造平台代次"""
    from satrap.core.config.administrator_groups import AdministratorService
    from satrap.core.call_context import CallOrigin
    from satrap.core.backend import control_server as control

    path = tmp_path / "config.json"
    saved = save_config_document(path, document())
    config = BackendConfig.from_dict(saved)
    config.source_path = str(path)
    backend = BackendManager(config)
    origin = CallOrigin("ob", "10000", "GroupMessage", "456", "789", "message", "request")
    assert not backend.administrator_service.resolve(origin, "group_admin").allowed
    proposed = draft()
    proposed[0]["members"] = []
    proposed[0]["chats"] = [{"platform_id": "ob", "chat_id": "000456", "platform_instance_id": "forged"}]
    preview = preview_administrator_groups(saved, proposed, overrides=[])
    assert preview["chats"][0]["chat_id"] == "456" and preview["members"] == []
    assert next(item for item in preview["chats"][0]["plugins"] if item["name"] == "group_admin")["allowed"]
    assert load_config_document(path) == saved
    monkeypatch.setattr(control, "CONFIG_PATH", path)
    monkeypatch.setattr(control, "_administrator_runtime_request", lambda *args, **kwargs: {"status": "next_start"})
    status, response = await control._route_administrator_settings(context(control, "PUT", "/config/administrator-groups", {
        "groups": proposed, "overrides": [], "expected_revision": config_document_revision(saved),
    }))
    assert status == 200
    saved = load_config_document(path)
    chat = saved["administrator_groups"][0]["chats"][0]
    assert chat == {"platform_id": "ob", "platform_instance_id": "a" * 32, "chat_id": "456"}
    api = BackendHTTPServer(backend)
    status, applied = await api._route("POST", "/api/administrators/apply", json.dumps({"section_revision": response["section_revision"]}).encode())
    assert status == 200 and applied["section_revision"] == response["section_revision"]
    assert backend.administrator_service.resolve(origin, "group_admin").allowed
    assert backend.administrator_service.protected_users("ob") == []
    saved["platforms"][0]["instance_id"] = "b" * 32
    saved = save_config_document(path, saved)
    submitted = deepcopy(saved["administrator_groups"])
    submitted[0]["chats"][0]["platform_instance_id"] = "b" * 32
    assert prepare_administrator_groups(saved, submitted)[0]["chats"][0]["platform_instance_id"] == "a" * 32
    assert len(administrator_settings_snapshot(saved)["invalid_chats"]) == 1
    status, response = await control._route_administrator_settings(context(control, "POST", "/config/administrator-groups/preview", {
        "groups": submitted, "overrides": [], "rebind_chats": [{"group_id": "admins", "platform_id": "ob", "chat_id": "456"}],
    }))
    assert status == 200 and response["groups"][0]["chats"][0]["platform_instance_id"] == "b" * 32
    assert load_config_document(path) == saved
    rebound = save_administrator_groups(path, submitted, overrides=[], expected_revision=config_document_revision(saved),
                                       rebind_chats=[{"group_id": "admins", "platform_id": "ob", "chat_id": "456"}])
    assert not administrator_settings_snapshot(rebound)["invalid_chats"]
    service = AdministratorService(lambda: rebound["platforms"], rebound["administrator_groups"])
    assert service.resolve(origin, "group_admin").allowed
    for invalid in ("not-a-group", "0", "-1"):
        bad = deepcopy(submitted); bad[0]["chats"][0]["chat_id"] = invalid
        with pytest.raises(ValueError): prepare_administrator_groups(rebound, bad)
    with pytest.raises(ValueError):
        prepare_administrator_groups(rebound, submitted, rebind_chats=[{"group_id": "missing", "platform_id": "ob", "chat_id": "456"}])
