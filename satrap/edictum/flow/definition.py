"""
流程定义 (params.flow) 解析, 配置合并与版本迁移

解析存档格式 (version/defaults/nodes/edges/layout/budget), 按
类型默认 < 插件全局默认 < 流程默认 < 本节点 的顺序合并配置并记录来源;
对节点实例, 流程默认, 插件全局默认与连线端口各自按版本逐版本迁移
"""
from __future__ import annotations

from typing import Any, Callable, cast

from satrap.core.type.flow.definition import (
    ConfigSource, FlowBudget, FlowDefaultsEntry, FlowDefinition, FlowDefinitionError,
    FlowEdge, FlowNodeInstance, ResolvedNodeConfig,
)
from satrap.core.type.flow.registry import FlowNodeType
from satrap.edictum.flow.registry import FlowNodeRegistry


FLOW_ARCHIVE_VERSION = 3
"""流程存档格式版本; 与节点契约版本, 插件发行版本是三个不同的东西"""


def _parse_budget(raw: object) -> FlowBudget | None:
    """
    解析 params.flow.budget

    参数:
    - raw: 原始 budget 值

    返回:
    - FlowBudget | None: 未声明返回 None, 非法抛 FlowDefinitionError
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise FlowDefinitionError("flow_budget_invalid", "params.flow.budget 必须是对象")
    spec = cast(dict[str, Any], raw)
    max_calls = spec.get("max_calls", 0)
    window = spec.get("window_minutes", 60)
    count = spec.get("count", "all")
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) or max_calls < 1:
        raise FlowDefinitionError("flow_budget_invalid", "budget.max_calls 必须是正整数")
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise FlowDefinitionError("flow_budget_invalid", "budget.window_minutes 必须是正整数")
    if count not in ("all", "wf"):
        raise FlowDefinitionError("flow_budget_invalid", "budget.count 只能是 all 或 wf")
    return FlowBudget(max_calls=max_calls, window_minutes=window, count=count)   # type: ignore[arg-type]


def _parse_endpoint(raw: object, edge_index: int, side: str) -> tuple[str, str]:
    """
    解析连线端点 "节点id.端口名"

    参数:
    - raw: 原始端点字符串
    - edge_index: 连线序号, 用于报错定位
    - side: "from" 或 "to"

    返回:
    - (节点 id, 端口名)
    """
    text = str(raw or "").strip()
    if "." not in text:
        raise FlowDefinitionError("flow_edge_invalid", f"第 {edge_index} 条连线的 {side} 需要 节点id.端口名, 当前 {text!r}")
    node_id, _, port = text.rpartition(".")
    if not node_id or not port:
        raise FlowDefinitionError("flow_edge_invalid", f"第 {edge_index} 条连线的 {side} 需要 节点id.端口名, 当前 {text!r}")
    return node_id, port


def parse_flow_definition(raw: object) -> FlowDefinition:
    """
    解析 params.flow 为流程定义

    参数:
    - raw: params.flow 原始值

    返回:
    - FlowDefinition: 结构合法的定义; 节点类型与端口在静态检查阶段判定
    """
    if not isinstance(raw, dict):
        raise FlowDefinitionError("flow_definition_invalid", "params.flow 必须是对象")
    spec = cast(dict[str, Any], raw)
    version = spec.get("version", FLOW_ARCHIVE_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise FlowDefinitionError("flow_definition_invalid", "params.flow.version 必须是正整数")
    if version > FLOW_ARCHIVE_VERSION:
        raise FlowDefinitionError("flow_definition_invalid",
                                  f"流程格式版本 {version} 高于宿主支持的 {FLOW_ARCHIVE_VERSION}")

    # Step.1 节点列表
    nodes_raw = spec.get("nodes")
    if not isinstance(nodes_raw, list):
        raise FlowDefinitionError("flow_definition_invalid", "params.flow.nodes 必须是列表")
    nodes: dict[str, FlowNodeInstance] = {}
    for index, item in enumerate(cast(list[Any], nodes_raw)):
        if not isinstance(item, dict):
            raise FlowDefinitionError("flow_definition_invalid", f"第 {index} 个节点必须是对象")
        entry = cast(dict[str, Any], item)
        node_id = str(entry.get("id") or "").strip()
        if not node_id:
            raise FlowDefinitionError("flow_definition_invalid", f"第 {index} 个节点缺少 id")
        if node_id in nodes:
            raise FlowDefinitionError("flow_duplicate_id", f"节点 id 重复: {node_id}", node=node_id)
        type_name = str(entry.get("type") or "").strip()
        if not type_name:
            raise FlowDefinitionError("flow_definition_invalid", f"节点 {node_id} 缺少 type", node=node_id)
        node_version = entry.get("version", 1)
        if not isinstance(node_version, int) or isinstance(node_version, bool) or node_version < 1:
            raise FlowDefinitionError("flow_definition_invalid", f"节点 {node_id} 的 version 必须是正整数", node=node_id)
        config = entry.get("config") or {}
        if not isinstance(config, dict):
            raise FlowDefinitionError("flow_definition_invalid", f"节点 {node_id} 的 config 必须是对象", node=node_id)
        nodes[node_id] = FlowNodeInstance(
            id=node_id, type=type_name, version=node_version,
            title=str(entry.get("title") or node_id),
            config=dict(cast(dict[str, Any], config)),
        )

    # Step.2 连线
    edges_raw = spec.get("edges") or []
    if not isinstance(edges_raw, list):
        raise FlowDefinitionError("flow_definition_invalid", "params.flow.edges 必须是列表")
    edges: list[FlowEdge] = []
    for index, item in enumerate(cast(list[Any], edges_raw)):
        if not isinstance(item, dict):
            raise FlowDefinitionError("flow_edge_invalid", f"第 {index} 条连线必须是对象")
        entry = cast(dict[str, Any], item)
        source, source_port = _parse_endpoint(entry.get("from"), index, "from")
        target, target_port = _parse_endpoint(entry.get("to"), index, "to")
        if source not in nodes or target not in nodes:
            raise FlowDefinitionError("flow_edge_invalid",
                                      f"第 {index} 条连线引用了不存在的节点 ({source} -> {target})",
                                      node=target)
        edges.append(FlowEdge(
            source=source, source_port=source_port,
            target=target, target_port=target_port,
            label=str(entry.get("label") or ""),
        ))

    # Step.3 流程默认, 布局与预算
    defaults_raw = spec.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise FlowDefinitionError("flow_definition_invalid", "params.flow.defaults 必须是对象")
    defaults: dict[str, FlowDefaultsEntry] = {}
    for type_name, entry in cast(dict[str, Any], defaults_raw).items():
        if not isinstance(entry, dict):
            raise FlowDefinitionError("flow_definition_invalid", f"defaults.{type_name} 必须是对象")
        entry_map = cast(dict[str, Any], entry)
        dversion = entry_map.get("version", 1)
        if not isinstance(dversion, int) or isinstance(dversion, bool) or dversion < 1:
            raise FlowDefinitionError("flow_definition_invalid", f"defaults.{type_name}.version 必须是正整数")
        dconfig = entry_map.get("config") or {}
        if not isinstance(dconfig, dict):
            raise FlowDefinitionError("flow_definition_invalid", f"defaults.{type_name}.config 必须是对象")
        defaults[str(type_name)] = FlowDefaultsEntry(version=dversion, config=dict(cast(dict[str, Any], dconfig)))

    layout_raw = spec.get("layout") or {}
    layout: dict[str, dict[str, float]] = {}
    if isinstance(layout_raw, dict):
        for node_id, pos in cast(dict[str, Any], layout_raw).items():
            if isinstance(pos, dict):
                pos_map = cast(dict[str, Any], pos)
                x, y = pos_map.get("x", 0), pos_map.get("y", 0)
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    layout[str(node_id)] = {"x": float(x), "y": float(y)}

    return FlowDefinition(
        version=version, nodes=nodes, edges=edges, defaults=defaults,
        layout=layout, budget=_parse_budget(spec.get("budget")),
    )


# ---------- 配置合并与迁移 ----------

def _migrate_config_dict(
    node_type: FlowNodeType,
    from_version: int,
    config: dict[str, Any],
    migrate_func: Callable[[str, int, dict[str, Any]], dict[str, Any]] | None,
    node_name: str,
) -> tuple[int, dict[str, Any]]:
    """
    对一处配置逐版本执行 renamed.fields 与 migrate()

    参数:
    - node_type: 目标节点契约
    - from_version: 记录版本, 缺失按 1
    - config: 待迁移配置
    - migrate_func: 插件的 migrate(节点名, 目标版本, config), 可为 None
    - node_name: 节点名 (传给 migrate)

    返回:
    - (迁移后版本, 迁移后配置); 版本高于契约时报 flow_node_version_ahead
    """
    if from_version > node_type.version:
        raise FlowDefinitionError("flow_node_version_ahead",
                                  f"配置版本 {from_version} 高于节点契约 {node_type.version} (插件可能被降级)")
    current = dict(config)
    for target in range(from_version + 1, node_type.version + 1):
        renamed = node_type.renamed.get(target)
        if renamed:
            for old, new in renamed.fields.items():
                if old in current:
                    current[new] = current.pop(old)
        if migrate_func is not None:
            current = dict(migrate_func(node_name, target, current))
    return node_type.version, current


def migrate_flow_definition(
    definition: FlowDefinition,
    registry: FlowNodeRegistry,
    plugin_defaults: dict[str, dict[str, dict[str, Any]]] | None = None,
    migrate_funcs: dict[str, Callable[[str, int, dict[str, Any]], dict[str, Any]] | None] | None = None,
) -> tuple[FlowDefinition, dict[str, dict[str, Any]], list[FlowDefinitionError]]:
    """
    对存档四处配置各自按版本迁移, 并迁移连线端口

    参数:
    - definition: parse_flow_definition 的结果
    - registry: 节点类型注册表
    - plugin_defaults: {插件名: {节点名: {version, config}}} 插件全局默认 (只读, 不改动)
    - migrate_funcs: {插件名: migrate 函数} (可选)

    返回:
    - (迁移后流程定义, 迁移后插件全局默认, 失败列表);
      任一位置迁移失败记录 FlowDefinitionError, 该位置保持原样, 流程判为不可运行
    """
    errors: list[FlowDefinitionError] = []
    new_nodes: dict[str, FlowNodeInstance] = {}
    new_defaults: dict[str, FlowDefaultsEntry] = {}
    new_plugin_defaults: dict[str, dict[str, dict[str, Any]]] = {}

    # Step.1 节点实例迁移
    for node in definition.nodes.values():
        node_type = registry.get(node.type)
        if node_type is None:
            new_nodes[node.id] = node   # 未知类型留给静态检查报 flow_unknown_node_type
            continue
        node_name = node.type.rsplit(".", 1)[-1]
        plugin = node_type.plugin
        try:
            version, config = _migrate_config_dict(
                node_type, node.version, node.config,
                (migrate_funcs or {}).get(plugin), node_name,
            )
            new_nodes[node.id] = FlowNodeInstance(
                id=node.id, type=node.type, version=version,
                title=node.title, config=config,
            )
        except FlowDefinitionError as error:
            errors.append(error)
            new_nodes[node.id] = node
        except Exception as error:
            errors.append(FlowDefinitionError("flow_node_config_stale",
                                              f"节点 {node.id} 的实例配置迁移失败: {error}", node=node.id))
            new_nodes[node.id] = node

    # Step.2 流程默认迁移
    for type_name, entry in definition.defaults.items():
        node_type = registry.get(type_name)
        if node_type is None:
            new_defaults[type_name] = entry
            continue
        try:
            version, config = _migrate_config_dict(
                node_type, entry.version, entry.config,
                (migrate_funcs or {}).get(node_type.plugin), type_name.rsplit(".", 1)[-1],
            )
            new_defaults[type_name] = FlowDefaultsEntry(version=version, config=config)
        except FlowDefinitionError as error:
            errors.append(error)
            new_defaults[type_name] = entry
        except Exception as error:
            errors.append(FlowDefinitionError("flow_node_config_stale",
                                              f"流程默认 {type_name} 迁移失败: {error}"))
            new_defaults[type_name] = entry

    # Step.3 插件全局默认迁移 (内存结果; 由调用方决定是否写回)
    for plugin_name, nodes_map in (plugin_defaults or {}).items():
        migrated_map: dict[str, dict[str, Any]] = {}
        for node_name, saved in nodes_map.items():
            type_name = f"{plugin_name}.{node_name}"
            node_type = registry.get(type_name)
            if node_type is None:
                migrated_map[node_name] = saved
                continue
            try:
                version, config = _migrate_config_dict(
                    node_type, int(saved.get("version", 1)), dict(saved.get("config") or {}),
                    (migrate_funcs or {}).get(plugin_name), node_name,
                )
                migrated_map[node_name] = {"version": version, "config": config}
            except FlowDefinitionError as error:
                errors.append(error)
                migrated_map[node_name] = saved
            except Exception as error:
                errors.append(FlowDefinitionError("flow_node_config_stale",
                                                  f"插件全局默认 {type_name} 迁移失败: {error}"))
                migrated_map[node_name] = saved
        new_plugin_defaults[plugin_name] = migrated_map

    # Step.4 连线端口迁移: 跟随所连节点实例的版本
    port_renames: dict[tuple[str, str], str] = {}
    for node in definition.nodes.values():
        migrated = new_nodes.get(node.id)
        node_type = registry.get(node.type)
        if node_type is None or migrated is None or migrated.version == node.version:
            continue
        for target in range(node.version + 1, migrated.version + 1):
            renamed = node_type.renamed.get(target)
            if renamed:
                for old, new in renamed.ports.items():
                    port_renames[(node.id, old)] = new
    new_edges = [
        FlowEdge(
            source=e.source,
            source_port=port_renames.get((e.source, e.source_port), e.source_port),
            target=e.target,
            target_port=port_renames.get((e.target, e.target_port), e.target_port),
            label=e.label,
        )
        for e in definition.edges
    ]

    migrated = FlowDefinition(
        version=definition.version, nodes=new_nodes, edges=new_edges,
        defaults=new_defaults, layout=definition.layout, budget=definition.budget,
    )
    return migrated, new_plugin_defaults, errors


def resolve_node_config(
    node_type: FlowNodeType,
    node: FlowNodeInstance | None,
    flow_defaults: dict[str, FlowDefaultsEntry],
    plugin_defaults: dict[str, dict[str, Any]] | None = None,
) -> ResolvedNodeConfig:
    """
    按 类型默认 < 插件全局默认 < 流程默认 < 本节点 合并一个节点的生效配置

    参数:
    - node_type: 节点契约
    - node: 节点实例, 只取 config; 纯类型查询时可传 None
    - flow_defaults: 流程默认表
    - plugin_defaults: {节点名: {version, config}} 该插件的全局默认

    返回:
    - ResolvedNodeConfig: 生效值, 每字段来源与继承预览
    """
    node_name = node_type.type.rsplit(".", 1)[-1]
    layers: list[tuple[ConfigSource, dict[str, Any]]] = [
        ("plugin", (plugin_defaults or {}).get(node_name, {}).get("config", {}) if node_type.plugin else {}),
        ("flow", (flow_defaults.get(node_type.type) or FlowDefaultsEntry(1, {})).config),
        ("node", node.config if node is not None else {}),
    ]
    values: dict[str, Any] = {}
    sources: dict[str, ConfigSource] = {}
    inherited: dict[str, Any] = {}
    for key, field_def in node_type.config_schema.items():
        values[key] = field_def.default
        sources[key] = "type"
        for source, layer in layers:
            if source == "node":
                inherited[key] = values[key]
            if key in layer:
                value = layer[key]
                if value is None and not field_def.nullable:
                    # null 在不可空字段上是校验报错, 不视为继承, 留待静态检查报 flow_config_invalid
                    values[key] = None
                else:
                    values[key] = value
                sources[key] = source
    return ResolvedNodeConfig(values=values, sources=sources, inherited=inherited)


def validate_node_config(
    node_type: FlowNodeType,
    node: FlowNodeInstance,
    resolved: ResolvedNodeConfig,
) -> list[FlowDefinitionError]:
    """
    校验一个节点实例的显式字段与生效值

    参数:
    - node_type: 节点契约
    - node: 节点实例
    - resolved: resolve_node_config 的结果

    返回:
    - list[FlowDefinitionError]: 问题列表, 空为通过
    """
    errors: list[FlowDefinitionError] = []
    for key in node.config:
        if key not in node_type.config_schema:
            errors.append(FlowDefinitionError("flow_config_unknown_field",
                                              f"{node.title} 有未声明的配置项 {key}", node=node.id, field=key))
    for key, field_def in node_type.config_schema.items():
        if key not in node.config:
            continue   # 未显式覆盖的字段按上层生效值, 由 schema 层校验
        raw = node.config[key]
        if raw is None and not field_def.nullable:
            errors.append(FlowDefinitionError("flow_config_invalid",
                                              f"{node.title}.{field_def.label or key} 不允许 null", node=node.id, field=key))
            continue
        try:
            field_def.validate_strict(raw)
        except ValueError as error:
            errors.append(FlowDefinitionError("flow_config_invalid",
                                              f"{node.title}.{field_def.label or key}: {error}", node=node.id, field=key))
    # 生效值整体再过一次宽松校验, 捕捉上层默认带下来的非法值
    for key, field_def in node_type.config_schema.items():
        value = resolved.values.get(key)
        if value is None:
            continue
        try:
            field_def.validate_strict(value)
        except ValueError as error:
            source = resolved.sources.get(key, "type")
            errors.append(FlowDefinitionError("flow_config_invalid",
                                              f"{node.title}.{field_def.label or key} 的生效值非法 ({source} 层): {error}",
                                              node=node.id, field=key))
    return errors
