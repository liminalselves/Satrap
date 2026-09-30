import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from './client';
import { backendApi } from './backend';

vi.mock('./client', () => ({ apiClient: { post: vi.fn() } }));
beforeEach(() => vi.resetAllMocks());

describe('平台通信探测', () => {
  it('只提交平台 ID, 不要求群号或凭据', async () => {
    const result = { ok: true, detail: '通信正常, 对端已响应', elapsed_ms: 12 };
    vi.mocked(apiClient.post).mockResolvedValue(result);
    expect(await backendApi.checkPlatformConnection('bot 中文')).toEqual(result);
    expect(apiClient.post).toHaveBeenCalledWith('/api/platforms/bot%20%E4%B8%AD%E6%96%87/connection-test', {});
  });

  it('保留对端通信失败结果', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ ok: false, detail: '通信请求超时 (8 秒)', elapsed_ms: 8000 });
    expect(await backendApi.checkPlatformConnection('mk')).toMatchObject({ ok: false, detail: '通信请求超时 (8 秒)' });
  });
});
