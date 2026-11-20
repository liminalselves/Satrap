import type { FlowEdge, FlowNode, TypeIndex } from './flowModel';
import { toolName } from './flowModel';

/** 一个 WF 的结构信息, 只用于画布和检查器展示; 规则检查以后端为准 */
export interface WorkflowInfo {
  /** 被阈值链路 (含经交接或完成) 触发, 运行在后台 */
  background: boolean;
  /** 有唤醒或阈值根触发 */
  triggered: boolean;
  /** 运行时额外获得的交接工具名 */
  handoffTools: string[];
  /** 本 WF 写入的状态槽 id */
  writesState: string[];
}

/** 触发磁贴最终驱动到的 WF */
export type Drives = Map<string, Set<string>>;

export interface FlowAnalysis {
  /** WF 节点 id -> 结构信息 */
  workflows: Map<string, WorkflowInfo>;
  /** 触发磁贴 id -> 驱动的 WF id */
  drives: Drives;
}

/** 从触发链路倒推每个 WF 的根触发类型, 并收集交接工具与状态写入 */
export function analyzeFlow(nodes: FlowNode[], edges: FlowEdge[], types: TypeIndex): FlowAnalysis {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const role = (id: string) => types.get(byId.get(id)?.data.type ?? '')?.role;
  const incoming = (id: string, port?: string) => edges.filter((edge) => edge.target === id && (port === undefined || edge.targetHandle === port));

  // 沿信号边向上找根: wake / threshold; 经 WF done 或交接工具时继续向上
  const roots = (id: string, seen: Set<string>): Set<string> => {
    const out = new Set<string>();
    if (seen.has(id)) return out;
    seen.add(id);
    const type = byId.get(id)?.data.type;
    if (type === 'wake' || type === 'threshold') out.add(type);
    const kind = role(id);
    const ports = kind === 'workflow' ? ['trigger'] : kind === 'logic' ? ['a', 'b', 'in'] : type === 'tool' ? ['caller'] : [];
    for (const port of ports) {
      for (const edge of incoming(id, port)) {
        const sourceRole = role(edge.source);
        if (sourceRole === 'condition') continue;
        for (const item of roots(edge.source, seen)) out.add(item);
      }
    }
    return out;
  };

  const workflows = new Map<string, WorkflowInfo>();
  for (const node of nodes) {
    if (role(node.id) !== 'workflow') continue;
    const found = roots(node.id, new Set());
    const handoffTools = edges
      .filter((edge) => edge.source === node.id && edge.sourceHandle === 'tool')
      .map((edge) => byId.get(edge.target))
      .filter((target): target is FlowNode => Boolean(target && target.data.type === 'tool'))
      .map((target) => toolName(target.id, target.data.config));
    workflows.set(node.id, {
      background: found.has('threshold') && !found.has('wake'),
      triggered: found.size > 0,
      handoffTools,
      writesState: edges.filter((edge) => edge.source === node.id && edge.sourceHandle === 'text' && role(edge.target) === 'state').map((edge) => edge.target),
    });
  }

  // 触发磁贴向下沿信号边找到 WF
  const drives: Drives = new Map();
  const reach = (id: string, seen: Set<string>, out: Set<string>) => {
    if (seen.has(id)) return;
    seen.add(id);
    for (const edge of edges.filter((item) => item.source === id)) {
      const targetRole = role(edge.target);
      if (targetRole === 'workflow' && edge.targetHandle === 'trigger') out.add(edge.target);
      else if (targetRole === 'logic') reach(edge.target, seen, out);
    }
  };
  for (const node of nodes) {
    if (role(node.id) !== 'trigger') continue;
    const out = new Set<string>();
    reach(node.id, new Set(), out);
    drives.set(node.id, out);
  }
  return { workflows, drives };
}
