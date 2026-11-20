import { describe, expect, it } from 'vitest';

import typesFixture from '../../../e2e/fixtures/flow-types.json';
import type { EdictumTypeDefinition, FlowDefinitionJSON } from '@/api/types';
import { indexTypes, toReactFlow } from './flowModel';
import { TEMPLATES } from './templates';
import { pathHighlight, traceable, traceActivation } from './trace';

const meta = (typesFixture.types as unknown as EdictumTypeDefinition[]).find((item) => item.name === 'flow')!.flow!;
const types = indexTypes(meta);
const load = (definition: FlowDefinitionJSON) => toReactFlow(definition, types);
const ids = (path: { steps: Array<{ id: string }> }) => path.steps.map((step) => step.id);

describe('激活链路追踪', () => {
  it('Agent 交接让 RP 开口: 回复节点有唤醒和阈值经交接两条链路, 后者标记为模型决定', () => {
    const base = TEMPLATES[0].build('');
    const draft = load({
      ...base,
      nodes: [...base.nodes, { id: 'nudge', type: 'tool', version: 1, title: '让 RP 开口', config: { tool_name: 'nudge_rp' } }],
      edges: [...base.edges!, { from: 'agent.tool', to: 'nudge.caller' }, { from: 'nudge.event', to: 'rp.trigger' }],
    });
    const trace = traceActivation('reply', draft.nodes, draft.edges, types);
    expect(trace.paths.map(ids)).toEqual([
      ['wake', 'rp', 'reply'],
      ['th', 'agent', 'nudge', 'rp', 'reply'],
    ]);
    expect(trace.paths.map((path) => [path.root, path.modelDecides])).toEqual([['wake', false], ['threshold', true]]);
    expect(trace.paths[1].steps.map((step) => step.link)).toEqual(['event', 'handoff', 'event', 'reply', undefined]);
    expect(trace.paths[1].edges).toEqual(['th.event->agent.trigger', 'agent.tool->nudge.caller', 'nudge.event->rp.trigger', 'rp.text->reply.reply']);
    // 快照, 输入拼装等数据节点不在激活链路上
    expect(trace.nodes.has('snapshot')).toBe(false);
    expect(trace.nodes.has('rp_input')).toBe(false);
  });

  it('逻辑门: 触发输入继续向上, 条件输入记为这一步的条件, 磁贴 when 口的条件一并高亮', () => {
    const draft = load(TEMPLATES[1].build(''));
    const trace = traceActivation('reply', draft.nodes, draft.edges, types);
    expect(trace.paths.map(ids)).toEqual([
      ['wake', 'and_wake', 'or_trigger', 'rp', 'reply'],
      ['th', 'agent', 'remind', 'or_trigger', 'rp', 'reply'],
    ]);
    const [wake, remind] = trace.paths;
    expect(wake.steps.find((step) => step.id === 'and_wake')?.conditions).toEqual(['and_ok']);
    // 条件门往上的条件链全部收进高亮
    expect(new Set(wake.conditionNodes)).toEqual(new Set(['and_ok', 'or_ok', 'budget', 'not_quiet', 'quiet', 'mention']));
    expect(remind.steps[0]).toMatchObject({ id: 'th', kind: 'root', conditions: ['daytime'] });
    const only = pathHighlight(remind);
    expect(only.nodes.has('daytime')).toBe(true);
    expect(only.nodes.has('wake')).toBe(false);
    expect(only.edges.has('daytime.out->th.when')).toBe(true);
  });

  it('WF 完成触发下游; 没有触发输入的链路标为断开并排在最后', () => {
    const draft = load(TEMPLATES[2].build(''));
    const trace = traceActivation('reply', draft.nodes, draft.edges, types);
    expect(trace.paths.map(ids)).toEqual([
      ['wake', 'host', 'reply'],
      ['wake', 'host', 'ask', 'expert', 'polish', 'reply'],
    ]);
    expect(trace.paths[1].steps.map((step) => step.link)).toEqual(['event', 'handoff', 'event', 'done', 'reply', undefined]);

    const cut = load({ ...TEMPLATES[2].build(''), edges: TEMPLATES[2].build('').edges!.filter((edge) => edge.to !== 'host.trigger') });
    const dead = traceActivation('reply', cut.nodes, cut.edges, types);
    expect(dead.paths.every((path) => path.root === null)).toBe(true);
    expect(dead.paths[0].steps[0]).toMatchObject({ id: 'host', kind: 'workflow' });
  });

  it('环不会无限展开; 只输出条件的逻辑门不可追踪', () => {
    const draft = load({
      version: 3,
      nodes: [
        { id: 'wake', type: 'wake' }, { id: 'a', type: 'react_workflow' }, { id: 'b', type: 'react_workflow' },
      ],
      edges: [{ from: 'wake.event', to: 'a.trigger' }, { from: 'a.done', to: 'b.trigger' }, { from: 'b.done', to: 'a.trigger' }],
    });
    const trace = traceActivation('b', draft.nodes, draft.edges, types);
    expect(trace.paths.map((path) => [ids(path), path.root])).toEqual([
      [['wake', 'a', 'b'], 'wake'],
      [['b', 'a', 'b'], null],
    ]);
    expect(trace.paths[1].steps[0].kind).toBe('dead');

    const logic = load(TEMPLATES[1].build(''));
    expect(traceable('and_ok', logic.nodes, logic.edges, types)).toBe(false);
    expect(traceable('and_wake', logic.nodes, logic.edges, types)).toBe(true);
    expect(traceable('snapshot', logic.nodes, logic.edges, types)).toBe(false);
  });
});
