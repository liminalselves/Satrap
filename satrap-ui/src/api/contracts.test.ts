import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/utils/constants', () => ({ getApiBaseUrl: () => 'http://backend', getControlApiUrl: () => 'http://control', getChatApiUrl: () => 'http://chat' }));
vi.mock('./auth', () => ({ ensureApiSession: vi.fn().mockResolvedValue(undefined), establishApiSession: vi.fn().mockResolvedValue(undefined) }));

const json = (status: number, body: unknown) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) } as Response);
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.resetModules();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

describe('serviceFetch', () => {
  it('省略空查询参数并保留错误码', async () => {
    fetchMock.mockResolvedValueOnce(json(400, { error: '查询范围超过限制', code: 'invalid_window' }));
    const { serviceFetch } = await import('./serviceFetch');
    await expect(serviceFetch('http://control', '/api/stats/overview', { query: { from: 1, to: undefined, tz: '' } }))
      .rejects.toMatchObject({ message: '查询范围超过限制', status: 400, code: 'invalid_window' });
    expect(fetchMock.mock.calls[0][0]).toBe('http://control/api/stats/overview?from=1');
  });

  it('认证失效时重新建立会话后重试一次', async () => {
    fetchMock.mockResolvedValueOnce(json(401, { error: 'auth' })).mockResolvedValueOnce(json(200, { ok: true }));
    const auth = await import('./auth');
    const { serviceFetch } = await import('./serviceFetch');
    await expect(serviceFetch('http://control', '/api/health', { background: true })).resolves.toEqual({ ok: true });
    expect(auth.establishApiSession).toHaveBeenCalledWith('http://control', { background: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('契约客户端', () => {
  it('批量决策拒绝超出 1-20 项的批次且不发请求', async () => {
    const { approvalsApi } = await import('./contracts');
    expect(() => approvalsApi.decide([])).toThrow();
    const key = { platform_id: 'qq', self_id: 'bot', source: 'friend' as const, action_id: 'a' };
    expect(() => approvalsApi.decide(Array.from({ length: 21 }, () => ({ key, expected_revision: 1, approve: true })))).toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('Chat 平台知识库任务直连 Chat, 其它平台经 Control 并携带平台', async () => {
    fetchMock.mockResolvedValue(json(200, { items: [], has_more: false, next_cursor: null }));
    const { ragJobsApi } = await import('./contracts');
    await ragJobsApi.jobs({ platform_id: 'chat', session_id: 's', kb_id: 'k' });
    await ragJobsApi.jobs({ platform_id: 'qq', session_id: 's', kb_id: 'k' });
    expect(fetchMock.mock.calls[0][0]).toBe('http://chat/api/chat/rag/jobs?session_id=s&kb_id=k');
    expect(fetchMock.mock.calls[1][0]).toBe('http://control/config/rag/jobs?session_id=s&kb_id=k&platform_id=qq');
  });

  it('健康采样记录浏览器往返耗时', async () => {
    fetchMock.mockResolvedValueOnce(json(200, { service: 'chat', running: true, healthy: true }));
    const { healthApi } = await import('./contracts');
    const sample = await healthApi.sample('chat');
    expect(fetchMock.mock.calls[0][0]).toBe('http://chat/api/chat/health');
    expect(sample.health.service).toBe('chat');
    expect(sample.rtt_ms).toBeGreaterThanOrEqual(0);
  });
});
