import { describe, expect, it } from 'vitest';

import typesFixture from '../../../e2e/fixtures/flow-types.json';
import type { EdictumTypeDefinition, FlowDefinitionJSON } from '@/api/types';
import { parseFlowImport, unknownNodeTypes } from './flowImport';
import { indexTypes, toDefinition, toReactFlow } from './flowModel';
import { TEMPLATES } from './templates';

const meta = (typesFixture.types as unknown as EdictumTypeDefinition[]).find((item) => item.name === 'flow')!.flow!;
const types = indexTypes(meta);
const flow: FlowDefinitionJSON = TEMPLATES[0].build('deepseek-v3');
const named = { provider: 'edictum', edictum_type: 'flow', enabled: true, description: '', model_name: 'm', params: { flow }, plugins: [] };

describe('流程导入解析', () => {
  it('接受 params.flow, 命名配置, 和只含一个流程的配置文件', () => {
    expect(parseFlowImport(JSON.stringify(flow))).toEqual({ definition: flow, origin: 'params.flow' });
    expect(parseFlowImport(JSON.stringify(named))).toEqual({ definition: flow, origin: '命名配置' });
    const file = { chat: { ...named, edictum_type: 'simple', params: {} }, rp: named };
    expect(parseFlowImport(JSON.stringify(file))).toEqual({ definition: flow, origin: '命名配置 rp' });
  });

  it('导入结果经画布往返后与原存档一致', () => {
    const { definition } = parseFlowImport(JSON.stringify(named));
    expect(toDefinition(toReactFlow(definition, types), types)).toEqual(flow);
  });

  it.each([
    ['{', /不是有效的 JSON/],
    ['[]', /顶层必须是 JSON 对象/],
    ['{"a": 1}', /无法识别/],
    [JSON.stringify({ ...named, edictum_type: 'simple' }), /类型是 simple, 不是流程/],
    [JSON.stringify({ ...named, params: {} }), /没有 params.flow/],
    [JSON.stringify({ a: named, b: named }), /有 2 个流程配置 \(a, b\)/],
    [JSON.stringify({ ...flow, version: 4 }), /版本 4 高于编辑器支持的 3/],
    [JSON.stringify({ nodes: [{ id: 'a' }] }), /节点 a 缺少 type/],
    [JSON.stringify({ nodes: [{ id: 'a', type: 'note' }, { id: 'a', type: 'note' }] }), /节点 id 重复: a/],
    [JSON.stringify({ nodes: [{ id: 'a', type: 'note' }], edges: [{ from: 'a.out', to: 'b.in' }] }), /不存在的节点 b/],
    [JSON.stringify({ nodes: [{ id: 'a', type: 'note' }], edges: [{ from: 'a', to: 'a.in' }] }), /端点格式无效/],
    [JSON.stringify({ nodes: [{ id: 'a', type: 'note', config: [] }] }), /config 不是对象/],
  ])('拒绝 %s', (text, message) => {
    expect(() => parseFlowImport(text)).toThrow(message);
  });

  it('列出当前宿主不认识的节点类型, 但不拒绝导入', () => {
    const definition = parseFlowImport(JSON.stringify({ nodes: [{ id: 'q', type: 'missing_plugin.node' }, { id: 'n', type: 'note' }] })).definition;
    expect(unknownNodeTypes(definition, types)).toEqual(['missing_plugin.node']);
  });
});
