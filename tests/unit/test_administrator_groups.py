from copy import deepcopy
import hashlib
import pytest

from satrap.core.call_context import CallOrigin
from satrap.core.config.administrator_groups import (
    AdministratorService, migrate_legacy_scope_exclusions, normalize_administrator_groups,
    normalize_administrator_overrides,
)
from satrap.core.config.platform_identity import platform_instance_id


def platform(identity="qq", instance="instance-1"):
    return {"id": identity, "type": "future-adapter", "instance_id": instance}


def group(identity="admins", user="user-alpha", *, mode="selected", included=None, excluded=None, enabled=True, protect=None):
    value = {"id": identity, "name": "管理员", "enabled": enabled,
             "members": [{"platform_id": "qq", "platform_instance_id": "instance-1", "user_id": user}],
             "plugin_scope": {"mode": mode, "included": ["example"] if included is None else included, "excluded": excluded or []}}
    if protect is not None:
        value["protect"] = protect
    return value


def override(identity="entry", user="user-beta", *, allow=None, deny=None, enabled=True, protect=None, instance="instance-1"):
    value = {"id": identity, "enabled": enabled, "platform_id": "qq", "platform_instance_id": instance,
             "user_id": user, "allow": allow or [], "deny": deny or []}
    if protect is not None:
        value["protect"] = protect
    return value


def origin(user="user-alpha", adapter="qq"):
    return CallOrigin(adapter, "bot", "GroupMessage", "room", user, "message", "request")


def test_platform_user_identity_is_scoped_and_not_numeric_only():
    platforms = [platform(), platform("other")]
    service = AdministratorService(lambda: platforms, [group()])
    assert service.resolve(origin(), "example").allowed
    assert not service.resolve(origin(adapter="other"), "example").allowed
    assert not service.resolve(origin(user="other-member"), "example").allowed
    assert not service.resolve(None, "example").allowed


def test_groups_union_and_exclusion_priority_only_for_matching_members():
    groups = [group(), group("all", mode="all", included=[]), group("deny", mode="all", included=[], excluded=["example"]),
              group("unrelated", user="someone-else", excluded=["another"])]
    service = AdministratorService(lambda: [platform()], groups)
    result = service.resolve(origin(), "example")
    assert not result.allowed and result.excluded_by == ("deny",)
    assert service.resolve(origin(), "another").allowed
    assert service.protected_users("qq") == ["someone-else", "user-alpha"]


def test_recreated_platform_never_inherits_old_grant_or_protection():
    platforms = [platform()]
    service = AdministratorService(lambda: platforms, [group()])
    platforms[0] = platform(instance="replacement")
    assert not service.resolve(origin(), "example").allowed
    assert service.protected_users("qq") == []
    saved = normalize_administrator_groups([group()], platforms, bind_new=True, previous=[group()])
    assert saved[0]["members"][0]["platform_instance_id"] == "instance-1"


def test_server_binds_new_members_and_keeps_missing_saved_platform():
    submitted = group()
    submitted["members"][0]["platform_instance_id"] = "forged"
    normalized = normalize_administrator_groups([submitted], [platform()], bind_new=True)
    assert normalized[0]["members"][0]["platform_instance_id"] == platform_instance_id(platform())
    assert normalize_administrator_groups(normalized, []) == normalized
    with pytest.raises(ValueError, match="平台不存在"):
        normalize_administrator_groups([submitted], [], bind_new=True)


def test_atomic_snapshot_and_revocation():
    submitted = [group()]
    service = AdministratorService(lambda: [platform()], submitted)
    submitted[0]["members"].clear()
    assert service.resolve(origin(), "example").allowed
    saved, saved_overrides, revision = service.snapshot()
    saved[0]["enabled"] = False
    assert service.resolve(origin(), "example").allowed
    service.apply(saved, saved_overrides)
    assert not service.resolve(origin(), "example").allowed
    assert service.snapshot()[2] != revision
    with pytest.raises(ValueError):
        service.apply({})
    assert service.snapshot()[0] == saved


@pytest.mark.parametrize("mutate", [
    lambda g: g.update(enabled="true"),
    lambda g: g.update(members=[{"platform_id": "qq", "user_id": 123}]),
    lambda g: g["plugin_scope"].update(mode="unknown"),
    lambda g: g["plugin_scope"].update(mode=[]),
    lambda g: g["plugin_scope"].update(included=["example", "example"]),
    lambda g: g.update(unknown="x"),
])
def test_invalid_administrator_configuration_rejected(mutate):
    value = deepcopy(group())
    mutate(value)
    with pytest.raises(ValueError):
        normalize_administrator_groups([value], [platform()])


