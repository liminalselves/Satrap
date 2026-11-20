import type { FlowDefinitionJSON } from '@/api/types';

/** 新建流程时可选的起点 */
export interface FlowTemplate {
  /** 模板 id */
  id: string;
  /** 显示名 */
  label: string;
  /** 用途说明 */
  description: string;
  /** 模板依赖的插件节点所属插件; 未安装时从模板中省略这些节点 */
  nodePlugins: string[];
  /** 生成存档; model 为默认模型配置名, 空串表示让用户自己选 */
  build: (model: string) => FlowDefinitionJSON;
}

type NodeSpec = { id: string; type: string; title: string; version: number; config?: Record<string, unknown>; x: number; y: number };

function assemble(nodes: NodeSpec[], edges: Array<[string, string, string?]>, model: string): FlowDefinitionJSON {
  return {
    version: 3,
    nodes: nodes.map(({ id, type, title, version, config }) => ({ id, type, version, title, config: { ...(config ?? {}) } })),
    edges: edges.map(([from, to, label]) => ({ from, to, ...(label ? { label } : {}) })),
    defaults: model ? { react_workflow: { version: 3, config: { model_name: model } } } : {},
    layout: Object.fromEntries(nodes.map((item) => [item.id, { x: item.x, y: item.y }])),
  };
}

const AGENT_PROMPT = `你负责维护这个群的"理解快照"。触发时你会收到上一版快照和新到的一批群消息。
输出一份完整的新快照, 包括: 当前话题, 成员近况与关系, 需要长期记住的要点。
只用 snapshot_write 写入结果, 不要在群里发言。`;

const RP_PROMPT = `你是群里的一员"小萨"。你会看到群理解快照, 快照未覆盖的新消息和最近的对话。
先判断这次是否需要开口: 被 @ 或话题和你有关时回复, 否则保持沉默。
需要回复时调用 flow_reply, 不回复时直接结束。`;

