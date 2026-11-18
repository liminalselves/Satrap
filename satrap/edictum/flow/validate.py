"""
流程静态检查: 结构, 端口, 逻辑, 触发, 状态槽, workflow, 配置, 成本与环境

移植 satrap-ui-preview/src/flow/validate.ts 的全部规则, 并加上后端独有的
插件目录/flow.workflow/版本迁移检查; 每条规则返回稳定 code 的问题列表
"""
from __future__ import annotations

from satrap.core.type.flow.definition import FlowDefinition, FlowEdge
from satrap.edictum.flow.analysis import analyze
from satrap.edictum.flow.definition import validate_node_config
from satrap.edictum.flow.definition import resolve_node_config as _resolve_node_config
from satrap.edictum.flow.graph import edge_key
from satrap.edictum.flow.registry import DEFAULT_SOURCE_MAX_MESSAGES, FlowNodeRegistry
from satrap.core.type.flow.registry import FlowNodeType
from satrap.core.type.plugin.catalog import PluginCatalogEntry

from dataclasses import dataclass, field
from typing import Any, Literal


from satrap.core.type.flow.validation import IssueLevel, IssueCategory, Issue, Origin, WfInfo, Analysis

MAX_NODES = 80


def _find_cycle(nodes: dict[str, Any], edges: list[FlowEdge], get_type) -> list[str] | None:
    """
    找环; 从状态槽出发的边不参与 (读取端读上一版, 不构成同次运行依赖)

    参数:
    - nodes: {id: FlowNodeInstance}
    - edges: 连线
    - get_type: 按类型名查契约

    返回:
    - 环上的节点 id 列表, 无环返回 None
    """
    is_state = {n.id for n in nodes.values() if (get_type(n.type) or FlowNodeType("", 0, "", "", "note")).role == "state"}
    active = [e for e in edges if e.source not in is_state]
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        state[node_id] = 1
        stack.append(node_id)
        for e in (x for x in active if x.source == node_id):
            s = state.get(e.target, 0)
            if s == 1:
                return stack[stack.index(e.target):]
            if s == 0:
                found = visit(e.target)
                if found:
                    return found
        state[node_id] = 2
        stack.pop()
        return None

    for node_id in nodes:
        if not state.get(node_id):
            found = visit(node_id)
            if found:
                return found
    return None


