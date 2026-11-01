import { describe, expect, it, vi } from 'vitest';
import type { ModelMetrics, ModelsResponse, Overview, SessionMetrics, SessionsResponse } from '@/api/contracts';
import { loadUsage, nextUsageNotifySignature, type UsageApi, type UsageLoadResult } from './loadUsage';

const OVERVIEW = { usage: { calls: 1 }, series: [] } as unknown as Overview;
const MODEL = { model: 'fast', model_config: 'fast', provider: null, usage: {}, trend: [] } as unknown as ModelMetrics;
const SESSION = { platform_id: 'chat', agent: null, session_id: 's1', usage: {}, trend: [], context: {} } as unknown as SessionMetrics;
const MODELS = { models: [MODEL] } as unknown as ModelsResponse;
const SESSIONS = { sessions: [SESSION] } as unknown as SessionsResponse;

function makeApi(overrides: Partial<UsageApi> = {}): UsageApi {
  return {
    overview: async () => OVERVIEW,
    models: async () => MODELS,
    sessions: async () => SESSIONS,
    ...overrides,
  };
}

const failure = (): UsageLoadResult => ({ status: 'failure', data: {}, errors: ['概览: 断线', '模型: 断线', '会话: 断线'] });

describe('Usage 数据加载', () => {
  it('全部成功时返回完整数据', async () => {
    const result = await loadUsage({}, undefined, makeApi());
    expect(result.status).toBe('success');
    expect(result.data).toEqual({ overview: OVERVIEW, models: [MODEL], sessions: [SESSION] });
    expect(result.errors).toEqual([]);
  });

  it('部分失败保留成功来源并记录失败来源', async () => {
    const result = await loadUsage({}, undefined, makeApi({
      models: async () => { throw new Error('模型接口超时'); },
    }));
    expect(result.status).toBe('partial');
    expect(result.data.overview).toBe(OVERVIEW);
    expect(result.data.sessions).toEqual([SESSION]);
    expect(result.data.models).toBeUndefined();
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('模型');
    expect(result.errors[0]).toContain('模型接口超时');
  });

  it('格式错误不被当成空数组', async () => {
    const result = await loadUsage({}, undefined, makeApi({
      models: async () => ({ models: 'not-an-array' } as unknown as ModelsResponse),
    }));
    expect(result.status).toBe('partial');
    expect(result.data.models).toBeUndefined();
    expect(result.errors[0]).toContain('models 数组');
  });

  it('全部失败时返回 failure 且不携带空结果', async () => {
    const result = await loadUsage({}, undefined, makeApi({
      overview: async () => { throw new Error('a'); },
      models: async () => { throw new Error('b'); },
      sessions: async () => { throw new Error('c'); },
    }));
    expect(result.status).toBe('failure');
    expect(result.data).toEqual({});
    expect(result.errors).toHaveLength(3);
  });

  it('失败后再次请求可恢复为成功', async () => {
    const api = makeApi({
      overview: vi.fn()
        .mockRejectedValueOnce(new Error('断线'))
        .mockResolvedValueOnce(OVERVIEW),
      models: vi.fn()
        .mockRejectedValueOnce(new Error('断线'))
        .mockResolvedValueOnce(MODELS),
      sessions: vi.fn()
        .mockRejectedValueOnce(new Error('断线'))
        .mockResolvedValueOnce(SESSIONS),
    });
    await expect(loadUsage({}, undefined, api)).resolves.toMatchObject({ status: 'failure' });
    await expect(loadUsage({}, undefined, api)).resolves.toMatchObject({ status: 'success' });
  });

  it('轮询只在新错误签名出现时通知', () => {
    const partial = failure();
    expect(nextUsageNotifySignature('', partial)).toEqual({ signature: '概览: 断线\n模型: 断线\n会话: 断线', notify: true });
    expect(nextUsageNotifySignature('概览: 断线\n模型: 断线\n会话: 断线', partial).notify).toBe(false);
    const changed = { ...partial, errors: ['概览: 新错误'] };
    expect(nextUsageNotifySignature('概览: 断线\n模型: 断线\n会话: 断线', changed).notify).toBe(true);
    expect(nextUsageNotifySignature('概览: 新错误', { status: 'success', data: {}, errors: [] })).toEqual({ signature: '', notify: false });
    expect(nextUsageNotifySignature('same', { status: 'aborted', data: {}, errors: [] })).toEqual({ signature: 'same', notify: false });
  });
});
