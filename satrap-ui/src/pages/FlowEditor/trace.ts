import type { FlowEdge, FlowNode, TypeIndex } from './flowModel';
import { outputType } from './flowModel';

/** 这一步怎样激活链路里的下一步 */
export type TraceLink = 'event' | 'done' | 'handoff' | 'reply';

/** 激活链路上的一步; 链路按从根触发到追踪目标的顺序排列 */
export interface TraceStep {
  /** 节点 id */
  id: string;
  /** root 根触发, workflow WF, handoff 交接工具, gate 逻辑门, target 平台回复, dead 断开或成环 */
  kind: 'root' | 'workflow' | 'handoff' | 'gate' | 'target' | 'dead';
  /** 连到下一步的方式; 最后一步没有 */
  link?: TraceLink;
  /** 直接接在这一步上的条件节点 id: 磁贴和交接工具的 when 口, 逻辑门的条件输入 */
  conditions: string[];
  /** AND 门有多个触发输入: 要在时间窗内都到达才放行 */
  joinAll?: boolean;
}

/** 一条从根触发到目标的激活链路 */
export interface TracePath {
  /** 链路上的节点, 根在前 */
  steps: TraceStep[];
  /** 链路经过的边 id, 不含条件边 */
  edges: string[];
  /** 根触发的节点类型 (wake / threshold / 插件触发); null 表示链路断开, 不会被触发 */
  root: string | null;
  /** 链路经过交接工具, 是否激活由调用方模型决定 */
  modelDecides: boolean;
  /** 链路上的条件节点及其上游条件, 用于高亮 */
  conditionNodes: string[];
  /** 条件节点之间和接到链路上的边 */
  conditionEdges: string[];
}

/** 一个节点的全部激活链路 */
export interface ActivationTrace {
  /** 追踪目标节点 id */
  target: string;
  /** 全部链路; 断开的排在最后 */
  paths: TracePath[];
  /** 全部链路和条件涉及的节点 */
  nodes: Set<string>;
  /** 全部链路和条件涉及的边 */
  edges: Set<string>;
  /** 链路数超过上限, 只列出了前面一部分 */
  truncated: boolean;
}

/** 链路条数上限; 扇入很多时避免组合爆炸 */
export const MAX_TRACE_PATHS = 64;

/** 能追踪的节点: 平台回复, WF, 触发磁贴, 以及输出触发的逻辑门 (只输出条件的门没有激活链路) */
export function traceable(id: string, nodes: FlowNode[], edges: FlowEdge[], types: TypeIndex): boolean {
  const node = nodes.find((item) => item.id === id);
  const role = node && types.get(node.data.type)?.role;
  if (role === 'logic') return outputType(id, 'out', nodes, edges, types) === 'event';
  return role === 'sink' || role === 'workflow' || role === 'trigger';
}

const LINKS: Record<string, TraceLink> = { done: 'done', tool: 'handoff', text: 'reply' };

/**
 * 从目标节点沿触发边倒推全部激活链路
 *
 * 平台回复向上找连到它的 WF; WF 找 trigger 口; 交接工具找调用方 WF; WF 完成也继续向上;
 * 逻辑门的触发输入继续向上, 条件输入记为这一步的条件; 唤醒, 阈值等根触发处结束
 */
