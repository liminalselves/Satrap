"""逐群账号配置, 旧配置采用和平台数据库迁移反例"""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from satrap.core.config.document import upsert_platform
from satrap.core.config.group_store import GroupConfigConflict, GroupConfigStore, GroupLegacyConflict
from satrap.core.config.session_overrides import SessionOverrideStore
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformConfig
from satrap.core.conversation import ConversationRoute


def test_new_platform_defaults_to_selected_without_groups(tmp_path: Path) -> None:
    platform = upsert_platform([], {"id": "bot", "type": "onebot", "settings": {}})[0]
    assert platform["settings"]["group_management_version"] == 1
    store = GroupConfigStore(tmp_path / "platform.db")
    assert store.adopt_legacy("100", platform["settings"])["mode"] == "selected"
    assert store.read_group("100", "123")["revision"] == 0


def test_old_empty_whitelist_keeps_all_and_old_group_policy(tmp_path: Path) -> None:
    store = GroupConfigStore(tmp_path / "platform.db")
    old = {"group_whitelist": [], "wake_group_overrides": {"123": {"wake_cooldown": 3}}}
    assert store.adopt_legacy("100", old)["mode"] == "all"
    assert store.read_group("100", "123")["explicit"]["policy"] == {
        "wake_cooldown": {"mode": "value", "value": 3},
    }
    assert store.adopt_legacy("100", old)["revision"] == 1
    with pytest.raises(GroupLegacyConflict):
        store.check_legacy_source("100", {"group_whitelist": ["123"]})


def test_old_whitelist_and_accounts_are_isolated(tmp_path: Path) -> None:
    store = GroupConfigStore(tmp_path / "platform.db")
    old = {"group_whitelist": ["123"], "wake_group_overrides": {"456": {"wake_mode": "frequency"}}}
    assert store.adopt_legacy("100", old)["mode"] == "selected"
    assert store.read_group("100", "123")["explicit"]["policy"]["enabled"] == {
        "mode": "value", "value": True,
    }
    assert "enabled" not in store.read_group("100", "456")["explicit"]["policy"]
    assert store.read_account("200") is None
    assert store.read_group("200", "123")["revision"] == 0
    assert store.adopt_legacy("200", old)["mode"] == "selected"
    assert store.read_group("200", "123")["revision"] == 0


def test_area_patch_keeps_other_sections_and_changes_route_only_for_binding(tmp_path: Path) -> None:
    store = GroupConfigStore(tmp_path / "platform.db")
    store.adopt_legacy("100", {"group_management_version": 1})
    first = store.patch_group("100", "123", "policy", {"enabled": {"mode": "value", "value": True}}, expected_revision=0)
    assert first["route_generation"] == 0
    binding = {"mode": "value", "value": {"provider": "edictum", "config_name": "assistant"}}
    second = store.patch_group("100", "123", "session", {"binding": binding}, expected_revision=1)
    assert second["route_generation"] == 1
    assert second["explicit"]["policy"] == first["explicit"]["policy"]
    third = store.patch_group("100", "123", "session", {"binding": binding, "prompt": {"mode": "value", "value": ""}}, expected_revision=2)
    assert third["route_generation"] == 1
    with pytest.raises(GroupConfigConflict):
        store.patch_group("100", "123", "events", {}, expected_revision=2)


def test_schema_migration_rejects_declared_missing_table(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(RuntimeError, match="session_config_overrides"):
        GroupConfigStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='group_accounts'").fetchone() is None


def test_override_store_uses_central_schema_without_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    SessionOverrideStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        connection.execute("PRAGMA user_version = 3")
    SessionOverrideStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_policy_values_reject_unknown_and_ambiguous_values(tmp_path: Path) -> None:
    store = GroupConfigStore(tmp_path / "platform.db")
    store.adopt_legacy("100", {"group_management_version": 1})
    with pytest.raises(ValueError, match="不支持字段"):
        store.patch_group("100", "123", "policy", {"enable_group": {"mode": "value", "value": True}}, expected_revision=0)
    with pytest.raises(ValueError, match="布尔值"):
        store.patch_group("100", "123", "policy", {"enabled": {"mode": "value", "value": "true"}}, expected_revision=0)
    with pytest.raises(ValueError, match="wake_cooldown"):
        store.patch_group("100", "123", "policy", {"wake_cooldown": {"mode": "value", "value": -1}}, expected_revision=0)
    assert store.read_group("100", "123")["revision"] == 0


def test_route_generation_preserves_old_key_and_separates_new_history() -> None:
    old = ConversationRoute(user_id="200", platform="bot", session_type="assistant",
                            scope="group_member", self_id="100", group_id="123")
    new = ConversationRoute(user_id="200", platform="bot", session_type="assistant",
                            scope="group_member", self_id="100", group_id="123", generation=1)
    assert old.key != new.key
    assert old.context_value != new.context_value
    legacy = ConversationRoute(user_id="200", platform="bot", session_type="assistant")
    changed = ConversationRoute(user_id="200", platform="bot", session_type="assistant",
                                self_id="100", group_id="123", generation=1)
    assert legacy.key is None and changed.key is not None


@pytest.mark.asyncio
async def test_runtime_access_requires_explicit_enable_and_bound_account(tmp_path: Path) -> None:
    settings = {"group_management_version": 1}
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    store = GroupConfigStore(tmp_path / "platform.db")
    adapter.set_group_access_store(store)
    await adapter._handle_meta({"self_id": "100", "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert not adapter.allows_group("123")
    store.patch_group("100", "123", "policy", {"enabled": {"mode": "value", "value": True}}, expected_revision=0)
    await adapter.refresh_group_access("100")
    assert adapter.allows_group("123")
    assert not adapter.allows_group("456")
    await adapter._handle_meta({"self_id": "200", "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert adapter.bot_self_id == "100"
    assert store.read_account("200") is None
    assert adapter.allows_group("123")


@pytest.mark.asyncio
async def test_old_access_and_legacy_conflict_fail_closed(tmp_path: Path) -> None:
    settings = {"group_whitelist": ["123"]}
    adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=settings))
    store = GroupConfigStore(tmp_path / "platform.db")
    adapter.set_group_access_store(store)
    await adapter._handle_meta({"self_id": "100", "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert adapter.allows_group("123")
    assert not adapter.allows_group("456")
    settings["group_whitelist"] = ["456"]
    with pytest.raises(GroupLegacyConflict):
        await adapter._handle_meta({"self_id": "100", "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert not adapter.allows_group("123")
    assert not adapter.allows_group("456")