def test_empty_configuration_and_disabled_group_grant_nothing():
    assert normalize_administrator_groups(None, []) == []
    service = AdministratorService(lambda: [platform()], [group(enabled=False)])
    assert not service.resolve(origin(), "example").allowed
    assert service.protected_users("qq") == []


def test_bad_platform_entry_does_not_interrupt_other_platforms_or_empty_groups():
    platforms = ["bad-config", {"type": "missing-id"}, platform()]
    service = AdministratorService(lambda: platforms, [])
    assert not service.resolve(origin(), "example").allowed
    service.apply([group()])
    assert service.resolve(origin(), "example").allowed


def test_strict_scope_rejects_selected_exclusions_only_on_save_path():
    legacy = group(mode="selected", excluded=["example"])
    with pytest.raises(ValueError, match="成员例外"):
        normalize_administrator_groups([legacy], [platform()], strict_scope=True)
    kept = normalize_administrator_groups([legacy], [platform()])
    assert kept[0]["plugin_scope"]["excluded"] == ["example"]   # 读取路径保留原组合交给迁移


def test_override_normalization_rejects_unknown_fields_and_duplicate_identity():
    with pytest.raises(ValueError):
        normalize_administrator_overrides([{**override(), "unknown": 1}], [platform()])
    with pytest.raises(ValueError):
        normalize_administrator_overrides([override(), override(identity="other")], [platform()])
    with pytest.raises(ValueError):
        normalize_administrator_overrides([override(allow=["example", "example"])], [platform()])


def test_override_flags_default_and_type_are_validated():
    normalized = normalize_administrator_overrides([override(allow=["example"])], [platform()])[0]
    assert normalized["enabled"] is True and normalized["protect"] is False
    for field, value in (("enabled", "true"), ("protect", 1)):
        with pytest.raises(ValueError, match="布尔值"):
            normalize_administrator_overrides([{**override(allow=["example"]), field: value}], [platform()])


def test_empty_override_entries_are_dropped_unless_protecting():
    assert normalize_administrator_overrides([override()], [platform()]) == []
    kept = normalize_administrator_overrides([override(protect=True)], [platform()])
    assert len(kept) == 1 and kept[0]["protect"] is True


