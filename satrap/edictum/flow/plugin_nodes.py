"""
插件 flow_nodes 声明解析与 flow.workflow 声明

从 meta.yaml 的 flow_nodes 列表解析节点契约 (端口, 配置, 版本, 改名记录),
推断角色, 端口显示与 passthrough; 解析 flow.workflow 的 WF 安装限制声明
"""
from __future__ import annotations

from satrap.edictum.flow.registry import FlowNodeType, PortSpec, RenamedEntry
from satrap.edictum.flow.types import PLUGIN_PORT_TYPES, PORT_TYPE_LABELS, PortType
from satrap.edictum.plugin_config import ConfigField, parse_config_schema

from dataclasses import dataclass, field
from typing import Any, cast
import inspect
import re

from satrap.core.log import logger


_NODE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
"""节点名和端口名格式; when 保留给宿主的启用条件口"""

MODEL_FIELD_TYPES = ("llm", "embed", "rerank", "asr", "img")
"""出现即视为调用模型的配置字段类型"""

FLOW_WORKFLOW_PROVIDES = ("memory_write", "state_write")
"""flow.workflow.provides 首版允许的取值"""


class FlowContractError(ValueError):
    """flow_nodes 契约校验失败, 带稳定错误码"""

    def __init__(self, code: str, message: str) -> None:
        """
        记录错误码与可读原因

        参数:
        - code: flow_contract_* 错误码
        - message: 面向插件作者的失败原因
        """
        super().__init__(message)
        self.code = code


def _fail(code: str, node: str, message: str) -> FlowContractError:
    """组装带节点定位的契约错误"""
    return FlowContractError(code, f"flow_nodes.{node}: {message}")


@dataclass(frozen=True)
class FlowWorkflowDecl:
    """插件 flow.workflow 声明: 装到 WF 节点上时的限制与能力"""

    foreground_only: bool = False
    """只能装在前台 WF; 装在纯后台 WF 上静态检查报错"""
    provides: tuple[str, ...] = ()
    """声明的能力, 首版固定 memory_write / state_write"""

    def to_payload(self) -> dict[str, Any]:
        """序列化给前端"""
        return {
            "foreground_only": self.foreground_only,
            "provides": list(self.provides),
        }


def parse_flow_workflow(meta: dict[str, Any], plugin_name: str) -> FlowWorkflowDecl:
    """
    解析 meta.yaml 的 flow.workflow 声明

    参数:
    - meta: 插件元数据
    - plugin_name: 插件名, 用于日志定位

    返回:
    - FlowWorkflowDecl: 未声明时返回默认值
    """
    flow = meta.get("flow")
    if flow is None:
        return FlowWorkflowDecl()
    if not isinstance(flow, dict):
        logger.warning(f"[插件目录] {plugin_name} 的 flow 应为映射, 已忽略")
        return FlowWorkflowDecl()
    workflow = cast(dict[str, Any], flow).get("workflow")
    if workflow is None:
        return FlowWorkflowDecl()
    if not isinstance(workflow, dict):
        logger.warning(f"[插件目录] {plugin_name} 的 flow.workflow 应为映射, 已忽略")
        return FlowWorkflowDecl()
    workflow = cast(dict[str, Any], workflow)
    provides_raw = workflow.get("provides") or []
    provides: list[str] = []
    if isinstance(provides_raw, list):
        for item in cast(list[Any], provides_raw):
            text = str(item)
            if text in FLOW_WORKFLOW_PROVIDES and text not in provides:
                provides.append(text)
            elif text not in FLOW_WORKFLOW_PROVIDES:
                logger.warning(f"[插件目录] {plugin_name} flow.workflow.provides 的 {text} 不可识别, 已忽略")
    else:
        logger.warning(f"[插件目录] {plugin_name} flow.workflow.provides 应为列表, 已忽略")
    return FlowWorkflowDecl(
        foreground_only=bool(workflow.get("foreground_only", False)),
        provides=tuple(provides),
    )


