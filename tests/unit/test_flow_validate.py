"""流程静态检查: demo 模板与逐条规则"""
from __future__ import annotations

import pytest
from pathlib import Path
from types import SimpleNamespace

from satrap.edictum.flow.definition import migrate_flow_definition, parse_flow_definition
from satrap.core.type.flow.workflow import FlowWorkflowDecl
from satrap.edictum.flow.registry import FlowNodeRegistry
from satrap.edictum.flow.validate import validate
from satrap.core.type.plugin.catalog import PluginCatalogEntry
from satrap.edictum.plugin.config import PluginConfigManager


def _catalog(**plugins):
    """造插件目录存根"""
    return {
        name: PluginCatalogEntry(
            name=name,
            path=Path(name),
            capabilities={"tools": kw.get("tools", {})},
            interop={"requires": kw.get("requires", {})},
            display_name=kw.get("label", name),
            flow_nodes=tuple(kw.get("flow_nodes", ())),
            flow_workflow=FlowWorkflowDecl(
                foreground_only=kw.get("foreground_only", False),
                provides=tuple(kw.get("provides", ())),
            ),
        )
        for name, kw in plugins.items()
    }


def _validate(raw, registry=None, catalog=None, kinds=None, archive=True, execution_enabled=None):
    reg = registry or FlowNodeRegistry()
    d, _, _ = migrate_flow_definition(parse_flow_definition(raw), reg)
    return validate(
        d, reg, plugin_catalog=catalog or {}, plugin_execution_enabled=execution_enabled,
        conversation_kinds=kinds, archive_enabled=archive,
    )


AGENT_RP = {
    "version": 3,
    "defaults": {"react_workflow": {"version": 1, "config": {"model_name": "deepseek-v3"}},
                 "threshold": {"version": 1, "config": {"max_batch": 200}}},
    "nodes": [
        {"id": "source", "type": "source", "title": "消息到达", "config": {}},
        {"id": "bucket", "type": "bucket", "title": "桶", "config": {}},
        {"id": "th", "type": "threshold", "title": "Agent 阈值", "config": {"messages": 12, "chars": 1500}},
        {"id": "agent", "type": "react_workflow", "title": "Agent",
         "config": {"model_name": "deepseek-r1", "thinking": "medium", "max_steps": 12,
                    "prompt": "维护快照。只用 snapshot_write。",
                    "plugins": "flow_snapshot\nrag", "on_busy": "merge"}},
        {"id": "snapshot", "type": "state", "title": "快照",
         "config": {"schema": "sections", "sections": "群内理解\n成员近况\n长期要点"}},
        {"id": "rp_input", "type": "assemble", "title": "RP 输入", "config": {}},
        {"id": "wake", "type": "wake", "title": "RP 唤醒", "config": {}},
        {"id": "rp", "type": "react_workflow", "title": "RP",
         "config": {"model_name": "claude-sonnet-4.5", "prompt": "你是群员小萨。", "plugins": ""}},
        {"id": "reply", "type": "output", "title": "最后回复", "config": {}},
        {"id": "note1", "type": "note", "title": "说明", "config": {"text": "x"}},
    ],
    "edges": [
        {"from": "source.out", "to": "bucket.in"},
        {"from": "bucket.out", "to": "th.messages"},
        {"from": "th.event", "to": "agent.trigger"},
        {"from": "agent.text", "to": "snapshot.write"},
        {"from": "snapshot.read", "to": "rp_input.snapshot"},
        {"from": "bucket.out", "to": "rp_input.messages"},
        {"from": "bucket.out", "to": "wake.messages"},
        {"from": "wake.event", "to": "rp.trigger"},
        {"from": "rp_input.context", "to": "rp.input"},
        {"from": "rp.text", "to": "reply.reply"},
    ],
}


def test_agent_rp_template_zero_errors():
    """验收用例模板: 0 错误, 警告数与 demo 对齐"""
    catalog = _catalog(rag={"tools": {"rag_search": ""}})
    issues = _validate(AGENT_RP, catalog=catalog)
    errors = [i for i in issues if i.level == "error"]
    assert errors == []
    # demo 期望的警告: 后台写入方未运行前前台读到空快照
    assert {i.code for i in issues} == {"flow_state_read_before_write"}


