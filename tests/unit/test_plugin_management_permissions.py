from copy import deepcopy
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace
import weakref
import pytest

from satrap.core.call_context import CallOrigin, bind_call_origin, bind_tool_workflow
from satrap.core.config.administrator_groups import AdministratorService
from satrap.core.plugin_authorization import (
    PluginEntryBinding, authorize_plugin_entry, bind_native_command, evaluate_plugin_permissions,
)
from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin_permissions import parse_plugin_permissions, validate_permission_install


def metadata():
    return {"permission_schema_version": 1,
            "config_schema": {"readers": {"type": "textarea"}, "writers": {"type": "textarea"}},
            "tools": {"query": "读取", "write": "修改", "ordinary": "普通"}, "commands": {"manage": "管理"},
            "management_permissions": {
                "read": {"description": "查询", "caller_list": "readers", "empty_policy": "allow", "system_admin": True},
                "write": {"description": "修改", "caller_list": "writers", "empty_policy": "deny", "system_admin": True}},
            "tool_permissions": {"query": ["read"], "write": ["read", "write"]},
            "command_permissions": {"manage": {"default": ["read"], "subcommands": {"approve": ["write"]}}}}


def spec():
    meta = metadata()
    return parse_plugin_permissions(meta, parse_config_schema(meta))


def origin(user="alice"):
    return CallOrigin("p", "bot", "GroupMessage", "room", user, "message", "request")


def administrators(*, excluded=()):
    groups = [{"id": "a", "name": "管理员", "enabled": True,
               "members": [{"platform_id": "p", "user_id": "alice", "platform_instance_id": "one"}],
               "plugin_scope": {"mode": "selected", "included": ["example"], "excluded": list(excluded)}}]
    return AdministratorService(lambda: [{"id": "p", "instance_id": "one"}], groups)


def test_legacy_and_ordinary_entries_do_not_receive_management_grants():
    assert not parse_plugin_permissions({}, {}).supports_administrators
    assert evaluate_plugin_permissions("example", spec(), "tools", "ordinary", {}, None).status == "not_applicable"


def test_empty_rules_and_permission_and_are_preserved():
    assert evaluate_plugin_permissions("example", spec(), "tools", "query", {}, origin()).status == "allowed"
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin()).status == "denied"
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": "alice", "readers": "bob"}, origin()).status == "denied"
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": "alice"}, origin()).status == "allowed"


def test_administrators_fill_multiple_gates_without_changing_config():
    config = {"readers": "bob", "writers": ""}
    before = deepcopy(config)
    result = evaluate_plugin_permissions("example", spec(), "tools", "write", config, origin(), administrators())
    assert result.status == "allowed" and all(item.source == "administrator_group" for item in result.grants)
    assert config == before
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", config, origin("bob"), administrators()).status == "denied"


def test_exclusion_preserves_independent_local_permissions_and_revision_is_scoped():
    service = administrators(excluded=["example"])
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin(), service).status == "denied"
    result = evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": "alice"}, origin(), service)
    assert result.status == "allowed" and result.grants[-1].source == "local_list"
    groups, _ = service.snapshot()
    groups[0]["name"] = "重命名"
    service.apply(groups)
    updated = evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": "alice"}, origin(), service)
    assert result.policy_revision != updated.policy_revision and result.permission_fingerprint == updated.permission_fingerprint


def test_invalid_config_and_non_platform_identity_cannot_use_admin_fallback():
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": 123}, origin(), administrators()).status == "denied"
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {}, replace(origin(), actor_kind="model"), administrators()).status == "denied"


def test_permissions_may_explicitly_reject_system_admin_grants():
    meta = metadata()
    meta["management_permissions"]["write"]["system_admin"] = False
    rules = parse_plugin_permissions(meta, parse_config_schema(meta))
    assert evaluate_plugin_permissions("example", rules, "tools", "write", {}, origin(), administrators()).status == "denied"


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(permission_schema_version=True),
    lambda m: m["tool_permissions"].update(write=["missing"]),
    lambda m: m["tool_permissions"].update(missing=["write"]),
    lambda m: m["tool_permissions"].update(write=["write", "write"]),
    lambda m: m["management_permissions"]["write"].update(caller_list="missing"),
    lambda m: m["management_permissions"]["write"].update(empty_policy="unknown"),
    lambda m: m["management_permissions"]["write"].update(empty_policy=[]),
    lambda m: m["management_permissions"]["write"].update(system_admin="true"),
    lambda m: m["management_permissions"]["write"].update(unknown=True),
    lambda m: m["management_permissions"]["write"].update(requirements=[True]),
])
def test_invalid_metadata_is_rejected(mutate):
    meta = metadata()
    mutate(meta)
    with pytest.raises(ValueError):
        parse_plugin_permissions(meta, parse_config_schema(meta))