def _parse_port_map(raw: object, node_name: str, direction: str) -> dict[str, tuple[PortType, bool]]:
    """
    解析 in/out 端口映射 {端口名: 类型}

    参数:
    - raw: meta.yaml 中的 in/out 原始值
    - node_name: 节点名, 用于报错定位
    - direction: "输入" 或 "输出"

    返回:
    - {端口名: (端口类型, 是否可选)}; ? 后缀只出现在输入, 可选标记随类型返回
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        # out: messages 是 out: {out: messages} 的简写
        if direction == "输出":
            raw = {"out": raw}
        else:
            raise _fail("flow_contract_port_type", node_name, f"{direction}声明必须是映射, 不能是字符串")
    if not isinstance(raw, dict):
        raise _fail("flow_contract_port_type", node_name, f"{direction}声明必须是映射")
    ports: dict[str, tuple[PortType, bool]] = {}
    for key, value in cast(dict[object, object], raw).items():
        port_name = str(key)
        if _NODE_NAME_RE.fullmatch(port_name) is None or port_name == "when":
            raise _fail("flow_contract_name", node_name, f"{direction}端口名 {port_name} 非法或为保留名")
        text = str(value).strip()
        optional = text.endswith("?")
        base = text[:-1] if optional else text
        if base not in PLUGIN_PORT_TYPES:
            raise _fail("flow_contract_port_type", node_name,
                        f"{direction}端口 {port_name} 的类型 {base} 不可用, 只能是 {'/'.join(PLUGIN_PORT_TYPES)}")
        if optional and direction != "输入":
            raise _fail("flow_contract_port_type", node_name, f"{direction}端口 {port_name} 不能声明为可选")
        ports[port_name] = (base, optional)   # type: ignore[dict-item]
    return ports


def _infer_passthrough(
    node_name: str,
    required_inputs: dict[str, PortType],
    outputs: dict[str, PortType],
    declared: object,
) -> dict[str, str | None]:
    """
    推断或校验 passthrough: 启用条件为假时每个输出取哪个必填输入

    参数:
    - node_name: 节点名, 用于报错定位
    - required_inputs: 必填输入 {端口名: 类型}
    - outputs: 输出 {端口名: 类型}
    - declared: meta.yaml 声明的 passthrough, 无则推断

    返回:
    - {输出口: 输入口 或 None}; 推断不出且未声明时报 flow_contract_passthrough
    """
    if declared is not None:
        if not isinstance(declared, dict):
            raise _fail("flow_contract_passthrough", node_name, "passthrough 必须是 {输出口: 输入口 或 null} 映射")
        mapping = cast(dict[object, object], declared)
        result: dict[str, str | None] = {}
        for out_name in outputs:
            if out_name not in mapping:
                raise _fail("flow_contract_passthrough", node_name,
                            f"手写 passthrough 必须列出全部输出, 缺少 {out_name}")
        for out_name, src in mapping.items():
            out_key = str(out_name)
            if out_key not in outputs:
                raise _fail("flow_contract_passthrough", node_name, f"passthrough 指向不存在的输出 {out_key}")
            if src is None:
                result[out_key] = None
                continue
            src_name = str(src)
            if src_name not in required_inputs:
                raise _fail("flow_contract_passthrough", node_name,
                            f"passthrough.{out_key} 指向非必填输入 {src_name} (可选输入不连线时没有值)")
            if required_inputs[src_name] != outputs[out_key]:
                raise _fail("flow_contract_passthrough", node_name,
                            f"passthrough.{out_key} 类型不一致: {required_inputs[src_name]} -> {outputs[out_key]}")
            result[out_key] = src_name
        return result
    inferred: dict[str, str | None] = {}
    for out_name, out_type in outputs.items():
        same_type = [name for name, t in required_inputs.items() if t == out_type]
        if len(same_type) == 1:
            inferred[out_name] = same_type[0]
        elif not same_type:
            raise _fail("flow_contract_passthrough", node_name,
                        f"输出 {out_name} 在必填输入里没有 {out_type} 类型, 无法推断 passthrough; 请手写 passthrough: {{{out_name}: null}}")
        else:
            raise _fail("flow_contract_passthrough", node_name,
                        f"输出 {out_name} 有多个同类型必填输入 ({', '.join(same_type)}), 无法推断 passthrough")
    return inferred


def parse_flow_nodes(meta: dict[str, Any], plugin_name: str) -> list[FlowNodeType]:
    """
    解析 meta.yaml 的 flow_nodes 列表为节点类型契约

    参数:
    - meta: 插件元数据
    - plugin_name: 插件稳定名称, 与节点名拼成完整类型名

    返回:
    - list[FlowNodeType]: 插件节点类型; 契约违法抛 FlowContractError
    """
    raw = meta.get("flow_nodes")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FlowContractError("flow_contract_name", f"{plugin_name}: flow_nodes 必须是列表")
    nodes: list[FlowNodeType] = []
    seen: set[str] = set()
    for item in cast(list[Any], raw):
        if not isinstance(item, dict):
            raise FlowContractError("flow_contract_name", f"{plugin_name}: flow_nodes 列表项必须是映射")
        spec = cast(dict[str, Any], item)
        name = str(spec.get("name") or "")
        if name in seen:
            raise _fail("flow_contract_name", name, f"节点名 {name} 在同一插件内重复")
        seen.add(name)
        if _NODE_NAME_RE.fullmatch(name) is None:
            raise _fail("flow_contract_name", name or "?", f"节点名 {name!r} 非法, 需要 [a-z][a-z0-9_]{{0,31}}")
        if spec.get("out") is None:
            raise _fail("flow_contract_port_type", name, "缺少必填字段 out")
        label = str(spec.get("label") or "").strip()
        if not label:
            raise _fail("flow_contract_name", name, "缺少必填字段 label")

        # Step.1 解析输出, 判定角色
        outputs = _parse_port_map(spec.get("out"), name, "输出")
        if not outputs:
            raise _fail("flow_contract_port_type", name, "缺少必填字段 out")
        out_types = {t for t, _ in outputs.values()}
        is_condition = out_types == {"condition"}
        if "condition" in out_types and not is_condition:
            raise _fail("flow_contract_port_type", name, "condition 只能作为唯一输出")
        role = "condition" if is_condition else "transform"

        # Step.2 解析输入并区分必填/可选
        inputs = _parse_port_map(spec.get("in"), name, "输入")
        if is_condition:
            bad = [p for p, (t, _) in inputs.items() if t != "state"]
            if bad:
                raise _fail("flow_contract_port_type", name,
                            f"条件节点只能接 state 输入, 端口 {', '.join(bad)} 类型不符")
            for port_name, (t, optional) in inputs.items():
                if optional:
                    raise _fail("flow_contract_port_type", name, f"条件节点的 state 输入 {port_name} 不能声明为可选")

        # Step.3 校验 needs 只用于条件节点
        needs_raw = spec.get("needs")
        needs: str | None = None
        if needs_raw is not None:
            if not is_condition:
                raise _fail("flow_contract_needs", name, "needs 只能用于条件节点")
            if needs_raw not in ("message", "batch"):
                raise _fail("flow_contract_needs", name, f"needs 只能是 message 或 batch, 当前 {needs_raw!r}")
            needs = str(needs_raw)

        # Step.4 解析版本与改名记录
        version_raw = spec.get("version", 1)
        version = 1
        if isinstance(version_raw, int) and not isinstance(version_raw, bool) and version_raw >= 1:
            version = version_raw
        else:
            raise _fail("flow_contract_renamed", name, f"version 必须是正整数, 当前 {version_raw!r}")
        renamed: dict[int, RenamedEntry] = {}
        renamed_raw = spec.get("renamed")
        if renamed_raw is not None:
            if not isinstance(renamed_raw, dict):
                raise _fail("flow_contract_renamed", name, "renamed 必须是 {版本: {fields, ports}} 映射")
            for rv, entry in cast(dict[object, object], renamed_raw).items():
                try:
                    target = int(str(rv))
                except ValueError:
                    raise _fail("flow_contract_renamed", name, f"renamed 版本键 {rv!r} 不是整数")
                if target <= 1 or target > version:
                    raise _fail("flow_contract_renamed", name,
                                f"renamed 记录的目标版本 {target} 必须位于 2..{version}")
                if target in renamed:
                    raise _fail("flow_contract_renamed", name, f"renamed 版本 {target} 重复")
                if not isinstance(entry, dict):
                    raise _fail("flow_contract_renamed", name, f"renamed.{target} 必须是映射")
                entry_map = cast(dict[object, object], entry)
                fields_map = {str(k): str(v) for k, v in cast(dict[object, object], entry_map.get("fields") or {}).items()}
                ports_map = {str(k): str(v) for k, v in cast(dict[object, object], entry_map.get("ports") or {}).items()}
                renamed[target] = RenamedEntry(fields=fields_map, ports=ports_map)

        # Step.5 解析节点配置 (复用插件 config_schema 解析) 与读取说明
        try:
            config_schema = parse_config_schema({"config_schema": spec.get("config_schema") or {}})
        except ValueError as error:
            raise _fail("flow_contract_visible_when", name, str(error)) from error
        calls_model = any(f.type in MODEL_FIELD_TYPES for f in config_schema.values())
        reading: dict[str, str] = {}
        reading_raw = spec.get("reading")
        if reading_raw is not None:
            if not isinstance(reading_raw, dict):
                raise _fail("flow_contract_name", name, "reading 必须是 {端口名: 文字} 映射")
            for port_name, text in cast(dict[object, object], reading_raw).items():
                key = str(port_name)
                if key not in inputs:
                    raise _fail("flow_contract_name", name, f"reading 指向不存在的输入端口 {key}")
                reading[key] = str(text)

        # Step.6 组装端口声明: 数据口上进下出, 信号口左进右出
        in_ports = tuple(
            PortSpec(
                name=port_name,
                types=(port_type,),
                required=not optional,
                label=PORT_TYPE_LABELS[port_type],
            )
            for port_name, (port_type, optional) in inputs.items()
        )
        if role == "transform":
            in_ports += (PortSpec(name="when", types=("condition",), multiple=True, side="left", label="启用条件"),)
        out_ports = tuple(
            PortSpec(
                name=port_name,
                types=(port_type,),
                label=PORT_TYPE_LABELS[port_type],
                side="right" if port_type == "condition" else "",
            )
            for port_name, (port_type, _) in outputs.items()
        )

        # Step.7 passthrough: 只有 transform 需要; 条件节点为假时直接不触发
        passthrough = None
        if role == "transform":
            required_inputs = {p: t for p, (t, optional) in inputs.items() if not optional}
            declared_outputs = {p: t for p, (t, _) in outputs.items()}
            passthrough = _infer_passthrough(name, required_inputs, declared_outputs, spec.get("passthrough"))

        nodes.append(FlowNodeType(
            type=f"{plugin_name}.{name}",
            version=version,
            label=label,
            description=str(spec.get("description") or ""),
            role=role,   # type: ignore[arg-type]
            category="条件" if is_condition else "插件",
            inputs=in_ports,
            outputs=out_ports,
            config_schema=config_schema,
            passthrough=passthrough,
            needs=needs,   # type: ignore[arg-type]
            calls_model=calls_model,
            renamed=renamed,
            reading=reading,
            plugin=plugin_name,
        ))
    return nodes


def load_flow_node_functions(plugin_dir: Any, module_name: str) -> dict[str, Any]:
    """
    加载插件 flow_nodes.py 并收集节点实现

    三种写法: 与节点同名的函数或类; @node("名") 装饰的函数; FlowNode 子类 (node_name 属性对应)

    参数:
    - plugin_dir: 插件目录
    - module_name: 加载用模块名

    返回:
    - dict[str, Any]: {节点名: 可调用对象或 FlowNode 子类}; 文件不存在返回 {}
    """
    from satrap.edictum.plugin import _load_module
    from satrap.edictum.flow.types import FlowNode

    mod = _load_module(plugin_dir / "flow_nodes.py", f"{module_name}.flow_nodes", fresh=True)
    if mod is None:
        return {}
    found: dict[str, Any] = {}
    for attr in dir(mod):
        if attr.startswith("_"):
            continue
        value = getattr(mod, attr)
        marked = getattr(value, "__flow_node_name__", None)
        if marked is not None:
            found[str(marked)] = value
            continue
        if inspect.isclass(value) and issubclass(value, FlowNode) and value is not FlowNode:
            node_name = str(getattr(value, "node_name", "") or attr)
            found[node_name] = value
            continue
        if inspect.isfunction(value) and getattr(value, "__module__", None) == mod.__name__:
            # 只收集本模块定义的函数, 避免把 import 进来的帮手当成节点
            found.setdefault(attr, value)
    return found


def validate_flow_node_pairing(
    plugin_name: str,
    declared: list[FlowNodeType],
    implemented: dict[str, Any],
) -> None:
    """
    校验 meta.yaml 声明与 flow_nodes.py 实现一一对应, 且签名与端口一致

    参数:
    - plugin_name: 插件名, 用于报错定位
    - declared: meta.yaml 解析出的节点类型
    - implemented: flow_nodes.py 收集到的实现

    声明与实现必须严格一致: 声明了没有实现, 或实现了没有声明, 均拒绝安装
    """
    from satrap.edictum.flow.types import FlowNode

    declared_names = {t.type.rsplit(".", 1)[-1] for t in declared}
    missing = declared_names - set(implemented)
    extra = {name for name in implemented if name not in declared_names}
    for name in sorted(missing):
        raise FlowContractError("flow_contract_unpaired",
                                f"{plugin_name}: meta.yaml 声明了节点 {name} 但 flow_nodes.py 没有同名函数, 类或 @node")
    for name in sorted(extra):
        raise FlowContractError("flow_contract_unpaired",
                                f"{plugin_name}: flow_nodes.py 的 {name} 是 @node 或 FlowNode 但未在 meta.yaml flow_nodes 声明")
    for node_type in declared:
        node_name = node_type.type.rsplit(".", 1)[-1]
        impl = implemented.get(node_name)
        if impl is None:
            continue
        target = impl if inspect.isfunction(impl) else getattr(impl, "__call__", None)
        if inspect.isclass(impl) and issubclass(impl, FlowNode):
            target = impl.__call__
        if target is None:
            raise FlowContractError("flow_contract_signature",
                                    f"{plugin_name}.{node_name}: 实现不可调用")
        if node_type.role == "condition":
            async_impl = inspect.iscoroutinefunction(impl) or (
                inspect.isclass(impl) and inspect.iscoroutinefunction(getattr(impl, "__call__", None)))
            if async_impl:
                raise FlowContractError("flow_contract_signature",
                                        f"{plugin_name}.{node_name}: 条件节点必须是同步函数, 不能是 async def")
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            continue
        params = [p for p in signature.parameters.values()
                  if p.name not in ("self", "ctx") and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
        declared_inputs = {p.name: p for p in node_type.inputs if p.name != "when"}
        param_names = {p.name for p in params}
        declared_names_set = set(declared_inputs)
        if param_names != declared_names_set:
            raise FlowContractError(
                "flow_contract_signature",
                f"{plugin_name}.{node_name}: 参数名 {sorted(param_names)} 与输入端口 {sorted(declared_names_set)} 不一致 (ctx 除外)")
        for p in params:
            port = declared_inputs[p.name]
            if not port.required and p.default is inspect.Parameter.empty:
                raise FlowContractError("flow_contract_signature",
                                        f"{plugin_name}.{node_name}: 可选输入 {p.name} 必须有默认值 (连线缺失时传 None)")