def test_validate_service_uses_only_resolved_adapter_capabilities():
    """仅在明确平台实例可用时按真实布尔能力提示后台发送。"""
    from copy import deepcopy
    from satrap.core.config.edictum_service import EdictumConfigService

    flow = deepcopy(AGENT_RP)
    flow["edges"].append({"from": "agent.text", "to": "reply.reply"})
    adapters = {
        "unsupported": SimpleNamespace(supports_scheduled_group_send=False, supports_scheduled_private_send=True),
        "supported": SimpleNamespace(supports_scheduled_group_send=True, supports_scheduled_private_send=False),
        "unknown": SimpleNamespace(supports_scheduled_group_send=0),
    }
    service = EdictumConfigService(None, None, adapter_resolver=adapters.get)
    service.plugin_catalog = SimpleNamespace(scan=lambda: [])

    def warned(platform=None, kinds=None):
        payload = {"flow": flow, "conversation_kinds": kinds or ["group"]}
        if platform is not None:
            payload["platform_id"] = platform
        return any(i["code"] == "flow_background_send_unsupported" for i in service.validate_flow(payload)["issues"])

    assert warned("unsupported")
    assert not warned("supported")
    assert warned("supported", ["private"])
    assert not warned("unsupported", ["private"])
    assert not warned("unknown") and not warned("missing") and not warned()
    with pytest.raises(ValueError):
        service.validate_flow({"flow": flow, "platform_id": 123})


def test_unknown_type_and_source_count():
    """flow_unknown_node_type / flow_source_count"""
    bad = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s1", "config": {}},
            {"id": "s2", "type": "source", "title": "s2", "config": {}},
            {"id": "x", "type": "ghost.node", "title": "x", "config": {}},
        ],
        "edges": [],
    }
    issues = _validate(bad)
    codes = {i.code for i in issues}
    assert "flow_unknown_node_type" in codes and "flow_source_count" in codes


def test_duplicate_id_and_cycle():
    """重复 id 在 parse 阶段报; WF 互触成环报 flow_cycle"""
    cycle = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s", "config": {}},
            {"id": "a", "type": "react_workflow", "title": "a", "config": {"model_name": "m"}},
            {"id": "b", "type": "react_workflow", "title": "b", "config": {"model_name": "m"}},
        ],
        "edges": [
            {"from": "a.done", "to": "b.trigger"},
            {"from": "b.done", "to": "a.trigger"},
        ],
    }
    issues = _validate(cycle)
    assert any(i.code == "flow_cycle" for i in issues)


def test_port_required_and_multiple():
    """必填口没连线 / 单口多线"""
    bad = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s", "config": {}},
            {"id": "b", "type": "bucket", "title": "b", "config": {}},
            {"id": "b2", "type": "bucket", "title": "b2", "config": {}},
            {"id": "t", "type": "threshold", "title": "t", "config": {}},
        ],
        "edges": [
            {"from": "s.out", "to": "t.messages"},   # b.in 没接
            {"from": "s.out", "to": "b2.in"},
            {"from": "s.out", "to": "b2.in"},          # 同一口两条线
        ],
    }
    issues = _validate(bad)
    assert any(i.code == "flow_port_required" and i.node == "b" for i in issues)
    assert any(i.code == "flow_port_multiple" and i.node == "b2" for i in issues)


def test_logic_gate_rules():
    """OR 混合触发条件报错; AND 双触发给时间窗警告"""
    mixed = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s", "config": {}},
            {"id": "w", "type": "wake", "title": "w", "config": {}},
            {"id": "c", "type": "cond_time", "title": "c", "config": {}},
            {"id": "o", "type": "or", "title": "o", "config": {}},
        ],
        "edges": [
            {"from": "s.out", "to": "w.messages"},
            {"from": "w.event", "to": "o.a"},
            {"from": "c.out", "to": "o.b"},
        ],
    }
    issues = _validate(mixed)
    assert any(i.code == "flow_logic_or_mixed" for i in issues)


