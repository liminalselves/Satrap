"""flow_nodes 契约解析与配对校验"""
from __future__ import annotations

from pathlib import Path
import pytest
import textwrap

from satrap.core.type.flow.definition import FlowContractError
from satrap.edictum.flow.plugin_nodes import load_flow_node_functions, parse_flow_nodes, validate_flow_node_pairing


def _parse(flow_nodes: list[dict], plugin: str = "test"):
    return parse_flow_nodes({"flow_nodes": flow_nodes}, plugin)


def test_parse_minimal_condition():
    """最小条件节点: 只有 label/out/config"""
    nodes = _parse([{
        "name": "in_hours", "label": "免打扰时段内", "out": "condition",
        "config_schema": {"start": {"type": "time", "default": "22:00"}},
    }], "quiet_hours")
    assert len(nodes) == 1
    n = nodes[0]
    assert n.type == "quiet_hours.in_hours"
    assert n.role == "condition" and n.category == "条件"
    assert n.config_schema["start"].type == "time"


def test_parse_transform_passthrough_inference():
    """transform 一进一出: passthrough 按必填输入同类型推断"""
    nodes = _parse([{
        "name": "filter", "label": "关键词过滤",
        "in": {"messages": "messages"}, "out": {"kept": "messages"},
    }], "keyword_filter")
    n = nodes[0]
    assert n.role == "transform"
    assert n.passthrough == {"kept": "messages"}
    assert any(p.name == "when" for p in n.inputs)   # transform 自动带启用条件口


def test_parse_transform_needs_manual_passthrough():
    """推断不出时必须手写; 手写必须列全且类型一致"""
    nodes = _parse([{
        "name": "split", "label": "按发言人拆分",
        "in": {"messages": "messages"},
        "out": {"members": "messages", "bot": "messages"},
        "passthrough": {"members": "messages", "bot": None},
    }], "message_split")
    assert nodes[0].passthrough == {"members": "messages", "bot": None}


def test_parse_optional_input():
    """可选输入: ? 后缀, required=False"""
    nodes = _parse([{
        "name": "retrieve", "label": "检索",
        "in": {"messages": "messages", "snapshot": "state?"},
        "out": "context",
        "passthrough": {"out": None},
    }], "rag_context")
    n = nodes[0]
    ports = {p.name: p for p in n.inputs}
    assert ports["messages"].required and not ports["snapshot"].required


def test_parse_version_and_renamed():
    """version 与 renamed 记录"""
    nodes = _parse([{
        "name": "filter", "label": "x", "version": 2,
        "renamed": {2: {"fields": {"words": "blocked_words"}, "ports": {"in": "messages"}}},
        "in": {"messages": "messages"}, "out": {"kept": "messages"},
    }], "keyword_filter")
    n = nodes[0]
    assert n.version == 2
    assert n.renamed[2].fields == {"words": "blocked_words"}
    assert n.renamed[2].ports == {"in": "messages"}


def test_parse_calls_model_by_model_field():
    """config_schema 含模型字段即 calls_model"""
    nodes = _parse([{
        "name": "compress", "label": "压缩",
        "in": {"context": "context"}, "out": "context",
        "passthrough": {"out": "context"},
        "config_schema": {"model_name": {"type": "llm", "required": True}},
    }], "context_compress")
    assert nodes[0].calls_model is True


def test_parse_needs_and_multi_output():
    """条件 needs; transform 多输出 dict"""
    nodes = _parse([{
        "name": "has_link", "label": "含链接", "out": "condition", "needs": "message",
    }], "link_guard")
    assert nodes[0].needs == "message"