def validate(
    definition: FlowDefinition,
    registry: FlowNodeRegistry,
    *,
    plugin_catalog: dict[str, PluginCatalogEntry] | None = None,
    plugin_execution_enabled: dict[str, bool] | None = None,
    archive_enabled: bool = True,
    resolved_map: dict[str, dict[str, Any]] | None = None,
    conversation_kinds: list[str] | None = None,
    analysis: Analysis | None = None,
    scheduled_send_group: bool | None = None,
    scheduled_send_private: bool | None = None,
) -> list[Issue]:
    """
    对流程定义做静态检查

    参数:
    - definition: 流程定义 (应先经 migrate_flow_definition 迁移)
    - registry: 节点类型注册表
    - plugin_catalog: {插件名: PluginCatalogEntry} 用于环境检查 (未装/flow.workflow)
    - plugin_execution_enabled: {插件名: 执行总闸}; None 表示调用方未提供宿主状态
    - archive_enabled: 平台是否开启消息档案
    - resolved_map: 可选的预计算生效配置 {节点 id: {字段: 值}}; 缺省按 defaults 解析
    - conversation_kinds: 绑定的会话类型 ["group","private"], 用于私聊相关检查
    - analysis: 可选的预计算分析
    - scheduled_send_group / scheduled_send_private: 平台适配器的后台发送能力;
      None 表示未检测 (跳过 flow_background_send_unsupported 检查)

    返回:
    - list[Issue]: 全部问题, 顺序与 demo validate 对齐
    """
    out: list[Issue] = []
    add = out.append
    nodes = definition.nodes
    edges = definition.edges
    get_type = registry.get
    plugin_catalog = plugin_catalog or {}
    kinds = set(conversation_kinds or ["group"])

    resolved_map = resolved_map or {
        nid: _resolve_node_config(
            get_type(node.type), node, definition.defaults,
        ).values
        for nid, node in nodes.items() if get_type(node.type) is not None
    }
    cfg = lambda nid: resolved_map.get(nid, {})
    name = lambda nid: f'"{nodes[nid].title}"' if nid in nodes else nid
    a = analysis or analyze(definition, registry, resolved_map)
    wfs, pulled_by, edge_types, drives, cond_contexts = a.wfs, a.pulled_by, a.edge_types, a.drives, a.cond_contexts
    in_e = lambda nid, port=None: [e for e in edges if e.target == nid and (port is None or e.target_port == port)]
    out_e = lambda nid, port=None: [e for e in edges if e.source == nid and (port is None or e.source_port == port)]
    of_type = lambda t: [n for n in nodes.values() if n.type == t]
    role_of = lambda nid: (get_type(nodes[nid].type).role if nid in nodes and get_type(nodes[nid].type) else "")
    tool_name = lambda tile_id: str(cfg(tile_id).get("tool_name") or f"handoff_{tile_id}")

    # ---------- 结构 ----------
    for node in nodes.values():
        if get_type(node.type) is None:
            add(Issue(code="flow_unknown_node_type", level="error", category="结构", node=node.id,
                      message=f"{name(node.id)} 的类型 {node.type} 不存在", hint="插件可能已卸载"))
    if len(nodes) > MAX_NODES:
        add(Issue(code="flow_node_limit", level="error", category="结构",
                  message=f"节点数 {len(nodes)} 超过上限 {MAX_NODES}"))
    sources = of_type("source")
    if len(sources) != 1:
        add(Issue(code="flow_source_count", level="error", category="结构",
                  node=sources[1].id if len(sources) > 1 else "",
                  message=f"需要恰好一个对话消息入口, 当前 {len(sources)} 个"))
    if not wfs:
        add(Issue(code="flow_no_workflow", level="error", category="结构",
                  message="流程里没有 WF, 不会调用任何模型"))
    cycle = _find_cycle(nodes, edges, get_type)
    if cycle:
        add(Issue(code="flow_cycle", level="error", category="结构", node=cycle[0],
                  message=f"存在环: {' → '.join(name(nid) for nid in cycle)}",
                  hint="WF 互相触发会无限运行; 需要反复执行时用阈值或唤醒磁贴驱动"))
    for node in nodes.values():
        t = get_type(node.type)
        if t is None or t.role == "note":
            continue
        if not in_e(node.id) and not out_e(node.id):
            add(Issue(code="flow_isolated", level="warning", category="结构", node=node.id,
                      message=f"{name(node.id)} 没有任何连线"))
            continue
        if t.role in ("transform", "state") and out_e(node.id) and node.id not in pulled_by \
                and not any(role_of(e.target) in ("trigger", "condition") for e in out_e(node.id)):
            add(Issue(code="flow_unpulled", level="warning", category="结构", node=node.id,
                      message=f"{name(node.id)} 不在任何 WF 的数据输入链上, 永远不会被求值"))
        if t.role in ("transform", "condition", "logic", "trigger") and not out_e(node.id):
            add(Issue(code="flow_dead_end", level="warning", category="结构", node=node.id,
                      message=f"{name(node.id)} 的输出没有连到任何地方"))

    # ---------- 端口 ----------
    seen_pairs: set[str] = set()
    for e in edges:
        source_def = get_type(nodes[e.source].type) if e.source in nodes else None
        target_def = get_type(nodes[e.target].type) if e.target in nodes else None
        if source_def is None or target_def is None:
            continue
        sp = source_def.output_port(e.source_port)
        tp = target_def.input_port(e.target_port)
        if sp is None or tp is None:
            bad = f"{e.source}.{e.source_port}" if sp is None else f"{e.target}.{e.target_port}"
            add(Issue(code="flow_port_unknown", level="error", category="端口",
                      edge=edge_key(e), node=e.target,
                      message=f"连线引用了不存在的端口 {bad}", hint="节点类型升级后删除了该端口"))
            continue
        t_edge = edge_types.get(edge_key(e))
        if t_edge is not None and t_edge not in tp.types:
            add(Issue(code="flow_port_type_mismatch", level="error", category="端口",
                      edge=edge_key(e), node=e.target,
                      message=f"端口类型不匹配: {name(e.source)} 输出{t_edge}, {name(e.target)}.{tp.label} 只接受{'/'.join(tp.types)}",
                      hint="逻辑门没有触发输入时只输出条件; 给它接一个触发磁贴"
                      if sp.derived and t_edge == "condition" and "event" in tp.types else ""))
        pair = edge_key(e)
        if pair in seen_pairs:
            add(Issue(code="flow_duplicate_edge", level="warning", category="端口", edge=pair,
                      message=f"{name(e.source)} 到 {name(e.target)} 重复连线"))
        seen_pairs.add(pair)
    for node in nodes.values():
        t = get_type(node.type)
        if t is None:
            continue
        for p in t.inputs:
            count = len(in_e(node.id, p.name))
            if p.required and not count:
                add(Issue(code="flow_port_required", level="error", category="端口", node=node.id,
                          message=f"{name(node.id)} 的必需输入\"{p.label}\"没有连线"))
            if not p.multiple and count > 1:
                add(Issue(code="flow_port_multiple", level="error", category="端口", node=node.id,
                          message=f"{name(node.id)} 的输入\"{p.label}\"只能接一条线, 当前 {count} 条"))

    # ---------- 逻辑 ----------
    for node in nodes.values():
        t = get_type(node.type)
        if t is None or t.role != "logic":
            continue
        ins = [edge_types.get(edge_key(e)) for e in in_e(node.id)]
        events = sum(1 for x in ins if x == "event")
        conds = sum(1 for x in ins if x == "condition")
        if node.type == "or" and events and conds:
            add(Issue(code="flow_logic_or_mixed", level="error", category="逻辑", node=node.id,
                      message=f"{name(node.id)} 同时接了触发和条件, OR 的输入必须同类",
                      hint="要给触发加条件请用 AND"))
        if node.type == "and" and events > 1:
            win = cfg(node.id).get("window_seconds", 120)
            add(Issue(code="flow_logic_and_events", level="warning", category="逻辑",
                      node=node.id, field="window_seconds",
                      message=f"{name(node.id)} 的 A, B 都是触发, 只有两者在 {win} 秒内都发生才会触发",
                      hint="如果只想\"任一触发\", 改用 OR"))
        if len(in_e(node.id)) > 1 and len({(e.source, e.source_port) for e in in_e(node.id)}) == 1:
            add(Issue(code="flow_logic_same_input", level="warning", category="逻辑", node=node.id,
                      message=f"{name(node.id)} 的 A 和 B 接的是同一个输出, 结果和直接连线相同"))
        if node.type == "and" and not events and any(
            (p := get_type(nodes[e.target].type).input_port(e.target_port)) is not None
            and "event" in p.types and "condition" not in p.types
            for e in out_e(node.id) if e.target in nodes and get_type(nodes[e.target].type)
        ):
            add(Issue(code="flow_logic_no_event", level="error", category="逻辑", node=node.id,
                      message=f"{name(node.id)} 没有触发输入, 输出的是条件, 不能驱动 WF"))
    for cid, ctxs in cond_contexts.items():
        t = get_type(nodes[cid].type)
        if t is None or not t.needs:
            continue
        ctx_labels = {"message": "唤醒", "batch": "阈值", "call": "调用"}
        if ctxs and t.needs not in ctxs:
            add(Issue(code="flow_condition_context", level="warning", category="逻辑", node=cid,
                      message=f"{name(cid)} 需要单条消息, 但它所在的触发 ({'/'.join(ctx_labels.get(c, c) for c in ctxs)}) 没有单条消息, 恒为假"))
        if len(ctxs) > 1 and t.needs in ctxs:
            add(Issue(code="flow_condition_context_partial", level="warning", category="逻辑", node=cid,
                      message=f"{name(cid)} 在部分触发路径上没有单条消息, 那些路径上恒为假"))

    # ---------- 触发 ----------
    tool_owners: dict[str, str] = {}
    for node in nodes.values():
        t = get_type(node.type)
        if t is None or t.role != "trigger":
            continue
        v = cfg(node.id)
        driven = sorted(drives.get(node.id, set()))
        if out_e(node.id) and not driven:
            add(Issue(code="flow_trigger_no_target", level="warning", category="触发", node=node.id,
                      message=f"{name(node.id)} 的触发没有到达任何 WF"))
        if node.type == "wake":
            if not v.get("use_platform_policy") and not v.get("mention") \
                    and not str(v.get("wake_words") or "").strip() and not int(v.get("new_messages") or 0):
                add(Issue(code="flow_wake_never", level="error", category="触发", node=node.id, field="mention",
                          message=f"{name(node.id)} 自定义唤醒没有任何条件, 永远不会触发"))
            if not v.get("use_platform_policy") and not int(v.get("cooldown") or 0):
                add(Issue(code="flow_wake_cooldown_zero", level="warning", category="触发",
                          node=node.id, field="cooldown",
                          message=f"{name(node.id)} 冷却为 0, 可能被连续回复"))
            if not v.get("use_platform_policy") and int(v.get("new_messages") or 0) == 1:
                add(Issue(code="flow_wake_every_message", level="warning", category="成本",
                          node=node.id, field="new_messages",
                          message=f"{name(node.id)} 每条消息都会触发, 每次都调用 LLM"))
        if node.type == "threshold":
            if not int(v.get("messages") or 0) and not int(v.get("chars") or 0) and not int(v.get("idle_seconds") or 0):
                add(Issue(code="flow_threshold_empty", level="error", category="触发", node=node.id, field="messages",
                          message=f"{name(node.id)} 的阈值条件全部为 0, 永远不会触发"))
            if 0 < int(v.get("messages") or 0) <= 2:
                add(Issue(code="flow_threshold_low", level="warning", category="成本",
                          node=node.id, field="messages",
                          message=f"{name(node.id)} 每 {v.get('messages')} 条消息就触发一次, 活跃对话里每小时可能调用上百次"))
            if len(driven) > 1:
                add(Issue(code="flow_threshold_shared", level="warning", category="触发", node=node.id,
                          message=f"{name(node.id)} 驱动 {len(driven)} 个 WF, 各自推进自己的位置",
                          hint="任一 WF 失败只重试它自己, 其他 WF 不重跑"))
        if node.type == "tool":
            tool = tool_name(node.id)
            import re
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", tool):
                add(Issue(code="flow_tool_name_invalid", level="error", category="触发",
                          node=node.id, field="tool_name",
                          message=f"工具名 {tool} 只能用小写字母, 数字和下划线"))
            if tool in tool_owners:
                add(Issue(code="flow_tool_name_conflict", level="error", category="触发",
                          node=node.id, field="tool_name",
                          message=f"工具名 {tool} 已被 {name(tool_owners[tool])} 使用"))
            tool_owners[tool] = node.id
            if not str(v.get("tool_description") or "").strip():
                add(Issue(code="flow_tool_description_empty", level="warning", category="触发",
                          node=node.id, field="tool_description",
                          message=f"{name(node.id)} 没有工具说明, 调用方模型很难判断什么时候该调用"))

    for w in wfs.values():
        if not w.origins:
            add(Issue(code="flow_wf_no_trigger", level="error", category="触发", node=w.id,
                      message=f"{name(w.id)} 没有触发输入, 永远不会运行",
                      hint="在左侧连接一个触发磁贴, 或上游 WF 的\"完成\""))
        elif not w.roots:
            add(Issue(code="flow_wf_never_triggered", level="error", category="触发", node=w.id,
                      message=f"{name(w.id)} 的触发链上没有唤醒或阈值磁贴, 永远不会运行"))
        direct = {nodes[o.id].type for o in w.origins if o.kind == "tile" and o.id in nodes}
        if "wake" in direct and "threshold" in direct:
            add(Issue(code="flow_mixed_roots", level="warning", category="触发", node=w.id,
                      message=f"{name(w.id)} 同时被唤醒和阈值磁贴直接驱动, 前台和后台运行共用模型, 提示词和插件"))
        if w.background and w.replies_directly:
            add(Issue(code="flow_background_reply", level="error", category="触发", node=w.id,
                      message=f"{name(w.id)} 只在后台运行, 不能直接连到平台回复",
                      hint="需要主动发言时, 用交接工具让前台 WF 发送"))
        has_downstream = bool(out_e(w.id, "done")) or bool(w.handoff_tools)
        if w.background and not w.writes_state and not has_downstream:
            add(Issue(code="flow_background_no_state", level="error", category="触发", node=w.id,
                      message=f"{name(w.id)} 在后台运行但不写状态槽, 也不触发其他 WF, 每次 LLM 调用的结果都会被丢弃"))
    for w in wfs.values():
        if w.replies_directly and not w.background and "threshold" in w.roots:
            add(Issue(code="flow_proactive_reply", level="warning", category="成本", node=w.id,
                      message=f"{name(w.id)} 能被阈值链路 (经交接或完成) 触发并直接发言, 这类发言不经过唤醒磁贴的冷却",
                      hint="在该路径上加条件或调用预算, 或让模型自己判断是否开口"))
    replying_wake = [w for w in wfs.values()
                     if "wake" in w.roots and w.replies_directly
                     and any(o.kind == "tile" and nodes[o.id].type == "wake" for o in w.origins)]
    if len(replying_wake) > 1:
        add(Issue(code="flow_wake_multiple", level="warning", category="成本", node=replying_wake[1].id,
                  message=f"{', '.join(name(w.id) for w in replying_wake)} 都由唤醒磁贴驱动并能回复, 同一条消息可能各回一次",
                  hint="用条件或逻辑门区分它们各自处理哪些消息"))

    # ---------- 状态槽 ----------
    for s in of_type("state"):
        writers = in_e(s.id, "write")
        if len(writers) > 1:
            add(Issue(code="flow_state_multi_writer", level="error", category="状态槽", node=s.id,
                      message=f"{name(s.id)} 有 {len(writers)} 个写入方, 只允许一个"))
        if not writers:
            add(Issue(code="flow_state_no_writer", level="warning", category="状态槽", node=s.id,
                      message=f"{name(s.id)} 没有写入方, 内容始终为空"))
        sv = cfg(s.id)
        for w in writers:
            back = next((e for e in out_e(s.id) if e.target == w.source and e.target_port == "input"), None)
            if back and sv.get("writer_reads_previous"):
                add(Issue(code="flow_state_read_back_redundant", level="warning", category="状态槽",
                          edge=edge_key(back), node=s.id,
                          message=f"{name(s.id)} 已开启\"写入方读取上一版\", 连回 {name(w.source)} 的读线是重复的",
                          hint="删掉这条线即可"))
        if not out_e(s.id) and not sv.get("writer_reads_previous"):
            add(Issue(code="flow_state_no_reader", level="warning", category="状态槽", node=s.id,
                      message=f"{name(s.id)} 没有被任何节点读取"))
        writer = writers and wfs.get(writers[0].source)
        threshold_origins = {o.id for o in (writer.origins if writer else [])
                             if o.kind == "tile" and nodes.get(o.id) and nodes[o.id].type == "threshold"}
        if len(threshold_origins) > 1:
            add(Issue(code="flow_coverage_ambiguous", level="warning", category="状态槽", node=s.id,
                      message=f"{name(s.id)} 的写入方由 {len(threshold_origins)} 个阈值磁贴驱动, 各自游标不同, 覆盖位置取最大值",
                      hint="它们读的消息范围不同时, 拼装节点可能漏掉消息"))
        readers = [wfs[rid] for rid in pulled_by.get(s.id, set()) if rid in wfs]
        if writer and writer.background and any(not r.background for r in readers):
            add(Issue(code="flow_state_read_before_write", level="warning", category="状态槽", node=s.id,
                      message=f"后台 {name(writer.id)} 首次运行前, 前台读到的{name(s.id)}为空",
                      hint="这是预期行为, 请在读取方的提示词里说明快照可能为空"))
        if writer and not any(o.kind == "tile" and nodes[o.id].type == "threshold" for o in writer.origins) \
                and any(nodes.get(e.target) and nodes[e.target].type == "assemble" and cfg(e.target).get("after_coverage")
                        for e in out_e(s.id)):
            add(Issue(code="flow_coverage_without_cursor", level="warning", category="状态槽", node=s.id,
                      message=f"{name(s.id)} 的写入方不由阈值磁贴驱动, 版本不带覆盖位置",
                      hint="拼装节点的\"快照未覆盖的消息\"会退化为全部消息"))

    # ---------- workflow 与插件 ----------
    for w in wfs.values():
        v = w.values
        plugins_raw = v.get("plugins") or ""
        plugins = [p.strip() for p in str(plugins_raw).splitlines() if p.strip()] \
            if isinstance(plugins_raw, str) else [str(p) for p in plugins_raw]
        if not v.get("model_name"):
            add(Issue(code="flow_model_missing", level="error", category="workflow", node=w.id, field="model_name",
                      message=f"{name(w.id)} 没有可用模型 (本节点和流程默认都未设置)"))
        model_name = str(v.get("model_name") or "")
        needs_tools = any((plugin_catalog.get(p) and _workflow_plugin_tools(plugin_catalog[p])) for p in plugins) \
            or bool(w.handoff_tools)
        if w.writes_state and not any(_plugin_provides_by_name(p, plugin_catalog.get(p), "state_write") for p in plugins):
            add(Issue(code="flow_snapshot_plugin_missing", level="error", category="workflow", node=w.id, field="plugins",
                      message=f"{name(w.id)} 写入状态槽, 需要装提供 state_write 的插件 (如 flow_snapshot)"))
        if not w.writes_state and any(_plugin_provides_by_name(p, plugin_catalog.get(p), "state_write") for p in plugins):
            add(Issue(code="flow_snapshot_plugin_unused", level="warning", category="workflow", node=w.id, field="plugins",
                      message=f"{name(w.id)} 装了提供 state_write 的插件但没有连到状态槽"))
        tool_owner: dict[str, str] = {h["tool"]: f'交接工具 {name(h["tile"])}' for h in w.handoff_tools}
        for p in plugins:
            if p == "flow_snapshot":
                # 内置插件随宿主发布, 不在插件目录, 跳过未安装检查
                continue
            entry = plugin_catalog.get(p)
            if entry is None:
                add(Issue(code="flow_plugin_unavailable", level="error", category="workflow", node=w.id, field="plugins",
                          message=f"插件 {p} 未安装"))
                continue
            if _plugin_disabled(entry, plugin_execution_enabled):
                add(Issue(code="flow_plugin_unavailable", level="error", category="环境", node=w.id, field="plugins",
                          message=f"{name(w.id)} 上的插件 {entry.display_name or p} 已在插件管理中停用"))
            decl = entry.flow_workflow
            if decl is not None and decl.foreground_only and w.background:
                add(Issue(code="flow_plugin_placement", level="error", category="workflow", node=w.id, field="plugins",
                          message=f"{entry.display_name or p} 只能装在前台 WF 上, {name(w.id)} 只会在后台运行"))
            requires = entry.interop.get("requires") if isinstance(entry.interop, dict) else None
            if isinstance(requires, dict):
                for req, req_decl in requires.items():
                    # optional: true 的依赖不强制同装 (只生图不发送时不需要 group_chat);
                    # dict 声明必须显式 optional:false 才按必须算
                    needed = req_decl is True or (isinstance(req_decl, dict) and req_decl.get("optional") is False)
                    if needed and req not in plugins:
                        add(Issue(code="flow_plugin_dependency", level="error", category="workflow", node=w.id, field="plugins",
                                  message=f"{entry.display_name or p} 依赖 {req}, 需要装在同一个 WF 上"))
            for tname in _workflow_plugin_tools(entry):
                owner = tool_owner.get(tname)
                if owner:
                    add(Issue(code="flow_tool_name_conflict", level="error", category="workflow", node=w.id, field="plugins",
                              message=f"{owner} 和插件 {p} 都提供工具 {tname}"))
                else:
                    tool_owner[tname] = p
            if decl is not None and "memory_write" in decl.provides and w.background:
                add(Issue(code="flow_memory_write_background", level="warning", category="workflow", node=w.id, field="plugins",
                          message=f"后台 {name(w.id)} 装了长期记忆, 会在无人对话时写入记忆"))
        if not str(v.get("prompt") or "").strip():
            add(Issue(code="flow_prompt_empty", level="warning", category="workflow", node=w.id, field="prompt",
                      message=f"{name(w.id)} 系统提示词为空"))
        missing = [h["tool"] for h in w.handoff_tools if h["tool"] not in str(v.get("prompt") or "")]
        if missing:
            add(Issue(code="flow_prompt_missing_handoff", level="warning", category="workflow", node=w.id, field="prompt",
                      message=f"{name(w.id)} 的提示词没有提到交接工具 {', '.join(missing)}"))
        if v.get("keep_history") and w.pulls:
            add(Issue(code="flow_history_duplicate", level="warning", category="workflow", node=w.id, field="keep_history",
                      message=f"{name(w.id)} 保留对话历史, 同时每次都拉取桶消息, 历史会和输入重复"))
        if v.get("on_busy") == "drop" and any(o.kind == "tile" and nodes[o.id].type == "threshold" for o in w.origins):
            add(Issue(code="flow_busy_drop_batch", level="warning", category="workflow", node=w.id, field="on_busy",
                      message=f"{name(w.id)} 运行中再次触发时丢弃; 被丢弃的阈值批次不推进游标, 空闲后会再触发一次",
                      hint="后台 WF 通常用\"合并为一次重跑\""))
        if v.get("on_busy") == "drop" and "wake" in w.roots:
            add(Issue(code="flow_busy_drop", level="warning", category="workflow", node=w.id, field="on_busy",
                      message=f"{name(w.id)} 运行中再次触发时丢弃, 连续 @ 时后一条不会得到处理"))

    # ---------- 配置 ----------
    for node in nodes.values():
        t = get_type(node.type)
        if t is None:
            continue
        for error in validate_node_config(t, node, _config_obj(t, node, definition)):
            add(Issue(code=error.code, level="error", category="配置",
                      node=error.node, field=error.field, message=str(error)))
        if node.version != t.version:
            add(Issue(code="flow_config_stale", level="error", category="配置", node=node.id,
                      message=f"{name(node.id)} 创建于 v{node.version}, 节点类型已升级到 v{t.version} 且迁移未完成",
                      hint="在属性面板确认配置后保存"))
        if node.type == "state" and cfg(node.id).get("schema") == "sections" \
                and not any(x.strip() for x in str(cfg(node.id).get("sections") or "").split("\n")):
            add(Issue(code="flow_config_invalid", level="error", category="配置", node=node.id, field="sections",
                      message=f"{name(node.id)} 选择分段格式但没有段名"))
        if node.type == "cond_message" and cfg(node.id).get("kind") == "keyword" \
                and not str(cfg(node.id).get("keywords") or "").strip():
            add(Issue(code="flow_condition_empty", level="warning", category="逻辑", node=node.id, field="keywords",
                      message=f"{name(node.id)} 没有关键词, 恒为假"))
        if t.calls_model:
            if not cfg(node.id).get("model_name"):
                add(Issue(code="flow_plugin_model_missing", level="error", category="配置", node=node.id, field="model_name",
                          message=f"{name(node.id)} 需要插件模型, 本节点和插件默认都未设置"))
            pullers = ", ".join(name(pid) for pid in pulled_by.get(node.id, set())) or "拉取方"
            add(Issue(code="flow_plugin_calls_model", level="warning", category="成本", node=node.id,
                      message=f"{name(node.id)} 会调用插件模型 {cfg(node.id).get('model_name') or '(未设置)'}, 属于插件行为, 不计入流程的 LLM 触发",
                      hint=f"每次被 {pullers} 拉取时都可能调用"))
    for asm in of_type("assemble"):
        v = cfg(asm.id)
        for wid in pulled_by.get(asm.id, set()):
            snap_chars = sum(int(cfg(e.source).get("max_chars") or 0) for e in in_e(asm.id, "snapshot") if e.source in nodes)
            tokens = (int(v.get("max_pending_chars") or 0) if v.get("after_coverage") else 0 + snap_chars) / 1.5
            if model_name := str(wfs.get(wid).values.get("model_name") or ""):
                ctx_size = _model_ctx_size(model_name)
                if ctx_size and tokens > ctx_size * 0.6:
                    add(Issue(code="flow_assemble_budget", level="warning", category="配置",
                              node=asm.id, field="max_pending_chars",
                              message=f"输入预算约 {round(tokens):,} tokens, 超过 {name(wid)} 所用 {model_name} 上下文窗口的 60%"))
        if v.get("after_coverage") and int(v.get("latest_n") or 0) > int(v.get("max_pending") or 0):
            add(Issue(code="flow_assemble_latest_exceeds", level="warning", category="配置",
                      node=asm.id, field="latest_n",
                      message=f"最新 n 条 ({v.get('latest_n')}) 大于未覆盖消息上限 ({v.get('max_pending')})"))
        if v.get("after_coverage") and not in_e(asm.id, "snapshot"):
            add(Issue(code="flow_coverage_no_snapshot", level="warning", category="配置",
                      node=asm.id, field="after_coverage",
                      message=f"{name(asm.id)} 勾选了\"快照未覆盖的消息\", 但没有连接快照, 会取到全部消息"))
    # source/bucket 直连 WF 或自定义 transform 时必须有消息硬上限; 未显式配置时
    # 运行使用默认值, 但编辑器明确提示该连接受默认窗口约束。
    warned_source_limits: set[str] = set()
    for edge in definition.edges:
        source = nodes.get(edge.source)
        target = nodes.get(edge.target)
        if source is None or target is None or source.type not in {"source", "bucket"}:
            continue
        target_type = get_type(target.type)
        direct_consumer = (
            target.id in wfs and edge.target_port == "input"
            or target_type is not None and target_type.role == "transform"
            and target.type not in {"assemble", "bucket"}
        )
        if not direct_consumer or source.id in warned_source_limits:
            continue
        if "max_messages" not in source.config:
            warned_source_limits.add(source.id)
            add(Issue(code="flow_source_default_limit", level="warning", category="配置",
                      node=source.id, edge=edge_key(edge), field="max_messages",
                      message=f"{name(source.id)} 直连输入未显式设置 max_messages, "
                              f"运行时使用默认上限 {DEFAULT_SOURCE_MAX_MESSAGES} 条并截断较旧消息"))
    for o in of_type("output"):
        v = cfg(o.id)
        send_mode = str(v.get("send_mode") or "flow_reply")
        if send_mode == "group_chat_reply":
            # 只能被唤醒链路驱动的前台 WF; 后台或私聊时报错
            feeding = {w for e in in_e(o.id, "reply") for w in [e.source] if w in wfs}
            if "private" in kinds:
                add(Issue(code="flow_reply_mode_private", level="error", category="配置", node=o.id, field="send_mode",
                          message=f"{name(o.id)} 的 group_chat_reply 不能用于私聊会话"))
            if any(wfs[w].background for w in feeding):
                add(Issue(code="flow_reply_mode_background", level="error", category="配置", node=o.id, field="send_mode",
                          message=f"{name(o.id)} 的 group_chat_reply 需要来源回复轮次, 后台触发的 WF 没有",
                          hint="改用默认的 flow_reply, 或让后台 WF 通过交接工具交给前台 WF"))
            for wid in feeding:
                wv = wfs[wid].values
                wplugins = [p.strip() for p in str(wv.get("plugins") or "").splitlines() if p.strip()]
                if "group_chat" not in wplugins:
                    add(Issue(code="flow_plugin_dependency", level="error", category="配置", node=wid, field="plugins",
                              message=f"{name(wid)} 连到 group_chat_reply 回复节点, 需要装 group_chat"))

    # ---------- 环境 ----------
    if not archive_enabled:
        add(Issue(code="flow_archive_required", level="error", category="环境",
                  node=sources[0].id if sources else "",
                  message="绑定的平台没有启用消息档案, 桶无法读取消息",
                  hint="在平台管理中开启消息档案"))
    # 后台发送通道: 有 WF 可能被阈值/交接/完成驱动且连着平台回复,
    # 需要适配器支持后台发送; 不支持时给个提示 (不阻塞保存, 运行时该次发送失败)
    replying_background = [
        w for w in wfs.values()
        if w.replies_directly and (w.background or "threshold" in w.roots)
    ]
    if replying_background:
        kinds_list = sorted(kinds)
        unsupported = (
            ("group" in kinds_list and scheduled_send_group is False)
            or ("private" in kinds_list and scheduled_send_private is False)
        )
        if unsupported:
            w = replying_background[0]
            add(Issue(code="flow_background_send_unsupported", level="warning", category="环境",
                      node=w.id, field="send_mode",
                      message=f"{name(w.id)} 等 {len(replying_background)} 个 WF 可能被后台触发并发送, "
                              f"但当前平台适配器不支持后台主动发送",
                      hint="用 wake 磁贴驱动前台回复, 或换支持 scheduled send 的适配器"))
    for node in nodes.values():
        t = get_type(node.type)
        if t is None or not t.plugin:
            continue
        entry = plugin_catalog.get(t.plugin)
        if entry is None:
            add(Issue(code="flow_node_plugin_missing", level="error", category="环境", node=node.id,
                      message=f"提供{name(node.id)}节点的插件 {t.plugin} 未安装"))
        elif _plugin_disabled(entry, plugin_execution_enabled):
            add(Issue(code="flow_node_plugin_disabled", level="error", category="环境", node=node.id,
                      message=f"提供{name(node.id)}节点的插件 {t.plugin} 已停用, 流程不可运行"))
    # /new drain 有意义性检查
    for o in of_type("threshold"):
        if str(cfg(o.id).get("on_new") or "drop") != "drain":
            continue
        driven = [w for w in wfs.values() if any(org.kind == "tile" and org.id == o.id for org in w.origins)]
        cleared = False
        for w in driven:
            for sid in w.writes_state:
                if nodes.get(sid) and str(cfg(sid).get("on_new") or "keep") == "clear":
                    cleared = True
        if cleared:
            add(Issue(code="flow_new_drain_cleared", level="warning", category="触发", node=o.id, field="on_new",
                      message=f"{name(o.id)} 的 drain 会先把积压批次跑完, 但它驱动的 WF 所写的状态槽在 /new 时清空, 跑完即被覆盖",
                      hint="把状态槽的 on_new 改成 keep, 或把阈值磁贴改成 drop"))
    return out


