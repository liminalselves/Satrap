import { afterEach, describe, expect, it, vi } from 'vitest';
import { AxiosError, type AxiosResponse } from 'axios';

import { controlApi, controlClient, parseEdictumTypes, RevisionConflictError } from './control';
import { edictumApi } from './edictum';

function rejected(status: number, data: unknown) {
  return new AxiosError('failed', 'ERR_BAD_RESPONSE', undefined, undefined, { status, data } as AxiosResponse);
}

afterEach(() => vi.restoreAllMocks());

describe('流程会话 API', () => {
  it('类型表保留 flow 元数据, 其他类型没有 flow 字段也能解析', () => {
    const types = parseEdictumTypes({ types: [
      { name: 'simple', is_async: false, description: '', config_schema: {}, capabilities: { plugins: true, mcp: true, stream: true } },
      { name: 'flow', is_async: true, description: '', config_schema: {}, capabilities: { plugins: true, mcp: false, stream: false },
        flow: { node_types: [{ type: 'source' }], port_types: {}, defaults: { flow: {}, plugin: {} }, models: [] } },
    ] });
    expect(types[0].flow).toBeUndefined();
    expect(types[1].flow?.node_types[0].type).toBe('source');
  });

  it('更新带 expected_revision; 不带时请求体不含该字段', async () => {
    const patch = vi.spyOn(controlClient, 'patch').mockResolvedValue({ data: { ok: true, revision: 'r2' } });
    await edictumApi.update('rp', { description: 'x' }, 'r1');
    await edictumApi.update('rp', { description: 'y' });
    expect(patch).toHaveBeenNthCalledWith(1, '/config/edictum/sessions/rp', { description: 'x', expected_revision: 'r1' });
    expect(patch).toHaveBeenNthCalledWith(2, '/config/edictum/sessions/rp', { description: 'y' });
  });

  it('409 转为 RevisionConflictError 并带回当前修订; 其他错误原样抛出', async () => {
    vi.spyOn(controlClient, 'patch')
      .mockRejectedValueOnce(rejected(409, { error: '已被修改', code: 'config_revision_conflict', revision: 'r9' }))
      .mockRejectedValueOnce(rejected(400, { error: 'bad' }));
    const conflict = await edictumApi.update('rp', {}, 'r1').catch((e) => e);
    expect(conflict).toBeInstanceOf(RevisionConflictError);
    expect(conflict.revision).toBe('r9');
    expect(conflict.message).toBe('已被修改');
    const other = await edictumApi.update('rp', {}, 'r1').catch((e) => e);
    expect(other).toBeInstanceOf(AxiosError);
  });

  it('检查请求透传 abort signal, 运行状态按查询参数请求', async () => {
    const post = vi.spyOn(controlClient, 'post').mockResolvedValue({ data: { ok: true, issues: [] } });
    const get = vi.spyOn(controlClient, 'get').mockResolvedValue({ data: { tiles: {} } });
    const controller = new AbortController();
    await edictumApi.validateFlow({ flow: { nodes: [] }, conversation_kinds: ['group'] }, controller.signal);
    expect(post).toHaveBeenCalledWith('/config/edictum/flow/validate',
      { flow: { nodes: [] }, conversation_kinds: ['group'] }, { signal: controller.signal });
    await edictumApi.getFlowRuntime('qq', 'g1');
    expect(get).toHaveBeenCalledWith('/config/edictum/flow/runtime',
      { params: { platform_id: 'qq', session_id: 'g1' }, signal: undefined });
  });

  it('插件节点默认按整张表替换保存, 冲突同样转换', async () => {
    const put = vi.spyOn(controlClient, 'put')
      .mockResolvedValueOnce({ data: { ok: true, saved: true, nodes: {}, revision: 'b', schema: {} } })
      .mockRejectedValueOnce(rejected(409, { error: 'conflict' }));
    await controlApi.savePluginFlowNodeDefaults('quiet_hours', { in_hours: { version: 1, config: { start: '23:00' } } }, 'a');
    expect(put).toHaveBeenCalledWith('/config/plugins/quiet_hours/flow-nodes',
      { config: { in_hours: { version: 1, config: { start: '23:00' } } }, expected_revision: 'a' });
    const conflict = await controlApi.savePluginFlowNodeDefaults('quiet_hours', {}, 'a').catch((e) => e);
    expect(conflict).toBeInstanceOf(RevisionConflictError);
    expect(conflict.revision).toBeNull();
  });
});
