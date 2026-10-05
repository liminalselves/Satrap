from copy import deepcopy
import pytest

from satrap.core.call_context import CallOrigin
from satrap.core.config.administrator_groups import AdministratorService, normalize_administrator_groups
from satrap.core.config.platform_identity import platform_instance_id


def platform(identity="qq", instance="instance-1"):
    return {"id": identity, "type": "future-adapter", "instance_id": instance}


def group(identity="admins", user="user-alpha", *, mode="selected", included=None, excluded=None, enabled=True):
    return {"id": identity, "name": "管理员", "enabled": enabled,
            "members": [{"platform_id": "qq", "platform_instance_id": "instance-1", "user_id": user}],
            "plugin_scope": {"mode": mode, "included": ["example"] if included is None else included, "excluded": excluded or []}}


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
    groups = [group(), group("all", mode="all", included=[]), group("deny", excluded=["example"]),
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
    saved, revision = service.snapshot()
    saved[0]["enabled"] = False
    assert service.resolve(origin(), "example").allowed
    service.apply(saved)
    assert not service.resolve(origin(), "example").allowed
    assert service.snapshot()[1] != revision
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
