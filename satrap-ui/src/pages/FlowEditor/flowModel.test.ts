import { describe, expect, it } from 'vitest';

import typesFixture from '../../../e2e/fixtures/flow-types.json';
import type { EdictumTypeDefinition, FlowDefinitionJSON } from '@/api/types';
import {
  edgeReading, indexTypes, outputType, parsePluginList, formatPluginList, resolveConfig, splitEndpoint, toDefinition, toReactFlow,
} from './flowModel';
import { pruneUnavailable, TEMPLATES } from './templates';

const meta = (typesFixture.types as unknown as EdictumTypeDefinition[]).find((item) => item.name === 'flow')!.flow!;
const types = indexTypes(meta);

describe('存档格式转换', () => {
  it('模板与插件节点 (id 带点) 往返后与原存档深度相等', () => {
    const cases: FlowDefinitionJSON[] = TEMPLATES.map((item) => item.build('deepseek-v3'));
    cases.push({
      version: 3,
      nodes: [
        { id: 'source', type: 'source', version: 1, title: '消息', config: {} },
        { id: 'quiet.v2', type: 'quiet_hours.in_hours', version: 1, title: '免打扰', config: { start: '23:00' } },
        { id: 'not', type: 'not', version: 1, title: 'NOT', config: {} },
      ],
      edges: [{ from: 'quiet.v2.out', to: 'not.in', label: '夜里' }],
      defaults: { 'quiet_hours.in_hours': { version: 1, config: { end: '06:00' } } },
      layout: { source: { x: 0, y: 0 }, 'quiet.v2': { x: 10, y: 20 }, not: { x: 30, y: 40 } },
      budget: { max_calls: 50, window_minutes: 60, count: 'wf' },
    });
    for (const definition of cases) {
      expect(toDefinition(toReactFlow(definition, types), types)).toEqual(definition);
    }
  });

  it('边端点按最后一个点拆分, 画布 id 与后端问题的 edge 字段同格式', () => {
    expect(splitEndpoint('quiet.v2.out')).toEqual(['quiet.v2', 'out']);
    expect(() => splitEndpoint('noport')).toThrow();
    const draft = toReactFlow({ version: 3, nodes: [{ id: 'a.b', type: 'note' }, { id: 'c', type: 'note' }], edges: [{ from: 'a.b.x', to: 'c.y' }] }, types);
    expect(draft.edges[0]).toMatchObject({ id: 'a.b.x->c.y', source: 'a.b', sourceHandle: 'x', target: 'c', targetHandle: 'y' });
  });

  it('缺失 layout 的节点排成网格, 缺失 title 用 id, 缺失 version 取当前契约版本', () => {
    const draft = toReactFlow({ nodes: [{ id: 'x', type: 'bucket' }, { id: 'y', type: 'bucket' }] }, types);
    expect(draft.nodes.map((node) => node.position)).toEqual([{ x: 0, y: 0 }, { x: 280, y: 0 }]);
    expect(draft.nodes[0].data).toMatchObject({ title: 'x', version: 2 });
  });

  it('空的流程默认和未配置预算不写入存档', () => {
    const out = toDefinition({ nodes: [], edges: [], defaults: { bucket: {} }, budget: null }, types);
    expect(out.defaults).toEqual({});
    expect('budget' in out).toBe(false);
  });
});

describe('配置解析', () => {
  it('类型默认 < 插件全局 < 流程默认 < 节点, inherited 不含节点层', () => {
    const data = { type: 'quiet_hours.in_hours', version: 1, title: 'q', config: { start: '21:00' } };
    const resolved = resolveConfig(data, types, { 'quiet_hours.in_hours': { end: '05:00' } }, { 'quiet_hours.in_hours': { start: '23:30', end: '06:00' } });
    expect(resolved.values).toEqual({ start: '21:00', end: '05:00' });
    expect(resolved.sources).toEqual({ start: 'node', end: 'flow' });
    expect(resolved.inherited).toEqual({ start: '23:30', end: '05:00' });
  });

  it('WF 插件字段与后端的每行一个插件名互转', () => {
    expect(parsePluginList('flow_snapshot\n  memory \n\n')).toEqual(['flow_snapshot', 'memory']);
    expect(formatPluginList(['flow_snapshot', 'memory'])).toBe('flow_snapshot\nmemory');
  });
});

describe('连线语义', () => {
  const draft = toReactFlow(TEMPLATES[0].build(''), types);
  const reading = (id: string) => edgeReading(draft.edges.find((edge) => edge.id === id)!, draft.nodes, draft.edges, types, draft.defaults, {});

  it('读取说明由接收端节点配置生成, 输出节点按 send_mode', () => {
    expect(reading('rp.text->reply.reply')).toEqual({ text: '工具发送, 可沉默', owner: 'reply' });
    expect(reading('th.event->agent.trigger')).toEqual({ text: '带本批消息', owner: 'th' });
    expect(reading('agent.text->snapshot.write')?.text).toBe('读 vN, 写 vN+1 与覆盖位置');
    expect(reading('bucket.out->th.messages')?.text).toBe('位置后增量 · 12 条 或 1500 字 或 10 分钟');
  });

  it('唤醒的读取说明随对话类型变化', () => {
    const wake = (kinds: Array<'group' | 'private'>) => edgeReading(draft.edges.find((edge) => edge.id === 'bucket.out->wake.messages')!,
      draft.nodes, draft.edges, types, draft.defaults, {}, kinds)?.text;
    expect(wake(['group'])).toBe('每条入站消息 · 沿用群策略');
    expect(wake(['private'])).toBe('每条入站消息 · 私聊每条唤醒');
    expect(wake(['group', 'private'])).toBe('每条入站消息 · 群聊按群策略, 私聊每条唤醒');
  });

  it('逻辑门 derived 输出: 有触发输入为 event, 否则为 condition', () => {
    const logic = toReactFlow(TEMPLATES[1].build(''), types);
    expect(outputType('and_wake', 'out', logic.nodes, logic.edges, types)).toBe('event');
    expect(outputType('and_ok', 'out', logic.nodes, logic.edges, types)).toBe('condition');
  });
});

describe('模板', () => {
  it('未安装的插件节点连同相连的边一起省略', () => {
    const available = new Set(meta.node_types.filter((item) => item.type !== 'quiet_hours.in_hours').map((item) => item.type));
    const { definition, missing } = pruneUnavailable(TEMPLATES[1].build(''), available);
    expect(missing).toEqual(['quiet_hours']);
    expect(definition.nodes.some((node) => node.id === 'quiet')).toBe(false);
    expect(definition.edges!.some((edge) => edge.from.startsWith('quiet.'))).toBe(false);
    expect(definition.layout!.quiet).toBeUndefined();
  });

  it('模板节点版本与后端契约一致', () => {
    for (const template of TEMPLATES) {
      for (const node of template.build('').nodes) {
        expect(types.get(node.type)?.version, `${template.id}.${node.id}`).toBe(node.version);
      }
    }
  });
});