def test_threshold_and_wake_tile_rules():
    """空阈值/每条消息唤醒/共享磁贴各自的码"""
    tiles = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s", "config": {}},
            {"id": "t", "type": "threshold", "title": "t",
             "config": {"messages": 0, "chars": 0, "idle_seconds": 0}},
            {"id": "w", "type": "wake", "title": "w",
             "config": {"use_platform_policy": False, "mention": False, "wake_words": "",
                        "new_messages": 0, "cooldown": 0}},
        ],
        "edges": [{"from": "s.out", "to": "t.messages"}, {"from": "s.out", "to": "w.messages"}],
    }
    issues = _validate(tiles)
    codes = {i.code for i in issues}
    assert "flow_threshold_empty" in codes and "flow_wake_never" in codes


def test_state_rules():
    """多写方报错; 读回冗余和写入方无阈值游标的提示"""
    multi_writer = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s", "config": {}},
            {"id": "a", "type": "react_workflow", "title": "a", "config": {"model_name": "m"}},
            {"id": "b", "type": "react_workflow", "title": "b", "config": {"model_name": "m"}},
            {"id": "st", "type": "state", "title": "st", "config": {}},
            {"id": "w", "type": "wake", "title": "w", "config": {}},
        ],
        "edges": [
            {"from": "s.out", "to": "w.messages"},
            {"from": "w.event", "to": "a.trigger"},
            {"from": "w.event", "to": "b.trigger"},
            {"from": "a.text", "to": "st.write"},
            {"from": "b.text", "to": "st.write"},
        ],
    }
    issues = _validate(multi_writer)
    assert any(i.code == "flow_state_multi_writer" for i in issues)


def test_workflow_plugin_rules():
    """未装插件/只后台装前台插件/写状态缺 state_write/记忆后台写"""
    catalog = _catalog(
        group_chat={"tools": {"group_chat_reply": ""}, "foreground_only": True, "provides": []},
        memory={"tools": {"add_memory": ""}, "provides": ["memory_write"]},
    )
    bg_only = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s", "config": {}},
            {"id": "t", "type": "threshold", "title": "t", "config": {"messages": 5}},
            {"id": "a", "type": "react_workflow", "title": "a",
             "config": {"model_name": "m", "plugins": "group_chat\nmemory"}},
            {"id": "st", "type": "state", "title": "st", "config": {}},
        ],
        "edges": [
            {"from": "s.out", "to": "t.messages"},
            {"from": "t.event", "to": "a.trigger"},
            {"from": "a.text", "to": "st.write"},
        ],
    }
    issues = _validate(bg_only, catalog=catalog)
    codes = {i.code for i in issues}
    assert "flow_plugin_placement" in codes       # group_chat 只能前台
    assert "flow_memory_write_background" in codes
    assert "flow_snapshot_plugin_missing" in codes


def test_group_chat_reply_mode_rules():
    """私聊报错; 后台 WF 连 reply 也报"""
    private = dict(AGENT_RP)
    private["nodes"] = [dict(n) if n["id"] != "reply" else
                        {**n, "config": {"send_mode": "group_chat_reply"}}
                        for n in AGENT_RP["nodes"]]
    issues = _validate(private, kinds=["private"])
    assert any(i.code == "flow_reply_mode_private" for i in issues)