def test_override_overlap_is_logged_and_evaluated_as_denial(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr("satrap.core.config.administrator_groups.logger.error", logged.append)
    groups = normalize_administrator_groups([], [platform()])
    overrides = normalize_administrator_overrides([override(allow=["example"], deny=["example"])], [platform()])
    assert logged and "否决优先" in logged[0]
    service = AdministratorService(lambda: [platform()], groups, overrides)
    assert not service.resolve(origin("user-beta"), "example").allowed


def test_override_allow_grants_without_any_group():
    service = AdministratorService(lambda: [platform()], [], [override(allow=["example"])])
    granted = service.resolve(origin("user-beta"), "example")
    assert granted.allowed and granted.allowed_overrides == ("entry",) and granted.group_ids == ()
    assert not service.resolve(origin("user-alpha"), "example").allowed


def test_override_deny_beats_group_allow_and_records_both_sources():
    service = AdministratorService(
        lambda: [platform()], [group()], [override(user="user-alpha", deny=["example"])])
    result = service.resolve(origin(), "example")
    assert not result.allowed
    assert result.group_ids == ("admins",) and result.denied_overrides == ("entry",)


def test_all_mode_group_exclusion_and_override_deny_coexist():
    service = AdministratorService(
        lambda: [platform()], [group(mode="all", included=[], excluded=["other"])], [override(user="user-alpha", deny=["example"])])
    assert not service.resolve(origin(), "example").allowed
    assert not service.resolve(origin(), "other").allowed


def test_disabled_and_rebuilt_identity_match_nothing():
    disabled = AdministratorService(lambda: [platform()], [], [override(allow=["example"], enabled=False)])
    assert not disabled.resolve(origin("user-beta"), "example").allowed
    platforms = [platform()]
    service = AdministratorService(lambda: platforms, [], [override(allow=["example"])])
    platforms[0] = platform(instance="replacement")
    assert not service.resolve(origin("user-beta"), "example").allowed
    saved = normalize_administrator_overrides([override(allow=["example"])], platforms, bind_new=True, previous=[override(allow=["example"])])
    assert saved[0]["platform_instance_id"] == "instance-1"   # 保留旧绑定, 同名平台重建不自动恢复


def test_override_rebind_requires_saved_binding_and_keeps_other_identity():
    platforms = [platform()]
    previous = [override(allow=["example"])]
    forged = override(allow=["example"], instance="forged")
    kept = normalize_administrator_overrides([forged], platforms, bind_new=True, previous=previous)
    assert kept[0]["platform_instance_id"] == "instance-1"
    with pytest.raises(ValueError, match="平台不存在"):
        normalize_administrator_overrides([override(identity="new", user="user-gamma", allow=["example"])], [], bind_new=True)


def test_protect_switch_controls_protection_for_groups_and_overrides():
    off = AdministratorService(lambda: [platform()], [group(protect=False)], [])
    assert off.protected_users("qq") == []
    on = AdministratorService(lambda: [platform()], [group(protect=False)], [override(protect=True)])
    assert on.protected_users("qq") == ["user-beta"]
    disabled = AdministratorService(lambda: [platform()], [], [override(protect=True, enabled=False)])
    assert disabled.protected_users("qq") == []


def test_migration_expands_legacy_exclusions_and_merges_existing_entries():
    groups = normalize_administrator_groups([group(mode="selected", excluded=["example", "other"])], [platform()])
    existing = normalize_administrator_overrides([override(user="user-alpha", deny=["example"])], [platform()])
    migrated_groups, migrated, pending = migrate_legacy_scope_exclusions(groups, existing)
    assert migrated_groups[0]["plugin_scope"]["excluded"] == []
    assert migrated[0]["deny"] == ["example", "other"]   # 并入既有条目而不是新建
    assert len(migrated) == 1 and pending == []
    assert migrate_legacy_scope_exclusions(migrated_groups, migrated) == (migrated_groups, migrated, [])   # 幂等


def test_migration_creates_entry_for_other_member_and_keeps_all_mode_group_exclusion():
    groups = normalize_administrator_groups([group(mode="selected", excluded=["example"])], [platform()])
    existing = normalize_administrator_overrides([override(user="user-beta", deny=["other"])], [platform()])
    _, migrated, pending = migrate_legacy_scope_exclusions(groups, existing)
    assert len(migrated) == 2 and sorted(item["deny"] for item in migrated) == [["example"], ["other"]]
    assert pending == []
    service = AdministratorService(lambda: [platform()], [group(mode="all", included=[], excluded=["example"])])
    assert not service.resolve(origin(), "example").allowed
    assert service.snapshot()[1] == []   # all 模式排除留在组内, 迁移只处理指定插件模式


def test_migration_on_read_path_keeps_effective_denial():
    service = AdministratorService(lambda: [platform()], [group(mode="selected", excluded=["example"])])
    groups, overrides, _ = service.snapshot()
    assert groups[0]["plugin_scope"]["excluded"] == []
    assert overrides[0]["deny"] == ["example"] and overrides[0]["protect"] is False
    assert not service.resolve(origin(), "example").allowed   # 迁移后有效行为不变


def test_revision_covers_both_sections_and_snapshot_is_atomic():
    service = AdministratorService(lambda: [platform()], [group()])
    _, _, before = service.snapshot()
    service.apply([group()], [override(allow=["example"])])
    groups, overrides, after = service.snapshot()
    assert after != before and len(groups) == 1 and len(overrides) == 1
    service.apply([group()], [override(allow=["other"])])
    assert service.snapshot()[2] != after


def test_migration_tolerates_legacy_entries_without_protect_field():
    legacy = group()
    assert "protect" not in legacy
    normalized = normalize_administrator_groups([legacy], [platform()])
    assert normalized[0]["protect"] is True   # 缺省保持现行全保护行为
    service = AdministratorService(lambda: [platform()], [legacy])
    assert service.protected_users("qq") == ["user-alpha"]


def test_migration_of_disabled_group_captures_denial_without_changing_behavior():
    # 源组停用 → 条目 enabled=False, 否决惰性; 迁移前后该成员都允许
    service = AdministratorService(lambda: [platform()], [group("allow"), group("off", enabled=False, excluded=["example"])])
    groups, overrides, _ = service.snapshot()
    assert [item["enabled"] for item in overrides] == [False]
    assert overrides[0]["deny"] == ["example"]
    assert groups[1]["plugin_scope"]["excluded"] == []
    assert service.resolve(origin(), "example").allowed


def test_migration_skips_group_when_existing_entry_enabled_state_conflicts():
    conflicting = override(user="user-alpha", deny=["other"], enabled=False)
    service = AdministratorService(lambda: [platform()], [group(excluded=["example"])], [conflicting])
    groups, overrides, _ = service.snapshot()
    assert groups[0]["plugin_scope"]["excluded"] == ["example"]   # 旧排除原样保留, 行为零变化
    assert overrides[0]["deny"] == ["other"] and overrides[0]["enabled"] is False
    assert not service.resolve(origin(), "example").allowed       # 旧规则仍生效


def test_migration_merges_when_enabled_state_matches():
    matching = override(user="user-alpha", deny=["other"], enabled=True)
    service = AdministratorService(lambda: [platform()], [group(excluded=["example"])], [matching])
    groups, overrides, _ = service.snapshot()
    assert groups[0]["plugin_scope"]["excluded"] == []
    assert overrides[0]["deny"] == ["other", "example"] and overrides[0]["enabled"] is True


def test_migration_pending_resolves_after_entry_enabled_state_is_adjusted():
    conflicting = override(user="user-alpha", deny=["other"], enabled=False)
    service = AdministratorService(lambda: [platform()], [group(excluded=["example"])], [conflicting])
    assert service.snapshot()[0][0]["plugin_scope"]["excluded"] == ["example"]
    service.apply(service.snapshot()[0], [dict(service.snapshot()[1][0], enabled=True)])
    groups, overrides, _ = service.snapshot()
    assert groups[0]["plugin_scope"]["excluded"] == []            # 启用状态对齐后下次读取即完成迁移
    assert overrides[0]["deny"] == ["other", "example"]


def test_migration_generates_unique_entry_id_when_digest_collides():
    digest = hashlib.sha1(f"qq\0user-alpha".encode("utf-8")).hexdigest()[:16]
    collision = override(identity=f"legacy-{digest}", user="user-beta", deny=["other"])
    groups = normalize_administrator_groups([group(excluded=["example"])], [platform()])
    overrides = normalize_administrator_overrides([collision], [platform()])
    _, migrated, _ = migrate_legacy_scope_exclusions(groups, overrides)
    assert len({item["id"] for item in migrated}) == len(migrated)   # ID 不冲突
    normalize_administrator_overrides(migrated, [platform()])        # 链式再归一化不报错


def test_strict_scope_accepts_saved_excluded_subset_only():
    saved = normalize_administrator_groups([group(excluded=["example", "other"])], [platform()])
    intact = normalize_administrator_groups([group(excluded=["example", "other"])], [platform()], previous=saved, strict_scope=True)
    assert intact[0]["plugin_scope"]["excluded"] == ["example", "other"]   # 原样往返
    trimmed = normalize_administrator_groups([group(excluded=["example"])], [platform()], previous=saved, strict_scope=True)
    assert trimmed[0]["plugin_scope"]["excluded"] == ["example"]           # 逐项收敛


def test_strict_scope_rejects_added_exclusion_and_names_it():
    saved = normalize_administrator_groups([group(excluded=["example"])], [platform()])
    with pytest.raises(ValueError, match=r"不能新增排除插件: \['other'\]"):
        normalize_administrator_groups([group(excluded=["example", "other"])], [platform()], previous=saved, strict_scope=True)


def test_strict_scope_treats_new_or_renamed_group_as_empty_previous():
    saved = normalize_administrator_groups([group(excluded=["example"])], [platform()])
    with pytest.raises(ValueError, match="不能新增排除插件"):
        normalize_administrator_groups([group("fresh", excluded=["example"])], [platform()], previous=saved, strict_scope=True)


def test_migration_skipped_group_round_trips_and_stays_effective():
    conflicting = override(user="user-alpha", deny=["other"], enabled=False)
    service = AdministratorService(lambda: [platform()], [group(excluded=["example"])], [conflicting])
    groups, overrides, _ = service.snapshot()
    # 待迁移组带着旧排除原样回存必须放行, 且语义不变 (规则6 解除死锁)
    round_tripped = normalize_administrator_groups(groups, [platform()], previous=groups, strict_scope=True)
    assert round_tripped[0]["plugin_scope"]["excluded"] == ["example"]
    service.apply(round_tripped, overrides)
    assert service.snapshot()[0][0]["plugin_scope"]["excluded"] == ["example"]
    assert not service.resolve(origin(), "example").allowed


def test_protection_requires_enabled_instance_matched_protect_in_same_entry():
    disabled_protect = AdministratorService(lambda: [platform()], [group("off", enabled=False, protect=True)], [])
    assert disabled_protect.protected_users("qq") == []
    leaked = AdministratorService(lambda: [platform()], [group("p", protect=True, enabled=False), group("np", protect=False)], [])
    assert leaked.protected_users("qq") == []
    enabled_protect = AdministratorService(lambda: [platform()], [group(protect=True)], [])
    assert enabled_protect.protected_users("qq") == ["user-alpha"]
    disabled_override = AdministratorService(lambda: [platform()], [], [override(protect=True, enabled=False)])
    assert disabled_override.protected_users("qq") == []


def test_protection_ignores_rebuilt_platform_instance():
    platforms = [platform()]
    service = AdministratorService(lambda: platforms, [group(protect=True), group("o", user="user-beta", protect=True)], [])
    assert sorted(service.protected_users("qq")) == ["user-alpha", "user-beta"]
    platforms[0] = platform(instance="replacement")
    assert service.protected_users("qq") == []