export const TEMPLATES: FlowTemplate[] = [
  {
    id: 'agent_rp', label: '群聊 Agent + RP', nodePlugins: [],
    description: '阈值磁贴驱动后台 Agent 更新快照, 唤醒磁贴驱动前台 RP 读取快照回复',
    build: (model) => assemble([
      { id: 'source', type: 'source', version: 1, title: '消息到达', x: 400, y: 0 },
      { id: 'bucket', type: 'bucket', version: 2, title: '桶', x: 400, y: 150 },
      { id: 'th', type: 'threshold', version: 1, title: 'Agent 阈值', config: { messages: 12, chars: 1500 }, x: 0, y: 330 },
      { id: 'agent', type: 'react_workflow', version: 3, title: 'Agent', config: { max_steps: 12, prompt: AGENT_PROMPT, plugins: 'flow_snapshot', on_busy: 'merge' }, x: 260, y: 330 },
      { id: 'snapshot', type: 'state', version: 2, title: 'Agent 输出快照', config: { schema: 'sections', sections: '群内理解\n成员近况\n长期要点' }, x: 260, y: 540 },
      { id: 'rp_input', type: 'assemble', version: 2, title: 'RP 输入', x: 640, y: 540 },
      { id: 'wake', type: 'wake', version: 1, title: 'RP 唤醒', x: 380, y: 730 },
      { id: 'rp', type: 'react_workflow', version: 3, title: 'RP', config: { prompt: RP_PROMPT }, x: 640, y: 730 },
      { id: 'reply', type: 'output', version: 2, title: '最后回复', x: 640, y: 940 },
      { id: 'note1', type: 'note', version: 1, title: '说明', config: { text: 'Agent 只在后台更新快照;\nRP 被唤醒时先用旧快照立即回复,\n不等 Agent。' }, x: -40, y: 540 },
    ], [
      ['source.out', 'bucket.in'], ['bucket.out', 'th.messages'], ['th.event', 'agent.trigger'],
      ['agent.text', 'snapshot.write'], ['snapshot.read', 'rp_input.snapshot'], ['bucket.out', 'rp_input.messages'],
      ['bucket.out', 'wake.messages'], ['wake.event', 'rp.trigger'], ['rp_input.context', 'rp.input'], ['rp.text', 'reply.reply'],
    ], model),
  },
  {
    id: 'logic', label: '带条件的 Agent + RP', nodePlugins: ['quiet_hours', 'llm_budget'],
    description: '夜间不更新快照; RP 在免打扰时段只回应 @, 且受调用预算限制; Agent 可用交接工具让 RP 提醒群里',
    build: (model) => assemble([
      { id: 'source', type: 'source', version: 1, title: '消息到达', x: 520, y: 0 },
      { id: 'bucket', type: 'bucket', version: 2, title: '桶', x: 520, y: 140 },
      { id: 'daytime', type: 'cond_time', version: 1, title: '白天', config: { start: '08:00', end: '23:00' }, x: -260, y: 300 },
      { id: 'th', type: 'threshold', version: 1, title: 'Agent 阈值', config: { messages: 12, chars: 1500 }, x: 0, y: 300 },
      { id: 'agent', type: 'react_workflow', version: 3, title: 'Agent', config: {
        max_steps: 12, plugins: 'flow_snapshot', on_busy: 'merge',
        prompt: `${AGENT_PROMPT}\n发现群里约定的事项快到时间了, 调用 remind_group 让 RP 提醒大家。`,
      }, x: 260, y: 300 },
      { id: 'snapshot', type: 'state', version: 2, title: 'Agent 输出快照', config: { schema: 'sections', sections: '群内理解\n成员近况\n长期要点' }, x: 260, y: 510 },
      { id: 'remind', type: 'tool', version: 1, title: '提醒群里', config: { tool_name: 'remind_group', tool_description: '群里约定的事项快到时间时调用, 参数写明要提醒的内容' }, x: 560, y: 330 },
      { id: 'wake', type: 'wake', version: 1, title: 'RP 唤醒', x: 560, y: 640 },
      { id: 'quiet', type: 'quiet_hours.in_hours', version: 1, title: '免打扰时段内', x: 300, y: 760 },
      { id: 'not_quiet', type: 'not', version: 1, title: '不在免打扰', x: 560, y: 790 },
      { id: 'mention', type: 'cond_message', version: 1, title: '被 @', x: 300, y: 870 },
      { id: 'or_ok', type: 'or', version: 2, title: '可以开口', x: 760, y: 830 },
      { id: 'budget', type: 'llm_budget.within', version: 1, title: '调用预算内', x: 760, y: 950 },
      { id: 'and_ok', type: 'and', version: 2, title: '可开口且有预算', x: 960, y: 900 },
      { id: 'and_wake', type: 'and', version: 2, title: '唤醒放行', x: 1000, y: 700 },
      { id: 'or_trigger', type: 'or', version: 2, title: 'RP 触发', x: 1120, y: 520 },
      { id: 'rp_input', type: 'assemble', version: 2, title: 'RP 输入', x: 1320, y: 300 },
      { id: 'rp', type: 'react_workflow', version: 3, title: 'RP', config: { prompt: `${RP_PROMPT}\n如果输入里带有提醒内容, 直接把提醒发到群里。` }, x: 1320, y: 520 },
      { id: 'reply', type: 'output', version: 2, title: '最后回复', x: 1320, y: 730 },
      { id: 'note2', type: 'note', version: 1, title: '唤醒规则', config: { color: 'accent', text: '免打扰时段外: 按群策略唤醒\n免打扰时段内: 只有 @ 能唤醒\n超出调用预算: 都不唤醒' }, x: 300, y: 1010 },
    ], [
      ['source.out', 'bucket.in'], ['bucket.out', 'th.messages'], ['daytime.out', 'th.when', '夜间不更新'],
      ['th.event', 'agent.trigger'], ['agent.text', 'snapshot.write'], ['agent.tool', 'remind.caller'],
      ['bucket.out', 'wake.messages'], ['quiet.out', 'not_quiet.in'], ['not_quiet.out', 'or_ok.a'], ['mention.out', 'or_ok.b'],
      ['or_ok.out', 'and_ok.a'], ['budget.out', 'and_ok.b'], ['wake.event', 'and_wake.a'], ['and_ok.out', 'and_wake.b'],
      ['and_wake.out', 'or_trigger.a'], ['remind.event', 'or_trigger.b', '提醒内容'], ['or_trigger.out', 'rp.trigger'],
      ['snapshot.read', 'rp_input.snapshot'], ['bucket.out', 'rp_input.messages'], ['rp_input.context', 'rp.input'], ['rp.text', 'reply.reply'],
    ], model),
  },
  {
    id: 'handoff', label: '接待 + 专家 (工具交接)', nodePlugins: [],
    description: '接待 WF 被唤醒后自己决定是否调用交接工具让专家处理; 专家完成后由润色 WF 接着执行',
    build: (model) => assemble([
      { id: 'source', type: 'source', version: 1, title: '消息到达', x: 380, y: 0 },
      { id: 'bucket', type: 'bucket', version: 2, title: '桶', x: 380, y: 140 },
      { id: 'ctx', type: 'assemble', version: 2, title: '接待输入', config: { after_coverage: false, latest_n: 30 }, x: 380, y: 280 },
      { id: 'wake', type: 'wake', version: 1, title: '接待唤醒', x: 120, y: 440 },
      { id: 'host', type: 'react_workflow', version: 3, title: '接待', config: {
        prompt: '你是群助手。闲聊直接用 flow_reply 回复; 遇到需要查资料或计算的问题, 调用 ask_expert 交给专家, 不要自己编答案。',
      }, x: 380, y: 440 },
      { id: 'ask', type: 'tool', version: 1, title: '问专家', config: { tool_name: 'ask_expert', tool_description: '把需要查资料, 计算或长推理的问题交给专家, 参数写清楚问题本身' }, x: 700, y: 440 },
      { id: 'expert', type: 'react_workflow', version: 3, title: '专家', config: { thinking: 'high', prompt: '你是领域专家, 只根据检索到的资料回答, 给出结论和依据。' }, x: 960, y: 440 },
      { id: 'polish', type: 'react_workflow', version: 3, title: '润色', config: { prompt: '把专家的结论改写成群聊口吻, 保留要点, 通过 flow_reply 发出。' }, x: 960, y: 660 },
      { id: 'reply', type: 'output', version: 2, title: '群回复', x: 380, y: 660 },
    ], [
      ['source.out', 'bucket.in'], ['bucket.out', 'ctx.messages'], ['bucket.out', 'wake.messages'],
      ['wake.event', 'host.trigger'], ['ctx.context', 'host.input'], ['host.tool', 'ask.caller'],
      ['ask.event', 'expert.trigger'], ['expert.done', 'polish.trigger'], ['host.text', 'reply.reply'], ['polish.text', 'reply.reply'],
    ], model),
  },
  {
    id: 'blank', label: '空白', nodePlugins: [],
    description: '只有消息来源节点, 从头搭建',
    build: () => assemble([{ id: 'source', type: 'source', version: 1, title: '消息到达', x: 0, y: 0 }], [], ''),
  },
];

/**
 * 省略未安装插件提供的节点, 以及与之相连的边
 *
 * 返回: 处理后的存档与被省略的插件名
 */
export function pruneUnavailable(definition: FlowDefinitionJSON, available: Set<string>): { definition: FlowDefinitionJSON; missing: string[] } {
  const missingPlugins = new Set<string>();
  const kept = definition.nodes.filter((node) => {
    const dot = node.type.lastIndexOf('.');
    if (dot < 0 || available.has(node.type)) return true;
    missingPlugins.add(node.type.slice(0, dot));
    return false;
  });
  const ids = new Set(kept.map((node) => node.id));
  const owner = (endpoint: string) => endpoint.slice(0, endpoint.lastIndexOf('.'));
  return {
    definition: {
      ...definition,
      nodes: kept,
      edges: (definition.edges ?? []).filter((edge) => ids.has(owner(edge.from)) && ids.has(owner(edge.to))),
      layout: Object.fromEntries(Object.entries(definition.layout ?? {}).filter(([id]) => ids.has(id))),
    },
    missing: [...missingPlugins],
  };
}
