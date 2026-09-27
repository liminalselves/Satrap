"""验证逐群审批的持久占用、幂等与请求目标边界"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
import asyncio
import sqlite3

import pytest

from satrap.core.backend.BackendManager import BackendConfig, BackendManager
from satrap.core.config.group_actions import GroupActionStore
from satrap.core.config.group_directory import GroupDirectoryStore
from satrap.core.config.group_store import GroupConfigConflict
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.platform import PlatformAdapterManager, PlatformConfig, set_current_adapter_manager
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.onebot.admin import AdminActionUnconfirmed
from satrap.core.platform.onebot.group_action_types import normalize_action_params
from satrap.core.platform.receipt import SendReceipt
from satrap.core.framework.SessionManager import SessionManager
from satrap.core.framework.providers import EdictumProvider
from satrap.core.type import SessionConfig
from satrap.edictum.config import EdictumConfigManager
from satrap.edictum.plugin_compatibility import PluginEnvironment
from satrap.edictum.registry import create_default_edictum_type_registry
from satrap.expend.plugins.group_admin.tools import AsyncGroupAdminTool, _build_tools


def test_pending_decision_is_atomic_and_cannot_replay(tmp_path: Path) -> None:
    store = GroupActionStore(tmp_path / "platform.db")
    params = {"user_id": "42", "reject_add_request": False}
    record, created = store.submit(
        "action-0001", "10000", "456", "kick_group_member", params,
        "panel", 7, approval_required=True,
    )
    assert created and record["state"] == "pending"
    duplicate, created = store.submit(
        "action-0001", "10000", "456", "kick_group_member", params,
        "panel", 7, approval_required=True,
    )
    assert not created and duplicate == record
    with pytest.raises(GroupConfigConflict):
        store.submit("action-0001", "10000", "456", "kick_group_member",
                     {"user_id": "43"}, "panel", 7, approval_required=True)
    occupied = store.decide("10000", "456", "action-0001", approve=True, policy_revision=7)
    assert occupied["state"] == "executing"
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-0001", approve=True, policy_revision=7)
    settled = store.settle("10000", "456", "action-0001", "succeeded", "platform_confirmed")
    assert settled["state"] == "succeeded"
    with pytest.raises(GroupConfigConflict):
        store.settle("10000", "456", "action-0001", "succeeded", "platform_confirmed")


def test_expiration_and_policy_change_persist_after_conflict(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    for action_id in ("action-expire", "action-policy"):
        store.submit(action_id, "10000", "456", "set_group_whole_ban",
                     {"enable": True}, "panel", 3, approval_required=True)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE group_actions SET expires_at=0 WHERE action_id='action-expire'")
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-expire", approve=True, policy_revision=3)
    expired = store.get("10000", "456", "action-expire")
    assert expired is not None and expired["state"] == "expired"
    with pytest.raises(GroupConfigConflict):
        store.decide("10000", "456", "action-policy", approve=True, policy_revision=4)
    policy = store.get("10000", "456", "action-policy")
    assert policy is not None
    assert policy["state"] == "expired"
    assert policy["result"]["reason"] == "policy_changed"


def test_executing_recovers_unknown_without_retry(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    store.submit("action-0002", "10000", "456", "leave_group",
                 {"dismiss": False}, "model", 2, approval_required=False)
    recovered = GroupActionStore(database, recover=True)
    record = recovered.get("10000", "456", "action-0002")
    assert record is not None
    assert record["state"] == "unknown"
    assert record["result"]["reason"] == "interrupted_restart"
    with pytest.raises(GroupConfigConflict):
        recovered.settle("10000", "456", "action-0002", "succeeded", "platform_confirmed")


def test_pending_model_action_expires_when_source_is_lost_on_restart(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    origin = {"adapter_id": "bot", "self_id": "100", "chat_type": "GroupMessage",
              "chat_id": "456", "actor_id": "123", "session_id": "sid", "tool_name": "group_admin_kick"}
    record, _ = store.submit("model-restart-1", "100", "456", "kick_group_member", {"user_id": "42"},
                             "model", 1, approval_required=True, model_origin=origin)
    assert record["state"] == "pending"
    assert "model_origin" not in record
    assert store.model_origin("100", "456", "model-restart-1") == origin
    recovered = GroupActionStore(database, recover=True)
    expired = recovered.get("100", "456", "model-restart-1")
    assert expired is not None and expired["state"] == "expired"
    assert expired["result"]["reason"] == "model_source_lost_on_restart"


def test_v2_action_database_migrates_without_losing_records(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    store.submit("legacy-action-1", "100", "456", "set_group_name", {"name": "旧群名"},
                 "panel", 1, approval_required=True)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE group_actions DROP COLUMN model_origin_json")
        connection.execute("PRAGMA user_version = 2")
    migrated = GroupActionStore(database)
    record = migrated.get("100", "456", "legacy-action-1")
    assert record is not None and record["state"] == "pending"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "model_origin_json" in {row[1] for row in connection.execute("PRAGMA table_info(group_actions)")}


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["write", "caller", "group", "tool"])
async def test_pending_model_action_rechecks_current_permission(tmp_path: Path, revocation: str) -> None:
    """审批不能替已撤销的模型工具权限补授权"""
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[
        {"id": "bot", "type": "onebot", "settings": settings},
    ]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.confirm_membership("100", "456", True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
        "bot", "100", gid, "model-pending-1", action, params, actor_kind="model",
    )
    set_current_adapter_manager(backend._adapter_mgr)
    config = {"write_tools_enabled": True, "allowed_callers": "123", "allowed_groups": "456"}
    tool = next(item for item in _build_tools(AsyncGroupAdminTool, config) if item.tool_name == "group_admin_kick")
    origin = CallOrigin("bot", "100", "GroupMessage", "456", "123", "message-1", "request-1")
    try:
        with bind_call_origin(origin):
            submitted = await tool.execute(user_id="42")
        assert submitted["data"]["state"] == "pending"
        adapter._bot.set_group_kick.assert_not_awaited()
        if revocation == "write":
            config["write_tools_enabled"] = False
        elif revocation == "caller":
            config["allowed_callers"] = "999"
        elif revocation == "group":
            config["allowed_groups"] = "789"
        else:
            tool.disable()
        result = await backend.decide_group_action("bot", "100", "456", "model-pending-1", approve=True)
        assert result["state"] == "failed"
        assert result["result"]["reason"] == "model_permission_revoked"
        adapter._bot.set_group_kick.assert_not_awaited()
    finally:
        set_current_adapter_manager(None)


@pytest.mark.asyncio
async def test_pending_model_action_uses_latest_source_group_plugin_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """群 A 停用管理插件后不能批准它提交给群 B 的旧动作"""
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(data_root=str(tmp_path), platforms=[
        {"id": "bot", "type": "onebot", "session_provider": "edictum", "session_type": "assistant", "settings": settings},
    ]))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.patch_account("100", expected_revision=1, mode="all", approval_defaults={})
    for group_id in ("456", "789"):
        directory.confirm_membership("100", group_id, True)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    registry = create_default_edictum_type_registry()
    config_manager = EdictumConfigManager(registry, tmp_path / "edictum.json")
    config_manager.create("assistant", {
        "edictum_type": "async_simple", "model_name": "base",
        "plugins": [{"name": "group_admin", "enabled": True, "config": {
            "write_tools_enabled": True, "allowed_callers": "123", "allowed_groups": "789",
        }}],
    })
    provider = EdictumProvider(config_manager, registry, default_checkpoint_db=str(backend.platform_db_path("bot")))
    manager = SessionManager(db_path=backend.platform_db_path("bot"), platform_id="bot")
    manager.register_provider(provider)
    cfg = SessionConfig(session_id="source-session", session_type_name="assistant", provider_name="edictum")
    manager.store.upsert(cfg)
    session = provider.create_session(cfg, llm=cast(Any, object()))
    session.plugin_environment = PluginEnvironment("platform", "onebot")
    manager.pool.put("source-session", session, "assistant")
    backend._platform_runtimes["bot"] = cast(Any, (manager, None))
    adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
        "bot", "100", gid, "source-group-action", action, params, actor_kind="model",
    )
    set_current_adapter_manager(backend._adapter_mgr)
    try:
        await provider.prepare_session_async(session)
        tool = session._wf.tools_manager.tools["group_admin_kick"]
        origin = CallOrigin("bot", "100", "GroupMessage", "456", "123", "message-1", "request-1")
        with bind_call_origin(origin):
            submitted = await tool.execute(group_id="789", user_id="42")
        assert submitted["data"]["state"] == "pending"
        directory.patch_group("100", "456", "session", {"plugins": {"mode": "value", "value": [
            {"name": "group_admin", "mode": "disabled"},
        ]}}, expected_revision=0)
        result = await backend.decide_group_action("bot", "100", "789", "source-group-action", approve=True)
        assert result["state"] == "failed" and result["result"]["reason"] == "model_permission_revoked"
        adapter._bot.set_group_kick.assert_not_awaited()
        directory.patch_group("100", "456", "session", {"plugins": {"mode": "value", "value": [
            {"name": "group_admin", "mode": "enabled"},
        ]}}, expected_revision=1)
        adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
            "bot", "100", gid, "race-source-action", action, params, actor_kind="model",
        )
        with bind_call_origin(origin):
            race = await tool.execute(group_id="789", user_id="42")
        assert race["data"]["state"] == "pending"
        entered = asyncio.Event()
        release = asyncio.Event()
        original_kick = adapter.admin.kick_group_member

        async def delayed_kick(*args: object, **kwargs: object) -> None:
            entered.set()
            await release.wait()
            await original_kick(*args, **kwargs)

        monkeypatch.setattr(adapter.admin, "kick_group_member", delayed_kick)
        decision = asyncio.create_task(backend.decide_group_action(
            "bot", "100", "789", "race-source-action", approve=True,
        ))
        await entered.wait()
        directory.patch_group("100", "456", "session", {"plugins": {"mode": "value", "value": [
            {"name": "group_admin", "mode": "disabled"},
        ]}}, expected_revision=2)
        release.set()
        race_result = await decision
        assert race_result["state"] == "failed" and race_result["result"]["reason"] == "model_permission_revoked"
        adapter._bot.set_group_kick.assert_not_awaited()
        adapter.group_action_handler = lambda gid, action, params: backend.submit_group_action(
            "bot", "100", gid, "invalid-source-action", action, params, actor_kind="model",
        )
        with sqlite3.connect(backend.platform_db_path("bot")) as connection:
            connection.execute("UPDATE group_configs SET config_json='{' WHERE self_id='100' AND group_id='456'")
        with bind_call_origin(origin):
            denied = await tool.execute(group_id="789", user_id="42")
        assert denied["status"] == "error"
        assert GroupActionStore(backend.platform_db_path("bot")).get("100", "789", "invalid-source-action") is None
    finally:
        await provider.release_session_async(session)
        set_current_adapter_manager(None)


def test_group_request_flag_never_enters_persistent_parameters(tmp_path: Path) -> None:
    params, flag = normalize_action_params(
        "handle_group_request", {"flag": "secret-flag", "sub_type": "invite", "approve": False}, "10000",
    )
    assert flag == "secret-flag"
    assert "flag" not in params and "flag_digest" in params
    database = tmp_path / "platform.db"
    store = GroupActionStore(database)
    store.submit("action-0003", "10000", "456", "handle_group_request",
                 params, "panel", 1, approval_required=True)
    assert b"secret-flag" not in database.read_bytes()
    recovered = GroupActionStore(database, recover=True)
    record = recovered.get("10000", "456", "action-0003")
    assert record is not None and record["state"] == "expired"
    assert record["result"]["reason"] == "request_flag_lost_on_restart"


@pytest.mark.asyncio
async def test_runtime_approval_and_auto_mode_execute_exactly_once(tmp_path: Path) -> None:
    """关闭响应的已加入群仍可管理, 人工批准前没有平台副作用"""
    settings = {"group_management_version": 1}
    backend = BackendManager(BackendConfig(
        data_root=str(tmp_path), platforms=[{"id": "bot", "type": "onebot", "settings": settings}],
    ))
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    adapter.bot_self_id = "100"
    adapter._bot = AsyncMock()
    backend._adapter_mgr = PlatformAdapterManager()
    backend._adapter_mgr._adapters["bot"] = adapter
    directory = GroupDirectoryStore(backend.platform_db_path("bot"))
    directory.adopt_legacy("100", settings)
    directory.confirm_membership("100", "456", True)
    directory.patch_group("100", "456", "approval", {
        "set_group_name": {"mode": "value", "value": "approval_required"},
    }, expected_revision=0)
    adapter.set_group_access_store(directory)
    await adapter.refresh_group_access("100")
    assert not adapter.allows_group("456")
    assert adapter.allows_management_target("456")
    adapter.admin.set_group_name = AsyncMock()
    params: dict[str, object] = {"name": "新群名"}
    pending = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-1", "set_group_name", params, actor_kind="panel",
    )
    assert pending["state"] == "pending"
    adapter.admin.set_group_name.assert_not_awaited()
    approved = await backend.decide_group_action("bot", "100", "456", "action-runtime-1", approve=True)
    assert approved["state"] == "succeeded"
    adapter.admin.set_group_name.assert_awaited_once_with("456", name="新群名")
    duplicate = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-1", "set_group_name", params, actor_kind="panel",
    )
    assert duplicate == approved
    adapter.admin.set_group_name.assert_awaited_once()
    with pytest.raises(GroupConfigConflict):
        await backend.decide_group_action("bot", "100", "456", "action-runtime-1", approve=True)
    directory.patch_group("100", "456", "approval", {
        "set_group_name": {"mode": "value", "value": "auto_execute"},
    }, expected_revision=1)
    executed = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-2", "set_group_name", {"name": "自动群名"}, actor_kind="panel",
    )
    assert executed["state"] == "succeeded"
    assert adapter.admin.set_group_name.await_count == 2
    adapter.admin.set_group_name = AsyncMock(side_effect=AdminActionUnconfirmed("网络超时"))
    unknown = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-3", "set_group_name", {"name": "未知群名"}, actor_kind="panel",
    )
    assert unknown["state"] == "unknown"
    repeated = await backend.submit_group_action(
        "bot", "100", "456", "action-runtime-3", "set_group_name", {"name": "未知群名"}, actor_kind="panel",
    )
    assert repeated == unknown
    adapter.admin.set_group_name.assert_awaited_once()
    adapter._running = True
    adapter._capability_states["set_group_name"] = (adapter.connection_generation(), "unsupported")
    metadata = await backend.group_action_types("bot", "100", "456")
    name_action = next(item for item in metadata["items"] if item["action_type"] == "set_group_name")
    assert name_action["capability"] == "unsupported" and name_action["available"] is False
    adapter.send_management_message = AsyncMock(return_value=SendReceipt("unknown", reason="network_timeout"))
    manual = await backend.send_group_message("bot", "100", "456", "action-send-1", "直接发送")
    assert manual["state"] == "unknown"
    repeated_send = await backend.send_group_message("bot", "100", "456", "action-send-1", "直接发送")
    assert repeated_send == manual
    adapter.send_management_message.assert_awaited_once_with("456", "直接发送")
    with pytest.raises(GroupConfigConflict):
        await backend.send_group_message("bot", "100", "456", "action-send-1", "另一条消息")