export function traceActivation(target: string, nodes: FlowNode[], edges: FlowEdge[], types: TypeIndex): ActivationTrace {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const typeOf = (id: string) => types.get(byId.get(id)?.data.type ?? '');
  const into = (id: string, port?: string) => edges.filter((edge) => edge.target === id && (port === undefined || edge.targetHandle === port));
  const isEvent = (edge: FlowEdge) => outputType(edge.source, edge.sourceHandle ?? '', nodes, edges, types) === 'event';
  const paths: TracePath[] = [];
  let truncated = false;

  /** 收集接在 ports 上的条件和它们的上游条件 */
  const conditionsOf = (incoming: FlowEdge[]) => {
    const direct: string[] = [];
    const found = new Set<string>(), via = new Set<string>();
    const climb = (id: string) => {
      if (found.has(id)) return;
      found.add(id);
      const role = typeOf(id)?.role;
      if (role !== 'condition' && role !== 'logic') return;
      for (const edge of into(id)) { via.add(edge.id); climb(edge.source); }
    };
    for (const edge of incoming) {
      direct.push(edge.source);
      via.add(edge.id);
      climb(edge.source);
    }
    return { direct, nodes: [...found], edges: [...via] };
  };

  /** 走到 id; after 是已经走过的下游部分 */
  const walk = (id: string, link: TraceLink | undefined, after: TracePath, seen: Set<string>) => {
    if (paths.length >= MAX_TRACE_PATHS) { truncated = true; return; }
    const def = typeOf(id);
    const node = byId.get(id);
    const prepend = (step: TraceStep, cond?: ReturnType<typeof conditionsOf>): TracePath => ({
      ...after,
      steps: [step, ...after.steps],
      conditionNodes: [...(cond?.nodes ?? []), ...after.conditionNodes],
      conditionEdges: [...(cond?.edges ?? []), ...after.conditionEdges],
    });
    const finish = (path: TracePath, root: string | null) => {
      paths.push({ ...path, root, modelDecides: path.steps.some((step) => step.kind === 'handoff') });
    };
    if (!def || !node || seen.has(id)) {
      finish(prepend({ id, kind: 'dead', link, conditions: [] }), null);
      return;
    }
    const next = new Set(seen).add(id);
    const climb = (path: TracePath, incoming: FlowEdge[]) => {
      if (!incoming.length) { finish(path, null); return; }
      for (const edge of incoming) {
        walk(edge.source, LINKS[edge.sourceHandle ?? ''] ?? 'event', { ...path, edges: [edge.id, ...path.edges] }, next);
      }
    };

    if (def.role === 'sink') {
      climb(prepend({ id, kind: 'target', link, conditions: [] }), into(id, 'reply'));
    } else if (def.role === 'workflow') {
      climb(prepend({ id, kind: 'workflow', link, conditions: [] }), into(id, 'trigger'));
    } else if (def.role === 'trigger') {
      const cond = conditionsOf(into(id, 'when'));
      if (node.data.type === 'tool') {
        climb(prepend({ id, kind: 'handoff', link, conditions: cond.direct }, cond), into(id, 'caller'));
      } else {
        finish(prepend({ id, kind: 'root', link, conditions: cond.direct }, cond), node.data.type);
      }
    } else if (def.role === 'logic') {
      const incoming = into(id);
      const events = incoming.filter(isEvent);
      const cond = conditionsOf(incoming.filter((edge) => !isEvent(edge)));
      const step: TraceStep = { id, kind: 'gate', link, conditions: cond.direct, ...(node.data.type === 'and' && events.length > 1 ? { joinAll: true } : {}) };
      climb(prepend(step, cond), events);
    } else {
      finish(prepend({ id, kind: 'dead', link, conditions: [] }), null);
    }
  };

  walk(target, undefined, { steps: [], edges: [], root: null, modelDecides: false, conditionNodes: [], conditionEdges: [] }, new Set());
  paths.sort((a, b) => Number(a.root === null) - Number(b.root === null));
  const all = { nodes: new Set<string>(), edges: new Set<string>() };
  for (const path of paths) {
    for (const step of path.steps) all.nodes.add(step.id);
    for (const id of path.conditionNodes) all.nodes.add(id);
    for (const id of [...path.edges, ...path.conditionEdges]) all.edges.add(id);
  }
  return { target, paths, nodes: all.nodes, edges: all.edges, truncated };
}

/** 一条链路要高亮的节点和边 */
export function pathHighlight(path: TracePath): { nodes: Set<string>; edges: Set<string> } {
  return {
    nodes: new Set([...path.steps.map((step) => step.id), ...path.conditionNodes]),
    edges: new Set([...path.edges, ...path.conditionEdges]),
  };
}
