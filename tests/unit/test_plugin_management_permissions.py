from copy import deepcopy
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace
from collections.abc import Mapping
from pathlib import Path
from typing import Any
import weakref
import json
import os
import pytest

from satrap.core.call_context import CallOrigin, bind_call_origin, bind_tool_workflow
from satrap.core.config.administrator_groups import AdministratorService
from satrap.core.plugin_authorization import (
    AuthorizationDecision, PluginEntryBinding, authorize_plugin_entry, bind_authorization_step,
    bind_native_command, bind_plugin_factory_tools, evaluate_plugin_permissions,
)
from satrap.core.utils.TCBuilder.manager import ToolsManager
from satrap.core.utils.TCBuilder.tool import Tool
from satrap.edictum.plugin_config import parse_config_schema
from satrap.edictum.plugin_permissions import PluginPermissions, parse_plugin_permissions, validate_permission_install


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


def override_administrators(allow=("example",), user="carol"):
    overrides = [{"id": "solo", "enabled": True, "protect": False, "platform_id": "p", "platform_instance_id": "one",
                  "user_id": user, "allow": list(allow), "deny": []}]
    return AdministratorService(lambda: [{"id": "p", "instance_id": "one"}], [], overrides)


def _write_grant(result):
    """取 write 权限对应的授权条目, 避免 read 的空名单允许规则干扰断言"""
    return next(item for item in result.grants if item.permission == "write")


def test_override_only_grant_is_labelled_as_administrator_override():
    result = evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin("carol"), override_administrators())
    assert result.status == "allowed"
    grant = _write_grant(result)
    assert grant.source == "administrator_override" and grant.override_ids == ("solo",) and grant.group_ids == ()
    assert evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin("alice"), override_administrators()).status == "denied"


def test_group_grant_records_group_source_and_fingerprint_distinguishes_layers():
    group_result = evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin("alice"), administrators())
    override_result = evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin("carol"), override_administrators())
    grant = _write_grant(group_result)
    assert grant.source == "administrator_group" and grant.group_ids == ("a",) and grant.override_ids == ()
    assert group_result.status == override_result.status == "allowed"
    assert group_result.permission_fingerprint != override_result.permission_fingerprint


def test_both_layers_allow_together_reports_group_source():
    service = AdministratorService(lambda: [{"id": "p", "instance_id": "one"}],
        [{"id": "a", "name": "管理员", "enabled": True,
          "members": [{"platform_id": "p", "user_id": "alice", "platform_instance_id": "one"}],
          "plugin_scope": {"mode": "selected", "included": ["example"], "excluded": []}}],
        [{"id": "solo", "enabled": True, "protect": False, "platform_id": "p", "platform_instance_id": "one",
          "user_id": "alice", "allow": ["example"], "deny": []}])
    result = evaluate_plugin_permissions("example", spec(), "tools", "write", {}, origin("alice"), service)
    assert result.status == "allowed"
    grant = _write_grant(result)
    assert grant.source == "administrator_group" and grant.group_ids == ("a",)
    assert grant.override_ids == ("solo",)   # 例外来源仍记录在归属里


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
    groups, overrides, _ = service.snapshot()
    groups[0]["name"] = "重命名"
    service.apply(groups, overrides)
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
    from satrap.core.plugin_authorization import PluginPermissionDenied, require_plugin_entry_permission

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
    assert result.status == "denied" and result.reason_code == "authorization_error"
    assert logged and "authorization fault injection" in logged[0]
    with pytest.raises(PluginPermissionDenied) as caught:
        require_plugin_entry_permission(binding)
    assert caught.value.code == "authorization_error"
    assert str(caught.value) == "example 管理入口权限检查暂时失败, 请查看后端日志 (authorization_error)"


def test_structural_bad_config_still_reports_invalid_config():
    result = evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": 123}, origin(), administrators())
    assert result.status == "denied" and result.reason_code == "invalid_permission_config"
    denied = evaluate_plugin_permissions("example", spec(), "tools", "write", {"writers": "other"}, origin(), administrators(excluded=["example"]))
    assert denied.status == "denied" and denied.reason_code == "permission_denied"


class _EnabledEntry:
    """入口绑定需要的常驻工具替身"""

    def is_enabled(self) -> bool:
        return True


def tool_enabled(name: str) -> bool:
    """
    模拟仍启用的注册工具

    参数:
    - name: 工具注册名

    返回:
    - 恒为 True
    """
    return True


_ENTRY = _EnabledEntry()


def tool_binding() -> PluginEntryBinding:
    """
    构造普通工具入口

    返回:
    - 指向常驻工具替身的工具绑定
    """
    return PluginEntryBinding("example", "tools", "query", spec(), {}, weakref.ref(_ENTRY))


