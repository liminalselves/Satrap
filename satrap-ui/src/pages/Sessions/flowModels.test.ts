import { describe, expect, it } from 'vitest';

import { agentModelLabel, flowModels } from './shared';

const flow = (nodes: Array<{ type: string; config?: Record<string, unknown> }>, fallback?: string) => ({
  edictum_type: 'flow',
  model_name: 'ignored',
  params: {
    flow: {
      nodes,
      ...(fallback ? { defaults: { react_workflow: { config: { model_name: fallback } } } } : {}),
    },
  },
});

describe('流程配置的模型显示', () => {
  it('取各 WF 节点的模型, 节点未设置时用流程默认, 去重保序, 忽略顶层模型', () => {
    const agent = flow([
      { type: 'source' },
      { type: 'react_workflow', config: { model_name: 'step-5' } },
      { type: 'react_workflow' },
      { type: 'react_workflow', config: { model_name: 'step-5' } },
    ], 'deepseek');
    expect(flowModels(agent)).toEqual(['step-5', 'deepseek']);
    expect(agentModelLabel(agent)).toBe('step-5, deepseek');
  });

  it('没有 WF 或都未设置模型时为空; 非流程配置显示顶层绑定', () => {
    expect(agentModelLabel(flow([{ type: 'react_workflow' }]))).toBe('');
    expect(agentModelLabel({ edictum_type: 'flow', model_name: 'x', params: {} })).toBe('');
    expect(flowModels({ edictum_type: 'async_simple', params: {} })).toEqual([]);
    expect(agentModelLabel({ edictum_type: 'async_simple', model_name: 'deepseek', params: {} })).toBe('deepseek');
  });
});
