"""集中契约、领域实现与公开 facade 共享同一组对象。"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("contract", "domain", "names"),
    [
        ("flow.ports", "satrap.edictum.flow", ("FlowMessage", "ContextBlock", "StateView", "FlowNode")),
        ("flow.definition", "satrap.edictum.flow.definition", ("FlowDefinition", "FlowDefinitionError", "ResolvedNodeConfig")),
        ("flow.definition", "satrap.edictum.flow.plugin_nodes", ("FlowContractError",)),
        ("flow.registry", "satrap.edictum.flow.registry", ("PortSpec", "RenamedEntry", "FlowNodeType")),
        ("flow.workflow", "satrap.edictum.flow.plugin_nodes", ("FlowWorkflowDecl",)),
        ("flow.events", "satrap.edictum.flow.engine", ("FlowEvent", "EventContext", "ConditionResult")),
        ("flow.validation", "satrap.edictum.flow.validate", ("Issue", "Origin", "WfInfo", "Analysis")),
        ("plugin.config", "satrap.edictum.plugin.config", ("ConfigField", "PluginHostState", "schema_to_payload")),
        ("plugin.compatibility", "satrap.edictum.plugin.compatibility", ("PluginEnvironment", "CompatibilityResult", "PluginCompatibilityError")),
        ("plugin.permissions", "satrap.edictum.plugin.permissions", ("ManagementPermission", "CommandPermissions", "PluginPermissions", "EMPTY_PERMISSIONS")),
        ("plugin.catalog", "satrap.edictum.plugin.catalog", ("PluginCatalogEntry",)),
        ("plugin.spec", "satrap.edictum.plugin.spec", ("PluginSpec", "PluginConfigResolver")),
    ],
)
def test_shared_identity(contract: str, domain: str, names: tuple[str, ...]) -> None:
    source = importlib.import_module("satrap.core.type." + contract)
    destination = importlib.import_module(domain)
    for name in names:
        assert getattr(source, name) is getattr(destination, name)


def test_facade_resources_and_literal_identity() -> None:
    import satrap.edictum.flow as flow
    from satrap.core.type.flow.ports import EventContextKind
    from satrap.core.type.flow.registry import EventContext
    from satrap.edictum.flow.node import node
    from satrap.edictum.flow.resources import FLOW_CONDITION_WORKERS

    assert flow.node is node
    assert flow.FLOW_CONDITION_WORKERS is FLOW_CONDITION_WORKERS
    assert EventContext is EventContextKind


def test_catalog_defaults_copy_and_config_fallback() -> None:
    from dataclasses import FrozenInstanceError
    from satrap.core.type.plugin.catalog import PluginCatalogEntry
    from satrap.core.type.plugin.config import ConfigField
    from satrap.core.type.plugin.permissions import EMPTY_PERMISSIONS

    first = PluginCatalogEntry("one", Path("one"), interop={"nested": {"value": 1}})
    second = PluginCatalogEntry("two", Path("two"))
    assert first.flow_workflow is not second.flow_workflow
    assert first.permissions is second.permissions is EMPTY_PERMISSIONS
    payload = first.to_payload()
    payload["interop"]["nested"]["value"] = 2
    assert first.interop["nested"]["value"] == 1
    with pytest.raises(FrozenInstanceError):
        first.name = "changed"
    assert ConfigField("flag", type="bool", default=True).validate("unrecognized") is True
    assert ConfigField("number", type="number", default=7).validate("bad") == 7


@pytest.mark.parametrize("module", [
    "satrap.core.type.flow.ports", "satrap.core.type.flow.definition",
    "satrap.core.type.flow.registry", "satrap.core.type.flow.workflow",
    "satrap.core.type.flow.events", "satrap.core.type.flow.validation",
    "satrap.core.type.plugin.config", "satrap.core.type.plugin.compatibility",
    "satrap.core.type.plugin.permissions", "satrap.core.type.plugin.catalog",
    "satrap.core.type.plugin.spec", "satrap.edictum.flow", "satrap.edictum",
])
def test_fresh_process_import(module: str) -> None:
    subprocess.run([sys.executable, "-c", "import importlib,sys; importlib.import_module(sys.argv[1])", module], check=True, timeout=30)


def test_old_flow_types_module_removed() -> None:
    assert importlib.util.find_spec("satrap.edictum.flow.types") is None
