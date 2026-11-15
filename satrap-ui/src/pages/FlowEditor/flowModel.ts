import type { Edge, Node } from '@xyflow/react';
import type {
  EdictumPluginConfigField,
  FlowDefinitionJSON,
  FlowNodeTypePayload,
  FlowPortPayload,
  FlowPortType,
  FlowTypeMetadata,
} from '@/api/types';

/** 画布节点数据: 只保存显式设置的字段, 继承值在渲染时解析 */
export interface NodeData extends Record<string, unknown> {
  /** 节点类型名 */
  type: string;
  /** 节点契约版本 */
  version: number;
  /** 显示名 */
  title: string;
  /** 显式设置的配置字段 */
  config: Record<string, unknown>;
}
export type FlowNode = Node<NodeData, 'flow'>;

/** 画布边数据 */
export interface EdgeData extends Record<string, unknown> {
  /** 用户写的注释; 自动生成的读取说明不存档 */
  label?: string;
}
export type FlowEdge = Edge<EdgeData>;

/** 流程默认: 节点类型名 -> 字段值 */
export type FlowDefaults = Record<string, Record<string, unknown>>;
/** 插件全局默认: 完整节点类型名 ("插件名.节点名") -> 字段值 */
export type PluginDefaults = Record<string, Record<string, unknown>>;

/** 编辑器内的完整流程: 画布状态加上不在画布上的存档字段 */
export interface FlowDraft {
  /** 画布节点 */
  nodes: FlowNode[];
  /** 画布边 */
  edges: FlowEdge[];
  /** 流程默认 */
  defaults: FlowDefaults;
  /** 硬预算; null 表示不限 */
  budget: FlowDefinitionJSON['budget'];
}

/** 节点类型查询表 */
export type TypeIndex = Map<string, FlowNodeTypePayload>;

export const ARCHIVE_VERSION = 3;

export function indexTypes(meta: FlowTypeMetadata | undefined): TypeIndex {
  return new Map((meta?.node_types ?? []).map((item) => [item.type, item]));
}

/** 边 id 与后端问题里的 edge 字段同格式: "源.端口->目标.端口" */
export const edgeId = (source: string, sourceHandle: string, target: string, targetHandle: string) =>
  `${source}.${sourceHandle}->${target}.${targetHandle}`;

/** 端点 "节点.端口" 按最后一个点拆分; 插件节点 id 可能含点 */
export function splitEndpoint(endpoint: string): [string, string] {
  const index = endpoint.lastIndexOf('.');
  if (index <= 0 || index === endpoint.length - 1) throw new Error(`连线端点格式无效: ${endpoint}`);
  return [endpoint.slice(0, index), endpoint.slice(index + 1)];
}

/** 未保存位置的节点按顺序排成网格, 避免全部叠在原点 */
function fallbackPosition(index: number) {
  return { x: (index % 4) * 280, y: Math.floor(index / 4) * 200 };
}

export function toReactFlow(definition: FlowDefinitionJSON, types: TypeIndex): FlowDraft {
  const layout = definition.layout ?? {};
  const nodes: FlowNode[] = definition.nodes.map((item, index) => {
    const position = layout[item.id];
    return {
      id: item.id,
      type: 'flow',
      position: position && Number.isFinite(position.x) && Number.isFinite(position.y)
        ? { x: position.x, y: position.y } : fallbackPosition(index),
      data: {
        type: item.type,
        version: item.version ?? types.get(item.type)?.version ?? 1,
        title: item.title || item.id,
        config: { ...(item.config ?? {}) },
      },
    };
  });
  const edges: FlowEdge[] = (definition.edges ?? []).map((item) => {
    const [source, sourceHandle] = splitEndpoint(item.from);
    const [target, targetHandle] = splitEndpoint(item.to);
    return {
      id: edgeId(source, sourceHandle, target, targetHandle),
      source, sourceHandle, target, targetHandle,
      data: item.label ? { label: item.label } : {},
    };
  });
  const defaults: FlowDefaults = {};
  for (const [type, entry] of Object.entries(definition.defaults ?? {})) {
    if (entry?.config && Object.keys(entry.config).length) defaults[type] = { ...entry.config };
  }
  return { nodes, edges, defaults, budget: definition.budget ?? null };
}

/**
 * 画布状态转回 params.flow
 *
 * 只写后端认识的字段; 节点 config 只含显式设置的值; 流程默认的版本取当前节点契约版本
 */