CONTRACT_REJECTS = [
    ([{"name": "Bad-Name", "label": "x", "out": "text"}], "flow_contract_name"),
    ([{"name": "x", "label": "x", "out": "json"}], "flow_contract_port_type"),
    ([{"name": "x", "label": "x", "out": "event"}], "flow_contract_port_type"),
    ([{"name": "x", "label": "x", "out": {"ok": "condition", "rest": "messages"}}], "flow_contract_port_type"),
    ([{"name": "x", "label": "x", "in": {"m": "messages"}, "out": "condition"}], "flow_contract_port_type"),
    ([{"name": "x", "label": "x", "in": {"when": "messages"}, "out": "text"}], "flow_contract_name"),
    ([{"name": "x", "label": "x", "out": "text",
       "config_schema": {"a": {"type": "string", "visible_when": {"mode": "x"}}}}], "flow_contract_visible_when"),
    ([{"name": "x", "label": "x", "in": {"m": "messages"}, "out": "text", "needs": "message"}], "flow_contract_needs"),
    ([{"name": "x", "label": "x", "version": 2, "renamed": {3: {"fields": {"a": "b"}}}, "out": "text"}], "flow_contract_renamed"),
    ([{"name": "x", "label": "x", "in": {"messages": "messages"}, "out": "text"}], "flow_contract_passthrough"),
    ([{"name": "x", "label": "x", "in": {"a": "messages", "b": "messages"}, "out": "messages"}], "flow_contract_passthrough"),
    ([{"name": "x", "label": "x", "in": {"messages": "messages"},
       "out": {"x": "messages", "y": "messages"}, "passthrough": {"x": "messages"}}], "flow_contract_passthrough"),
    ([{"name": "x", "label": "x", "in": {"ctx": "context"}, "out": "messages",
       "passthrough": {"out": "ctx"}}], "flow_contract_passthrough"),
    ([{"name": "x", "label": "x", "in": {"extra": "messages?"}, "out": "messages",
       "passthrough": {"out": "extra"}}], "flow_contract_passthrough"),
    ([{"name": "x", "label": "x", "out": "text", "passthrough": {"out": None}},
       {"name": "x", "label": "y", "out": "text", "passthrough": {"out": None}}], "flow_contract_name"),
]


@pytest.mark.parametrize("flow_nodes,code", CONTRACT_REJECTS)
def test_contract_rejects(flow_nodes, code):
    """示例文档第 18 节列举的拒绝写法"""
    with pytest.raises(FlowContractError) as exc:
        _parse(flow_nodes)
    assert exc.value.code == code


def test_load_flow_node_functions_and_pairing(tmp_path: Path):
    """三种写法收集 + 声明/实现配对校验"""
    (tmp_path / "flow_nodes.py").write_text(textwrap.dedent("""
        from satrap.edictum.flow import node, FlowNode

        @node("decorated")
        def whatever(ctx):
            return True

        def plain(ctx):
            return True

        class Helper(FlowNode):
            node_name = "helper"
            def __call__(self, ctx):
                return True
    """), encoding="utf-8")
    impl = load_flow_node_functions(tmp_path, "tp")
    assert sorted(impl) == ["decorated", "helper", "plain"]

    declared = _parse([
        {"name": "decorated", "label": "x", "out": "condition"},
        {"name": "helper", "label": "x", "out": "condition"},
        {"name": "plain", "label": "x", "out": "condition"},
    ], "tp")
    validate_flow_node_pairing("tp", declared, impl)   # 不抛错


def test_pairing_unpaired_and_signature(tmp_path: Path):
    """声明无实现/实现未声明/签名不符/可选无默认"""
    (tmp_path / "flow_nodes.py").write_text(textwrap.dedent("""
        def a(ctx, extra):
            return True
        def b(ctx):
            return True
    """), encoding="utf-8")
    impl = load_flow_node_functions(tmp_path, "tp")

    with pytest.raises(FlowContractError) as e1:
        validate_flow_node_pairing("tp", _parse([{"name": "c", "label": "x", "out": "condition"}], "tp"), impl)
    assert e1.value.code == "flow_contract_unpaired"
    with pytest.raises(FlowContractError) as e2:
        validate_flow_node_pairing("tp", _parse([{"name": "a", "label": "x", "out": "condition"}], "tp"), impl)
    assert e2.value.code == "flow_contract_unpaired"

    declared = _parse([{
        "name": "a", "label": "x", "in": {"extra": "messages?"},
        "out": {"k": "messages"}, "passthrough": {"k": None},
    }], "tp")
    with pytest.raises(FlowContractError) as e3:
        validate_flow_node_pairing("tp", declared, {"a": impl["a"]})
    assert e3.value.code == "flow_contract_signature"

    (tmp_path / "flow_nodes.py").write_text("async def c(ctx):\n    return True\n", encoding="utf-8")
    with pytest.raises(FlowContractError) as e4:
        validate_flow_node_pairing("tp",
            _parse([{"name": "c", "label": "x", "out": "condition"}], "tp"),
            load_flow_node_functions(tmp_path, "tp2"))
    assert e4.value.code == "flow_contract_signature"