def test_disabled_plugin_node_and_missing_plugin(tmp_path, monkeypatch):
    """真实目录条目和宿主执行总闸驱动节点停用诊断"""
    from satrap.core.type.flow.registry import FlowNodeType, PortSpec
    node_type = FlowNodeType(
        type="ghost.n", version=1, label="g", description="", role="transform",
        inputs=(PortSpec(name="m", types=("messages",), required=True),),
        outputs=(PortSpec(name="o", types=("messages",)),),
        passthrough={"o": "m"},
        plugin="ghost",
    )
    reg = FlowNodeRegistry()
    reg.register_plugin_nodes("ghost", [node_type])
    raw = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s", "config": {}},
            {"id": "g", "type": "ghost.n", "title": "g", "config": {}},
        ],
        "edges": [{"from": "s.out", "to": "g.m"}],
    }
    issues = _validate(raw, registry=reg, catalog={})
    assert any(i.code == "flow_node_plugin_missing" for i in issues)

    catalog = _catalog(ghost={"flow_nodes": (node_type,)})
    config = PluginConfigManager(tmp_path / "plugin-config")
    config.save_execution("ghost", False, 0)
    execution_enabled = {"ghost": config.host_state("ghost")["execution_enabled"]}
    issues = _validate(raw, registry=reg, catalog=catalog, execution_enabled=execution_enabled)
    assert any(i.code == "flow_node_plugin_disabled" for i in issues)

    from satrap.core.config import edictum_service as service_module
    monkeypatch.setattr(service_module, "PluginConfigManager", lambda: config)
    service = service_module.EdictumConfigService(None, None)
    service.plugin_catalog = SimpleNamespace(scan=lambda: list(catalog.values()))
    result = service.validate_flow({"flow": raw})
    assert any(i["code"] == "flow_node_plugin_disabled" for i in result["issues"])

    config.save_execution("ghost", True, 1)
    execution_enabled = {"ghost": config.host_state("ghost")["execution_enabled"]}
    issues = _validate(raw, registry=reg, catalog=catalog, execution_enabled=execution_enabled)
    assert not any(i.code == "flow_node_plugin_disabled" for i in issues)
    result = service.validate_flow({"flow": raw})
    assert not any(i["code"] == "flow_node_plugin_disabled" for i in result["issues"])


def test_archive_required():
    """绑定的平台没开消息档案时报错"""
    issues = _validate(AGENT_RP, archive=False)
    assert any(i.code == "flow_archive_required" for i in issues)


def test_new_drain_cleared_hint():
    """drain 的磁贴驱动写 clear 状态槽的 WF 时给提示"""
    raw = dict(AGENT_RP)
    raw["nodes"] = [
        {**n, "config": {**n["config"], "on_new": "drain"}} if n["id"] == "th" else
        {**n, "config": {**n["config"], "on_new": "clear"}} if n["id"] == "snapshot" else n
        for n in AGENT_RP["nodes"]
    ]
    issues = _validate(raw, catalog=_catalog(rag={"tools": {}}))
    assert any(i.code == "flow_new_drain_cleared" for i in issues)


@pytest.mark.parametrize("kind", ["source", "bucket"])
def test_direct_archive_input_warns_when_max_messages_is_not_explicit(kind):
    """source/bucket 直连 WF 未显式配置时提示默认硬上限, 显式设置后不重复提示。"""
    raw = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": kind, "title": "s", "config": {}},
            {"id": "w", "type": "react_workflow", "title": "w",
             "config": {"model_name": "test"}},
        ],
        "edges": [{"from": "s.out", "to": "w.input"}],
    }
    issues = _validate(raw)
    warning = next(issue for issue in issues if issue.code == "flow_source_default_limit")
    assert warning.level == "warning" and warning.field == "max_messages"
    raw["nodes"][0]["config"]["max_messages"] = 50
    issues = _validate(raw)
    assert not any(issue.code == "flow_source_default_limit" for issue in issues)


def test_custom_transform_archive_input_warns_when_max_messages_is_not_explicit():
    """source 直连自定义 transform 的输入连接同样给出上限提示。"""
    from satrap.core.type.flow.registry import FlowNodeType, PortSpec
    registry = FlowNodeRegistry()
    registry.register_plugin_nodes("ghost", [FlowNodeType(
        type="ghost.n", version=1, label="g", description="", role="transform",
        inputs=(PortSpec(name="m", types=("messages",), required=True),),
        outputs=(PortSpec(name="o", types=("messages",)),), plugin="ghost",
    )])
    raw = {
        "version": 3,
        "nodes": [
            {"id": "s", "type": "source", "title": "s", "config": {}},
            {"id": "g", "type": "ghost.n", "title": "g", "config": {}},
        ],
        "edges": [{"from": "s.out", "to": "g.m"}],
    }
    issues = _validate(raw, registry=registry, catalog=_catalog(ghost={}))
    assert any(issue.code == "flow_source_default_limit" for issue in issues)