export function toDefinition(draft: FlowDraft, types: TypeIndex): FlowDefinitionJSON {
  const defaults: NonNullable<FlowDefinitionJSON['defaults']> = {};
  for (const [type, config] of Object.entries(draft.defaults)) {
    if (!Object.keys(config).length) continue;
    defaults[type] = { version: types.get(type)?.version ?? 1, config: { ...config } };
  }
  const out: FlowDefinitionJSON = {
    version: ARCHIVE_VERSION,
    nodes: draft.nodes.map((node) => ({
      id: node.id,
      type: node.data.type,
      version: node.data.version,
      title: node.data.title,
      config: { ...node.data.config },
    })),
    edges: draft.edges.map((edge) => ({
      from: `${edge.source}.${edge.sourceHandle}`,
      to: `${edge.target}.${edge.targetHandle}`,
      ...(edge.data?.label ? { label: edge.data.label } : {}),
    })),
    defaults,
    layout: Object.fromEntries(draft.nodes.map((node) => [node.id, { x: Math.round(node.position.x), y: Math.round(node.position.y) }])),
  };
  if (draft.budget) out.budget = draft.budget;
  return out;
}

/** 字段值来源, 顺序即合并顺序 */
export type Source = 'type' | 'plugin' | 'flow' | 'node';
export const SOURCE_LABEL: Record<Source, string> = { type: '类型默认', plugin: '插件全局', flow: '流程默认', node: '本节点' };

/** 节点配置的解析结果 */
export interface Resolved {
  /** 生效值 */
  values: Record<string, unknown>;
  /** 每个字段生效值的来源层 */
  sources: Record<string, Source>;
  /** 去掉本节点层之后的值, 用于"恢复继承"预览和渲染器的继承值 */
  inherited: Record<string, unknown>;
}

/** 与后端 resolve_node_config 同顺序: 类型默认 < 插件全局 < 流程默认 < 节点 */
export function resolveConfig(
  data: NodeData, types: TypeIndex, flowDefaults: FlowDefaults, pluginDefaults: PluginDefaults,
): Resolved {
  const def = types.get(data.type);
  const values: Record<string, unknown> = {};
  const sources: Record<string, Source> = {};
  const inherited: Record<string, unknown> = {};
  if (!def) return { values: { ...data.config }, sources, inherited };
  const layers: Array<[Source, Record<string, unknown>]> = [
    ['plugin', def.plugin ? pluginDefaults[def.type] ?? {} : {}],
    ['flow', flowDefaults[data.type] ?? {}],
  ];
  for (const [key, field] of Object.entries(def.schema)) {
    values[key] = field.default;
    sources[key] = 'type';
    for (const [source, layer] of layers) {
      if (Object.prototype.hasOwnProperty.call(layer, key)) {
        values[key] = layer[key];
        sources[key] = source;
      }
    }
    inherited[key] = values[key];
    if (Object.prototype.hasOwnProperty.call(data.config, key)) {
      values[key] = data.config[key];
      sources[key] = 'node';
    }
  }
  return { values, sources, inherited };
}

export const portSide = (port: FlowPortPayload, io: 'in' | 'out'): 'top' | 'bottom' | 'left' | 'right' =>
  port.side ?? (io === 'in' ? 'top' : 'bottom');

/** 输出端口的实际类型: 逻辑门 derived 输出在有触发输入时为 event, 否则 condition */
export function outputType(
  nodeId: string, port: string, nodes: FlowNode[], edges: FlowEdge[], types: TypeIndex, seen = new Set<string>(),
): FlowPortType | null {
  const node = nodes.find((item) => item.id === nodeId);
  const def = node && types.get(node.data.type);
  const spec = def?.outputs.find((item) => item.name === port);
  if (!spec) return null;
  if (!spec.derived) return spec.types[0] ?? null;
  if (seen.has(nodeId)) return 'condition';
  seen.add(nodeId);
  const inputs = edges.filter((edge) => edge.target === nodeId)
    .map((edge) => outputType(edge.source, edge.sourceHandle ?? '', nodes, edges, types, seen));
  return inputs.includes('event') ? 'event' : 'condition';
}

export const toolName = (id: string, values: Record<string, unknown>) => String(values.tool_name || `handoff_${id}`);

/** WF 节点插件字段: 后端存为每行一个插件名的字符串 */
export const parsePluginList = (value: unknown): string[] =>
  String(value ?? '').split('\n').map((item) => item.trim()).filter(Boolean);
export const formatPluginList = (names: string[]) => names.join('\n');

/** 连线读取说明: 由接收端节点的类型和配置生成, 不可编辑; owner 是决定读取方式的节点 */
export interface EdgeReading {
  /** 说明文字 */
  text: string;
  /** 决定读取方式的节点 id */
  owner: string;
}

/** 会话会用在哪些对话类型上; 影响唤醒等读取说明和后端检查规则 */
export type ConversationKinds = Array<'group' | 'private'>;

/** 沿用平台策略时的唤醒说明: 群聊按群策略, 私聊每条都唤醒 */
export function platformWakeText(kinds: ConversationKinds): string {
  const group = kinds.includes('group'), dm = kinds.includes('private');
  if (group && dm) return '群聊按群策略, 私聊每条唤醒';
  return dm ? '私聊每条唤醒' : '沿用群策略';
}

