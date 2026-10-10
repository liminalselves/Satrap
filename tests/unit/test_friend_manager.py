"""好友管理的人工与模型授权, 迁移, 并发占用和平台扩展契约"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import ast
import json
import sqlite3

import pytest

from satrap.core.friends import FriendError
from satrap.core.friends.service import FriendService
from satrap.core.friends.store import FriendStore
from satrap.core.config.group_action_origin import ModelActionAuthorization
from satrap.edictum.plugin_catalog import PluginCatalog
from satrap.edictum.plugin_spec import parse_plugin_specs


class GenericFriends:
    """使用非数字账号的通用适配器替身"""

    def __init__(self):
        self.config = SimpleNamespace(enable=True, settings={})
        self.account = "bot@example.test"
        self.generation = 1
        self.items = [{"user_id": "alice", "nickname": "同名", "remark": "甲"},
                      {"user_id": "bob", "nickname": "同名", "remark": "乙"},
                      {"user_id": "manager", "nickname": "管理者", "remark": ""}]
        self.complete = True
        self.delete_calls = 0
        self.handle_calls = []
        self.friend_requests = AsyncMock(return_value={"items": [], "has_more": False, "next_cursor": None})

    def friend_account(self):
        return self.account

    def friend_generation(self):
        return self.generation

    def friend_capabilities(self):
        return {key: {"state": "supported"} for key in ("list_friends", "list_requests", "handle_request", "delete_friend")}

    async def friend_list(self, account):
        return {"items": deepcopy(self.items), "complete": self.complete}

    async def friend_delete(self, account, user_id):
        self.delete_calls += 1
        self.items = [row for row in self.items if row["user_id"] != user_id]

    async def friend_handle(self, account, request_id, approve, remark):
        self.handle_calls.append((request_id, approve, remark))


def setup(tmp_path):
    adapter = GenericFriends()
    active = [adapter]
    permission = {"enabled": True, "version": "initial"}
    def check(source, target):
        source.verify(target)
        if not permission["enabled"]:
            raise PermissionError("已撤权")
        return permission["version"]
    host = FriendService("generic", tmp_path / "platform.db", lambda: active[0], check, lambda: ["manager"])
    source = ModelActionAuthorization({"adapter_id": "generic", "self_id": adapter.account}, lambda target: None)
    return host, adapter, source, active, permission


@pytest.mark.asyncio
async def test_search_ambiguity_pagination_and_scope(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    first = await host.list_friends(adapter.account, actor="manager", query="同名", limit=1)
    assert first["ambiguous"] and first["has_more"] and first["coverage"]["complete"]
    assert first["items"][0]["matched_by"] == ["nickname"]
    second = await host.list_friends(adapter.account, actor="manager", query="同名", limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["user_id"] == "bob" and not second["has_more"]
    for actor, query in [("other", "同名"), ("manager", "乙")]:
        with pytest.raises(FriendError, match="当前账号或查询"):
            await host.list_friends(adapter.account, actor=actor, query=query, cursor=first["next_cursor"])
    adapter.generation += 1
    with pytest.raises(FriendError) as caught:
        await host.list_friends(adapter.account, actor="manager", query="同名", cursor=first["next_cursor"])
    assert caught.value.code == "cursor_expired"


@pytest.mark.asyncio
async def test_incomplete_directory_cannot_prove_absence(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    adapter.complete = False
    found = await host.list_friends(adapter.account, actor="manager", query="alice")
    assert not found["coverage"]["complete"] and found["items"][0]["exact"]
    with pytest.raises(FriendError) as caught:
        await host.submit(adapter.account, "delete-absent", "delete_friend", {"user_id": "missing"}, actor="panel")
    assert caught.value.code == "incomplete_directory" and adapter.delete_calls == 0


@pytest.mark.asyncio
async def test_human_deletion_no_plugin_approval_idempotency_and_history_kept(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    store = await host.store()
    with store.connection() as conn:
        conn.execute("INSERT INTO session_config_overrides(session_id,namespace,config_json,updated_at) VALUES(?,?,?,?)",
                     ("history", "history", '{}', 1))
    record = await host.submit(adapter.account, "human-delete", "delete_friend", {"user_id": "alice"}, actor="panel")
    assert record["state"] == "succeeded" and record["result"]["verification"] == "confirmed"
    assert record["target"]["nickname"] == "同名"
    same = await host.submit(adapter.account, "human-delete", "delete_friend", {"user_id": "alice"}, actor="panel")
    assert same == record and adapter.delete_calls == 1
    with pytest.raises(FriendError) as caught:
        await host.submit(adapter.account, "human-delete", "delete_friend", {"user_id": "bob"}, actor="panel")
    assert caught.value.code == "action_conflict"
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_config_overrides").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_model_deletion_requires_approval_and_concurrent_decision_executes_once(tmp_path):
    host, adapter, source, _, _ = setup(tmp_path)
    record = await host.submit(adapter.account, "model-delete", "delete_friend", {"user_id": "alice"}, actor="model", source=source)
    assert record["state"] == "pending" and adapter.delete_calls == 0
    results = await asyncio.gather(host.decide(adapter.account, "model-delete", True), host.decide(adapter.account, "model-delete", True))
    assert adapter.delete_calls == 1 and any(row["state"] == "succeeded" for row in results)
    assert (await host.decide(adapter.account, "model-delete", True))["state"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["permission", "fingerprint", "account", "connection", "replacement", "protection"])
async def test_pending_model_action_rechecks_every_boundary(tmp_path, revocation):
    host, adapter, source, active, permission = setup(tmp_path)
    await host.submit(adapter.account, "pending", "delete_friend", {"user_id": "alice"}, actor="model", source=source)
    if revocation == "permission":
        permission["enabled"] = False
    elif revocation == "fingerprint":
        permission["version"] = "changed"
    elif revocation == "account":
        adapter.account = "new-bot"
    elif revocation == "connection":
        adapter.generation += 1
    elif revocation == "replacement":
        active[0] = GenericFriends()
    else:
        await host.policy(adapter.account, ["alice"])
    if revocation == "account":
        with pytest.raises(FriendError):
            await host.decide("bot@example.test", "pending", True)
    else:
        assert (await host.decide(adapter.account, "pending", True))["state"] == "failed"
    assert adapter.delete_calls == 0


@pytest.mark.asyncio
async def test_manual_protection_rejection_and_pending_rejection(tmp_path):
    host, adapter, source, _, _ = setup(tmp_path)
    for uid in ["manager", adapter.account]:
        with pytest.raises(FriendError) as caught:
            await host.submit(adapter.account, uid, "delete_friend", {"user_id": uid}, actor="panel")
        assert caught.value.code == "protected_friend"
    await host.policy(adapter.account, ["alice"])
    with pytest.raises(FriendError):
        await host.submit(adapter.account, "protected", "delete_friend", {"user_id": "alice"}, actor="panel")
    await host.submit(adapter.account, "reject", "delete_friend", {"user_id": "bob"}, actor="model", source=source)
    assert (await host.decide(adapter.account, "reject", False))["state"] == "rejected" and adapter.delete_calls == 0


@pytest.mark.asyncio
async def test_unknown_blocks_replay_even_with_new_id_and_restart_recovers(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    adapter.friend_delete = AsyncMock(side_effect=FriendError("unconfirmed", "超时"))
    record = await host.submit(adapter.account, "unknown", "delete_friend", {"user_id": "alice"}, actor="panel")
    assert record["state"] == "unknown"
    assert (await host.submit(adapter.account, "unknown", "delete_friend", {"user_id": "alice"}, actor="panel"))["state"] == "unknown"
    with pytest.raises(FriendError) as caught:
        await host.submit(adapter.account, "other-id", "delete_friend", {"user_id": "alice"}, actor="panel")
    assert caught.value.code == "unresolved_action"
    adapter.friend_delete.assert_awaited_once()
    store = await host.store()
    store.register(adapter.account, "interrupted", "handle_request", {"request_id": "request-1", "approve": True}, "panel")
    store.transition(adapter.account, "interrupted", "ready", "executing")
    recovered = FriendStore(tmp_path / "platform.db")
    assert recovered.get(adapter.account, "interrupted")["state"] == "unknown"


@pytest.mark.asyncio
async def test_cancellation_settles_unknown_and_no_replay(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    entered = asyncio.Event()
    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()
    adapter.friend_delete = wait
    task = asyncio.create_task(host.submit(adapter.account, "cancel", "delete_friend", {"user_id": "alice"}, actor="panel"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await host.actions(adapter.account))["items"][0]["state"] == "unknown"


@pytest.mark.asyncio
async def test_request_handling_human_direct_and_strict_parameters(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    accepted = await host.submit(adapter.account, "accept", "handle_request", {"request_id": "request-1", "approve": True, "remark": "备注"}, actor="panel")
    assert accepted["state"] == "succeeded" and adapter.handle_calls == [("request-1", True, "备注")]
    for params in [{"request_id": "request-1", "approve": "false"}, {"request_id": "request-1", "approve": False, "remark": "无效"},
                   {"request_id": "request-1", "approve": True, "flag": "secret"}, {"request_id": "request-1", "approve": True, "remark": "x" * 61}]:
        with pytest.raises(FriendError):
            await host.submit(adapter.account, "invalid", "handle_request", params, actor="panel")
    assert len(adapter.handle_calls) == 1


@pytest.mark.asyncio
async def test_request_cursor_bound_to_actor_and_connection(tmp_path):
    host, adapter, _, _, _ = setup(tmp_path)
    adapter.friend_requests.return_value = {"items": [], "has_more": True, "next_cursor": "native-position"}
    result = await host.requests(adapter.account, actor="manager")
    assert result["next_cursor"] != "native-position"
    with pytest.raises(FriendError):
        await host.requests(adapter.account, cursor=result["next_cursor"], actor="other")
    await host.requests(adapter.account, cursor=result["next_cursor"], actor="manager")
    assert adapter.friend_requests.await_args.args[2] == "native-position"
    adapter.generation += 1
    with pytest.raises(FriendError):
        await host.requests(adapter.account, cursor=result["next_cursor"], actor="manager")


@pytest.mark.parametrize("old_enabled,write,callers", [(True, True, "manager"), (False, True, "manager"), (True, False, "manager"), (True, True, "other")])
def test_migration_conservative_idempotent_and_removes_old_entries(tmp_path, monkeypatch, old_enabled, write, callers):
    from satrap.edictum.plugin_config import PluginConfigManager
    monkeypatch.setattr(PluginConfigManager, "load_global_explicit", lambda *args: {})
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    value = [{"name": "group_admin", "enabled": old_enabled, "config": {"request_managers": "manager", "allowed_callers": callers,
              "write_tools_enabled": write}, "capabilities": {"tools": {"group_admin_list_friend_requests": True, "group_admin_handle_friend_request": False}}}]
    before = deepcopy(value)
    specs = {spec.name: spec for spec in parse_plugin_specs(value, catalog)}
    assert value == before
    friend = specs["friend_manager"]
    assert friend.enabled == old_enabled
    assert friend.capabilities["tools"]["friend_manager_list_requests"] == old_enabled
    assert not friend.capabilities["tools"]["friend_manager_handle_request"]
    assert not friend.capabilities["tools"]["friend_manager_list_friends"]
    assert not friend.capabilities["tools"]["friend_manager_find_friends"]
    assert not friend.capabilities["tools"]["friend_manager_delete_friend"]
    assert "request_handling_enabled" not in friend.config
    canonical = [spec.to_config() for spec in specs.values()]
    assert [spec.to_config() for spec in parse_plugin_specs(canonical, catalog)] == canonical


def test_plugins_do_not_import_each_other_and_old_tools_only_in_migration():
    root = Path(__file__).resolve().parents[2] / "satrap"
    for name in ("friend_manager", "group_admin", "group_chat"):
        for path in (root / "expend/plugins" / name).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert all(f"plugins.{other}" not in node.module for other in ("friend_manager", "group_admin", "group_chat") if other != name)
    for path in root.rglob("*.py"):
        if path.name == "plugin_migrations.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "group_admin_list_friend_requests" not in source and "group_admin_handle_friend_request" not in source


def test_migration_preserves_existing_empty_manager_deny_and_new_tool_switches(tmp_path, monkeypatch):
    from satrap.edictum.plugin_config import PluginConfigManager
    monkeypatch.setattr(PluginConfigManager, "load_global_explicit", lambda *args: {})
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    source = [{"name": "group_admin", "config": {"request_managers": "manager", "write_tools_enabled": True},
               "capabilities": {"tools": {"group_admin_list_friend_requests": True}}},
              {"name": "friend_manager", "config": {"managers": "", "write_callers": "manager", "request_handling_enabled": True},
               "capabilities": {"tools": {"friend_manager_delete_friend": False}}}]
    specs = {spec.name: spec for spec in parse_plugin_specs(source, catalog)}
    assert specs["friend_manager"].config["managers"] == ""
    assert not specs["friend_manager"].capabilities["tools"]["friend_manager_handle_request"]
    assert not specs["friend_manager"].capabilities["tools"]["friend_manager_delete_friend"]


def test_implicit_legacy_request_defaults_migrate_global_permissions(tmp_path, monkeypatch):
    from satrap.edictum.plugin_config import PluginConfigManager
    monkeypatch.setattr(PluginConfigManager, "load_global_explicit", lambda self, name, schema:
                        {"request_managers": "manager", "write_tools_enabled": True} if name == "group_admin" else {})
    catalog = PluginCatalog(user_dir=tmp_path / "plugins")
    specs = {spec.name: spec for spec in parse_plugin_specs(["group_admin"], catalog)}
    assert specs["friend_manager"].config["managers"] == "manager"
    assert specs["friend_manager"].config["write_callers"] == "manager"
    assert specs["friend_manager"].capabilities["tools"]["friend_manager_handle_request"]
    assert specs["friend_manager"].capabilities["tools"]["friend_manager_handle_request"]
    assert not specs["friend_manager"].capabilities["tools"]["friend_manager_delete_friend"]


def test_v7_schema_migration_preserves_history_and_checks_missing_friend_tables(tmp_path):
    from satrap.core.config.platform_schema import ensure_platform_tables, PLATFORM_SCHEMA_VERSION
    database = tmp_path / "platform.db"
    with sqlite3.connect(database) as conn:
        ensure_platform_tables(conn)
        conn.execute("INSERT INTO session_config_overrides(session_id, namespace, config_json, updated_at) VALUES('s','prompt','{}',1)")
        conn.execute("DROP TABLE friend_actions")
        conn.execute("DROP TABLE friend_policies")
        for table in ("memory_refs", "memory_proposals", "memory_operations", "memory_audit", "memories",
                      "group_chat_reminder_attempts", "group_chat_reminders", "reminder_operations"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version=7")
        ensure_platform_tables(conn)
        assert conn.execute("SELECT config_json FROM session_config_overrides WHERE session_id='s'").fetchone()[0] == '{}'
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PLATFORM_SCHEMA_VERSION
        conn.execute("DROP TABLE friend_actions")
        with pytest.raises(RuntimeError, match="friend_actions"):
            ensure_platform_tables(conn)


@pytest.mark.asyncio
async def test_management_http_works_without_plugin_and_refuses_account_mismatch(tmp_path):
    from satrap.core.backend.http_api import BackendHTTPServer
    host, adapter, _, _, _ = setup(tmp_path)
    server = object.__new__(BackendHTTPServer)
    server.backend = SimpleNamespace(friend_service=lambda adapter_id: host)
    status, info = await server._route_friends("GET", "/api/platforms/generic/friends/info", b"")
    assert status == 200 and info["current_account"] == adapter.account
    status, page = await server._route_friends("GET", f"/api/platforms/generic/friends?account={adapter.account}&q=同名", b"")
    assert status == 200 and len(page["items"]) == 2
    status, _ = await server._route_friends("POST", "/api/platforms/generic/friends/actions", json.dumps({
        "expected_self_id": "old-account", "action_id": "id", "action_type": "delete_friend", "params": {"user_id": "alice"}}).encode())
    assert status == 409 and adapter.delete_calls == 0
    status, result = await server._route_friends("POST", "/api/platforms/generic/friends/actions", json.dumps({
        "expected_self_id": adapter.account, "action_id": "id", "action_type": "delete_friend", "params": {"user_id": "alice"}}).encode())
    assert status == 200 and result["state"] == "succeeded" and adapter.delete_calls == 1
