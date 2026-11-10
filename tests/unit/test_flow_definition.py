"""流程定义解析, 配置合并与版本迁移"""
from __future__ import annotations

import pytest

from satrap.edictum.flow.definition import (
    FlowDefinitionError,
    FlowDefaultsEntry,
    migrate_flow_definition,
    parse_flow_definition,
    resolve_node_config,
    validate_node_config,
)
from satrap.edictum.flow.registry import FlowNodeRegistry, FlowNodeType, PortSpec, RenamedEntry
from satrap.edictum.plugin_config import ConfigField


def _registry_with_filter() -> FlowNodeRegistry:
    reg = FlowNodeRegistry()
    reg.register_plugin_nodes("keyword_filter", [FlowNodeType(
        type="keyword_filter.filter", version=2, label="过滤", description="", role="transform",
        inputs=(PortSpec(name="messages", types=("messages",), required=True),),
        outputs=(PortSpec(name="kept", types=("messages",)),),
        config_schema={"blocked_words": ConfigField("blocked_words", "textarea", "")},
        renamed={2: RenamedEntry(fields={"words": "blocked_words"}, ports={"in": "messages"})},
        plugin="keyword_filter",
    )])
    return reg


def test_parse_minimal_flow():
    """节点/连线/默认/布局/预算都解析"""
    d = parse_flow_definition({
        "version": 3,
        "defaults": {"react_workflow": {"version": 1, "config": {"model_name": "m1"}}},
        "nodes": [
            {"id": "s", "type": "source", "title": "消息", "config": {}},
            {"id": "w", "type": "react_workflow", "title": "WF", "config": {"prompt": "hi"}},
        ],
        "edges": [{"from": "s.out", "to": "w.input", "label": "数据"}],
        "layout": {"s": {"x": 1, "y": 2}},
        "budget": {"max_calls": 200, "window_minutes": 60, "count": "all"},
    })
    assert d.version == 3 and len(d.nodes) == 2 and len(d.edges) == 1
    assert d.defaults["react_workflow"].config == {"model_name": "m1"}
    assert d.edges[0].label == "数据" and d.edges[0].source_port == "out"
    assert d.layout["s"] == {"x": 1.0, "y": 2.0}
    assert d.budget and d.budget.max_calls == 200


def test_parse_rejects_bad_edges_and_duplicates():
    """重复 id, 悬空连线, 非法端点都报对应错误"""
    with pytest.raises(FlowDefinitionError) as e1:
        parse_flow_definition({"version": 3, "nodes": [{"id": "a", "type": "source"}, {"id": "a", "type": "source"}], "edges": []})
    assert e1.value.code == "flow_duplicate_id"
    with pytest.raises(FlowDefinitionError) as e2:
        parse_flow_definition({"version": 3, "nodes": [{"id": "a", "type": "source"}], "edges": [{"from": "a.out", "to": "ghost.in"}]})
    assert e2.value.code == "flow_edge_invalid"
    with pytest.raises(FlowDefinitionError) as e3:
        parse_flow_definition({"version": 3, "nodes": [{"id": "a", "type": "source"}], "edges": [{"from": "aout", "to": "a.out"}]})
    assert e3.value.code == "flow_edge_invalid"


def test_resolve_node_config_layers():
    """类型默认 < 插件全局 < 流程默认 < 本节点, 每层来源可追踪"""
    reg = FlowNodeRegistry()
    node = reg.get("cond_time")
    inst = parse_flow_definition({"version": 3, "nodes": [
        {"id": "t", "type": "cond_time", "title": "t", "config": {"end": "23:00"}},
    ], "edges": []}).nodes["t"]
    resolved = resolve_node_config(
        node, inst,
        {"cond_time": FlowDefaultsEntry(1, {"start": "07:00"})},
        {"cond_time": {"version": 1, "config": {"end": "22:00"}}},
    )
    # cond_time 是内置类型, plugin 层不适用
    assert resolved.values["start"] == "07:00" and resolved.sources["start"] == "flow"
    assert resolved.values["end"] == "23:00" and resolved.sources["end"] == "node"
    assert resolved.inherited["end"] == "22:00"