def _workflow_plugin_tools(entry: PluginCatalogEntry) -> list[str]:
    """插件声明的 WF 可用工具名列表 (tools 能力声明的键)"""
    tools = entry.capabilities.get("tools") if isinstance(entry.capabilities, dict) else None
    return list(tools) if isinstance(tools, dict) else []


def _plugin_provides(entry: PluginCatalogEntry | None, capability: str) -> bool:
    """插件 flow.workflow.provides 是否声明了能力; 内置 flow_snapshot 视为提供 state_write"""
    if capability == "state_write" and entry is None:
        return False
    return entry is not None and entry.flow_workflow is not None and capability in entry.flow_workflow.provides


def _plugin_provides_by_name(plugin_name: str, entry: PluginCatalogEntry | None, capability: str) -> bool:
    """按名判断 provides; 内置 flow_snapshot 视为提供 state_write"""
    if capability == "state_write" and plugin_name == "flow_snapshot":
        return True
    return _plugin_provides(entry, capability)


def _plugin_disabled(entry: PluginCatalogEntry, execution_enabled: dict[str, bool] | None) -> bool:
    """按宿主执行总闸判断是否停用; 权限声明不携带运行开关"""
    if execution_enabled is None:
        return False
    return execution_enabled.get(entry.name, True) is False


def _model_ctx_size(model_name: str) -> int | None:
    """模型上下文长度表 (首版写死 demo 的表, 后续由模型配置提供)"""
    table = {
        "claude-sonnet-4.5": 200_000, "deepseek-v3": 64_000, "deepseek-r1": 64_000,
        "gpt-5-mini": 128_000, "qwen3-max": 32_000, "gemma-3-12b": 8_192,
    }
    return table.get(model_name)


def _config_obj(t: FlowNodeType, node: Any, definition: FlowDefinition):
    """供 validate_node_config 用的生效配置对象"""
    return _resolve_node_config(t, node, definition.defaults)