def test_actual_install_validates_subcommands_and_not_prefix_matches():
    def handler(*args):
        return args
    handler.subcommands = ["approve"]
    validate_permission_install(spec(), {"query": object(), "write": object()}, {"manage": handler})
    assert spec().required("commands", "manage", "approve") == ("read", "write")
    assert spec().required("commands", "manage", "approve-all") == ("read",)
    handler.subcommands = []
    with pytest.raises(ValueError):
        validate_permission_install(spec(), {"query": object(), "write": object()}, {"manage": handler})


def test_command_requires_native_scope_and_rejects_model_tool_workflow():
    def handler(*args):
        return args
    binding = PluginEntryBinding("example", "commands", "manage", spec(), {"writers": "alice"}, weakref.ref(handler))
    with bind_call_origin(origin()):
        assert authorize_plugin_entry(binding, subcommand="approve").status == "denied"
        with bind_native_command(binding):
            assert authorize_plugin_entry(binding, subcommand="approve").status == "allowed"
            with bind_tool_workflow(object()):
                assert authorize_plugin_entry(binding, subcommand="approve").status == "denied"


def test_revoked_installed_entry_cannot_reuse_its_binding():
    class Entry:
        def is_enabled(self):
            return True
    entry = Entry()
    installed = SimpleNamespace(name="example", enabled=True, tools={"write": True})
    class Session:
        def __init__(self):
            self._wf = SimpleNamespace(tools_manager=SimpleNamespace(tools={"write": entry}, is_tool_enabled=lambda _: True))
        def list_plugins(self):
            return [installed]
    session = Session()
    binding = PluginEntryBinding("example", "tools", "write", spec(), {"writers": "alice"}, weakref.ref(entry), weakref.ref(session))
    with bind_call_origin(origin()):
        assert authorize_plugin_entry(binding).status == "allowed"
        installed.tools["write"] = False
        assert authorize_plugin_entry(binding).status == "denied"


def test_copied_native_command_context_is_revoked_after_dispatch():
    def handler(*args):
        return args
    binding = PluginEntryBinding("example", "commands", "manage", spec(), {"writers": "alice"}, weakref.ref(handler))
    with bind_call_origin(origin()):
        with bind_native_command(binding):
            copied = copy_context()
            assert copied.run(authorize_plugin_entry, binding, subcommand="approve").status == "allowed"
        assert copied.run(authorize_plugin_entry, binding, subcommand="approve").status == "denied"


def test_business_requirement_descriptions_are_visible_but_never_grant_permission():
    meta = metadata()
    meta["management_permissions"]["write"]["requirements"] = ["需开启功能并等待审批"]
    rules = parse_plugin_permissions(meta, parse_config_schema(meta))
    assert rules.to_payload()["management_permissions"]["write"]["requirements"] == ["需开启功能并等待审批"]
    assert evaluate_plugin_permissions("example", rules, "tools", "write", {}, origin()).status == "denied"


@pytest.mark.parametrize("kind, name", [("tools", "unregistered"), ("commands", "unregistered"), ("unknown", "write")])
def test_unknown_entries_cannot_be_treated_as_ordinary(kind, name):
    result = evaluate_plugin_permissions("example", spec(), kind, name, {}, origin(), administrators())
    assert result.status == "denied" and result.reason_code == "unknown_entry"


def test_authorization_exception_is_logged_and_denied(monkeypatch):
    import satrap.core.platform as platform
    from satrap.core import plugin_authorization as authorization

    class Entry:
        def is_enabled(self):
            return True

    def broken_manager():
        raise RuntimeError("authorization fault injection")

    logged = []
    entry = Entry()
    binding = PluginEntryBinding("example", "tools", "write", spec(), {}, weakref.ref(entry))
    monkeypatch.setattr(platform, "current_adapter_manager", broken_manager)
    monkeypatch.setattr(authorization.logger, "error", logged.append)
    result = authorize_plugin_entry(binding)
    assert result.status == "denied" and result.reason_code == "invalid_permission_config"
    assert logged and "authorization fault injection" in logged[0]