export const SEND_MODE_READING: Record<string, string> = {
  flow_reply: '工具发送, 可沉默',
  group_chat_reply: '群聊富消息 (仅前台)',
  final_text: '直发最终文本',
};

export function edgeReading(
  edge: FlowEdge, nodes: FlowNode[], edges: FlowEdge[], types: TypeIndex,
  flowDefaults: FlowDefaults, pluginDefaults: PluginDefaults, kinds: ConversationKinds = ['group'],
): EdgeReading | null {
  const byId = (id: string) => nodes.find((node) => node.id === id);
  const src = byId(edge.source);
  const dst = byId(edge.target);
  if (!src || !dst) return null;
  const sd = types.get(src.data.type);
  const td = types.get(dst.data.type);
  if (!sd || !td) return null;
  const v = resolveConfig(dst.data, types, flowDefaults, pluginDefaults).values;
  const lines = (value: unknown) => String(value ?? '').split('\n').filter((item) => item.trim()).length;
  const at = (text: string, owner = dst.id): EdgeReading => ({ text, owner });
  const port = edge.targetHandle ?? '';

  // 插件节点声明的读取说明优先
  if (td.reading?.[port]) return at(td.reading[port]);
  if (td.role === 'workflow' && port === 'trigger') {
    if (sd.role === 'workflow') return at('带上游最终输出', src.id);
    if (src.data.type === 'threshold') return at('带本批消息', src.id);
    if (src.data.type === 'tool') return at('带工具参数', src.id);
    if (src.data.type === 'wake') return at('带触发消息', src.id);
    return null;
  }
  if (td.role === 'workflow' && port === 'input') {
    if (sd.role === 'state') return at('读上一次提交的版本');
    if (src.data.type === 'source' || src.data.type === 'bucket') {
      const limit = resolveConfig(src.data, types, flowDefaults, pluginDefaults).values.max_messages;
      return at(`最新 ${limit ?? 200} 条`, src.id);
    }
    return null;
  }
  if (port === 'caller') return at(`获得工具 ${toolName(dst.id, v)}`);
  switch (`${dst.data.type}.${port}`) {
    case 'bucket.in': return at(v.include_outbound ? '入站 + 机器人已发出' : '仅入站消息');
    case 'threshold.messages': {
      const rule = [
        Number(v.messages) > 0 && `${v.messages} 条`,
        Number(v.chars) > 0 && `${v.chars} 字`,
        Number(v.idle_seconds) > 0 && `${Math.round(Number(v.idle_seconds) / 60)} 分钟`,
      ].filter(Boolean).join(' 或 ');
      return at(`位置后增量 · ${rule || '无阈值'}`);
    }
    case 'wake.messages':
      return at(v.use_platform_policy ? `每条入站消息 · ${platformWakeText(kinds)}`
        : `每条入站消息 · ${[v.mention && '@', Number(v.new_messages) > 0 && `${v.new_messages} 条`, lines(v.wake_words) > 0 && '唤醒词'].filter(Boolean).join(' / ') || '无规则'}`);
    case 'assemble.messages': {
      const parts = [v.after_coverage && `快照覆盖后 ≤${v.max_pending} 条`, Number(v.latest_n) > 0 && `最新 ${v.latest_n} 条`].filter(Boolean);
      return at(parts.join(' + ') || '不读消息');
    }
    case 'assemble.snapshot': return at('最新已提交版本');
    case 'state.write': {
      const byThreshold = edges.some((item) => item.target === src.id && item.targetHandle === 'trigger' && byId(item.source)?.data.type === 'threshold');
      return at(`${v.writer_reads_previous ? '读 vN, 写 vN+1' : '写 v+1'}${byThreshold ? ' 与覆盖位置' : ''}`);
    }
    case 'cond_state.state': return at('读版本');
    case 'output.reply': return at(SEND_MODE_READING[String(v.send_mode)] ?? String(v.send_mode));
  }
  return null;
}

/** 新建节点的数据 */
export function newNodeData(def: FlowNodeTypePayload, title?: string): NodeData {
  return { type: def.type, version: def.version, title: title ?? def.label, config: {} };
}

/** 生成流程内唯一的节点 id; 插件类型名里的点换成下划线, 便于阅读 */
export function newNodeId(type: string, existing: Set<string>): string {
  const base = type.replace(/\./g, '_');
  for (let index = 1; ; index += 1) {
    const id = index === 1 ? base : `${base}_${index}`;
    if (!existing.has(id)) return id;
  }
}

/** 字段是否被 schema 声明 (用于剔除过期字段) */
export const declared = (schema: Record<string, EdictumPluginConfigField>, key: string) =>
  Object.prototype.hasOwnProperty.call(schema, key);