def test_validate_node_config_unknown_and_invalid():
    """未声明字段, null 不可空, 非法 time 值都报错"""
    reg = FlowNodeRegistry()
    node = reg.get("cond_time")
    inst = parse_flow_definition({"version": 3, "nodes": [
        {"id": "t", "type": "cond_time", "title": "t", "config": {"ghost": 1, "start": "25:00"}},
    ], "edges": []}).nodes["t"]
    errors = validate_node_config(node, inst, resolve_node_config(node, inst, {}))
    codes = {e.code for e in errors}
    assert "flow_config_unknown_field" in codes and "flow_config_invalid" in codes


def test_migrate_node_instance_and_edge_ports():
    """v1 节点实例按 renamed 迁字段与端口, 连线跟随"""
    reg = _registry_with_filter()
    d = parse_flow_definition({
        "version": 3,
        "nodes": [
            {"id": "a", "type": "source", "title": "a", "config": {}},
            {"id": "kf", "type": "keyword_filter.filter", "version": 1, "title": "f",
             "config": {"words": "广告"}},
        ],
        "edges": [{"from": "a.out", "to": "kf.in"}],
    })
    migrated, _, errs = migrate_flow_definition(d, reg)
    assert errs == []
    assert migrated.nodes["kf"].version == 2
    assert migrated.nodes["kf"].config == {"blocked_words": "广告"}
    assert migrated.edges[0].target_port == "messages"


def test_migrate_v2_rename_then_v3_new_field(tmp_path):
    """v2 把 a 改名 b, v3 又新增 a: 不看版本会迁错"""
    reg = FlowNodeRegistry()
    reg.register_plugin_nodes("p", [FlowNodeType(
        type="p.n", version=3, label="n", description="", role="transform",
        inputs=(PortSpec(name="m", types=("messages",), required=True),),
        outputs=(PortSpec(name="o", types=("messages",)),),
        config_schema={"b": ConfigField("b", "string", ""), "a": ConfigField("a", "string", "new")},
        renamed={2: RenamedEntry(fields={"a": "b"})},
        plugin="p",
    )])
    calls: list[tuple[int, dict]] = []

    def migrate(node, to_version, config):
        calls.append((to_version, dict(config)))
        if to_version == 3:
            config["a"] = "from-migrate"
        return config

    d = parse_flow_definition({"version": 3, "nodes": [
        {"id": "x", "type": "p.n", "version": 1, "title": "x", "config": {"a": "old"}},
    ], "edges": []})
    migrated, _, errs = migrate_flow_definition(d, reg, migrate_funcs={"p": migrate})
    assert errs == []
    assert migrated.nodes["x"].config == {"b": "old", "a": "from-migrate"}
    assert [c[0] for c in calls] == [2, 3]


def test_migrate_version_ahead_and_stale():
    """插件降级报 version_ahead; migrate 抛错报 stale, 原配置不动"""
    reg = _registry_with_filter()
    d = parse_flow_definition({"version": 3, "nodes": [
        {"id": "kf", "type": "keyword_filter.filter", "version": 9, "title": "f", "config": {}},
    ], "edges": []})
    _, _, errs = migrate_flow_definition(d, reg)
    assert any(e.code == "flow_node_version_ahead" for e in errs)

    def bad(node, to_version, config):
        raise RuntimeError("boom")
    d2 = parse_flow_definition({"version": 3, "nodes": [
        {"id": "kf", "type": "keyword_filter.filter", "version": 1, "title": "f", "config": {"words": "x"}},
    ], "edges": []})
    _, _, errs2 = migrate_flow_definition(d2, reg, migrate_funcs={"keyword_filter": bad})
    assert any(e.code == "flow_node_config_stale" for e in errs2)