def counted_evaluations(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """
    记录每次真实权限判定的入口与子命令

    参数:
    - monkeypatch: pytest 补丁工具

    返回:
    - 依次追加 (入口名, 子命令) 的列表
    """
    from satrap.core import plugin_authorization as authorization

    recorded: list[tuple[str, str | None]] = []
    real = authorization.evaluate_plugin_permissions

    def counted(plugin_name: str, spec_value: PluginPermissions, kind: str, name: str, config: Mapping[str, Any],
                call_origin: CallOrigin | None, administrators: AdministratorService | None = None,
                *, subcommand: str | None = None) -> AuthorizationDecision:
        recorded.append((name, subcommand))
        return real(plugin_name, spec_value, kind, name, config, call_origin, administrators, subcommand=subcommand)

    monkeypatch.setattr(authorization, "evaluate_plugin_permissions", counted)
    return recorded


def test_entry_decision_is_evaluated_once_inside_one_step(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    binding = tool_binding()
    with bind_call_origin(origin()), bind_authorization_step():
        first = authorize_plugin_entry(binding)
        second = authorize_plugin_entry(binding)
    assert first is second and first.status == "allowed"
    assert evaluations == [("query", None)]


def test_entry_decision_is_not_reused_outside_its_step(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    binding = tool_binding()
    with bind_call_origin(origin()):
        first = authorize_plugin_entry(binding)
        with bind_authorization_step():
            second = authorize_plugin_entry(binding)
        third = authorize_plugin_entry(binding)
    assert first is not second and second is not third
    assert all(decision.status == "allowed" for decision in (first, second, third))
    assert evaluations == [("query", None), ("query", None), ("query", None)]


def test_entry_decision_is_re_evaluated_when_revoked_between_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    class Entry:
        def is_enabled(self) -> bool:
            return True
    entry = Entry()
    installed = SimpleNamespace(name="example", enabled=True, tools={"query": True})

    class Session:
        def __init__(self) -> None:
            self._wf = SimpleNamespace(tools_manager=SimpleNamespace(tools={"query": entry}, is_tool_enabled=tool_enabled))

        def list_plugins(self) -> list[SimpleNamespace]:
            return [installed]

    session = Session()
    evaluations = counted_evaluations(monkeypatch)
    binding = PluginEntryBinding("example", "tools", "query", spec(), {}, weakref.ref(entry), weakref.ref(session))
    with bind_call_origin(origin()):
        with bind_authorization_step():
            assert authorize_plugin_entry(binding).status == "allowed"
            assert authorize_plugin_entry(binding).status == "allowed"
        installed.tools["query"] = False
        with bind_authorization_step():
            assert authorize_plugin_entry(binding).status == "denied"
    assert evaluations == [("query", None)]


def test_nested_step_scope_shares_the_outer_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    binding = tool_binding()
    with bind_call_origin(origin()):
        with bind_authorization_step():
            outer = authorize_plugin_entry(binding)
            with bind_authorization_step():
                inner = authorize_plugin_entry(binding)
        after = authorize_plugin_entry(binding)
    assert inner is outer
    assert after is not outer and after.status == "allowed"
    assert evaluations == [("query", None), ("query", None)]


def test_copied_step_context_cannot_reuse_revoked_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    binding = tool_binding()
    with bind_call_origin(origin()):
        with bind_authorization_step():
            assert authorize_plugin_entry(binding).status == "allowed"
            copied = copy_context()
        # 步骤结束后身份仍存活: 复制出去的上下文不得沿用已失效作用域的判定
        assert copied.run(authorize_plugin_entry, binding).status == "allowed"
    decision = copied.run(authorize_plugin_entry, binding)
    assert decision.status == "denied" and decision.reason_code == "identity_missing"
    assert evaluations == [("query", None), ("query", None), ("query", None)]


def test_step_cache_does_not_cross_call_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    binding = tool_binding()
    with bind_call_origin(origin()), bind_authorization_step():
        assert authorize_plugin_entry(binding).status == "allowed"
        with bind_call_origin(None):
            masked = authorize_plugin_entry(binding)
    assert masked.status == "denied" and masked.reason_code == "identity_missing"
    assert evaluations == [("query", None), ("query", None)]


class _AuthorizedTool(Tool):
    """记录每次授权判定结果的工具替身"""

    def __init__(self, binding: PluginEntryBinding, seen: list[AuthorizationDecision]) -> None:
        super().__init__(tool_name="query", description="读取", params_dict={})
        self._plugin_entry_binding = binding
        self._seen = seen

    def is_available_for_call(self) -> bool:
        self._seen.append(authorize_plugin_entry(self._plugin_entry_binding))
        return True

    def execute(self) -> str:
        return "ok"


def test_definition_list_is_stable_and_does_not_evaluate_caller_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    seen: list[AuthorizationDecision] = []
    manager = ToolsManager()
    manager.register_tool(_AuthorizedTool(tool_binding(), seen))
    with bind_call_origin(origin()):
        definitions = manager.get_tools_definitions()
    assert [item["function"]["name"] for item in definitions] == ["query"]
    with bind_call_origin(origin("bob")):
        assert manager.get_tools_definitions() == definitions
    assert evaluations == [] and seen == []


def test_execution_shares_one_authorization_decision_within_each_step(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluations = counted_evaluations(monkeypatch)
    seen: list[AuthorizationDecision] = []
    manager = ToolsManager()
    manager.register_tool(_AuthorizedTool(tool_binding(), seen))
    with bind_call_origin(origin()):
        assert manager.execute_tool("query", {}) == "ok"
        assert manager.execute_tool("query", {}) == "ok"
    assert evaluations == [("query", None), ("query", None)]
    assert [decision.status for decision in seen] == ["allowed", "allowed"]
    assert seen[0] is not seen[1]


class _FactoryTool:
    """工厂绑定需要的工具替身, 支持弱引用"""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self._plugin_entry_binding: PluginEntryBinding | None = None

    def get_tool_name(self) -> str:
        return "query"


def _factory_plugin(plugin_dir: Path, name: str = "example") -> Path:
    """
    写出工厂插件目录的 meta.yaml

    参数:
    - plugin_dir: 插件目录
    - name: 插件名

    返回:
    - 写出的 meta.yaml 路径
    """
    plugin_dir.mkdir(parents=True, exist_ok=True)
    meta_path = plugin_dir / "meta.yaml"
    # YAML 是 JSON 的超集, 这里直接用 JSON 写出同一份声明
    meta_path.write_text(json.dumps({"name": name, **metadata()}, ensure_ascii=False), encoding="utf-8")
    return meta_path


def _counted_meta_loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    记录每次真实的 meta.yaml 读取

    参数:
    - monkeypatch: pytest 补丁工具

    返回:
    - 依次追加插件目录的列表
    """
    from satrap.core import plugin_authorization as authorization
    from satrap.edictum import plugin as plugin_module

    monkeypatch.setattr(authorization, "_FACTORY_DECLARATIONS", {})
    recorded: list[str] = []
    real = plugin_module.load_plugin_meta

    def counted(plugin_directory: Path) -> dict[str, Any]:
        recorded.append(str(plugin_directory))
        return real(plugin_directory)

    monkeypatch.setattr(plugin_module, "load_plugin_meta", counted)
    return recorded


def test_factory_declarations_reuse_unchanged_meta_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loads = _counted_meta_loads(monkeypatch)
    plugin_dir = tmp_path / "example"
    _factory_plugin(plugin_dir)
    first, second = _FactoryTool(), _FactoryTool()
    bind_plugin_factory_tools([first], str(plugin_dir / "tools.py"))
    bind_plugin_factory_tools([second], str(plugin_dir / "tools.py"))
    assert loads == [str(plugin_dir)]
    first_binding, second_binding = first._plugin_entry_binding, second._plugin_entry_binding
    assert first_binding is not None and second_binding is not None
    assert first_binding.plugin_name == "example"
    assert first_binding.permissions is second_binding.permissions


def test_factory_declarations_refresh_after_meta_yaml_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loads = _counted_meta_loads(monkeypatch)
    plugin_dir = tmp_path / "example"
    meta_path = _factory_plugin(plugin_dir)
    before = meta_path.stat()
    bind_plugin_factory_tools([_FactoryTool()], str(plugin_dir / "tools.py"))
    os.utime(meta_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    touched = _FactoryTool()
    bind_plugin_factory_tools([touched], str(plugin_dir / "tools.py"))
    _factory_plugin(plugin_dir, name="renamed")
    refreshed = _FactoryTool()
    bind_plugin_factory_tools([refreshed], str(plugin_dir / "tools.py"))
    assert loads == [str(plugin_dir)] * 3
    touched_binding, refreshed_binding = touched._plugin_entry_binding, refreshed._plugin_entry_binding
    assert touched_binding is not None and refreshed_binding is not None
    assert touched_binding.plugin_name == "example"
    assert refreshed_binding.plugin_name == "renamed"


def test_factory_declarations_do_not_survive_missing_meta_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loads = _counted_meta_loads(monkeypatch)
    plugin_dir = tmp_path / "example"
    meta_path = _factory_plugin(plugin_dir)
    bind_plugin_factory_tools([_FactoryTool()], str(plugin_dir / "tools.py"))
    meta_path.unlink()
    with pytest.raises(ValueError, match="meta.yaml"):
        bind_plugin_factory_tools([_FactoryTool()], str(plugin_dir / "tools.py"))
    # 文件消失后仍走真实读取并抛出既有错误, 不返回缓存声明
    assert loads == [str(plugin_dir)] * 2


def test_factory_declaration_cache_stays_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from satrap.core import plugin_authorization as authorization

    _counted_meta_loads(monkeypatch)
    monkeypatch.setattr(authorization, "_FACTORY_DECLARATIONS_LIMIT", 1)
    for name in ("first", "second"):
        plugin_dir = tmp_path / name
        _factory_plugin(plugin_dir, name=name)
        bind_plugin_factory_tools([_FactoryTool()], str(plugin_dir / "tools.py"))
    assert list(authorization._FACTORY_DECLARATIONS) == [str(tmp_path / "second" / "meta.yaml")]
