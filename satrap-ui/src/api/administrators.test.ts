import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { administratorsApi } from './administrators';
import { apiClient, ApiError } from './client';
import { getControlApiUrl } from '@/utils/constants';

const get = vi.fn<ReturnType<typeof apiClient.forService>['get']>();
const failure = (status: number) => new ApiError('失败', status);
beforeEach(() => { vi.spyOn(apiClient, 'forService').mockReturnValue({ get: get as ReturnType<typeof apiClient.forService>['get'], post: vi.fn(), put: vi.fn() }); });
afterEach(() => { vi.restoreAllMocks(); get.mockReset(); });

describe('管理员配置与运行状态分开读取', () => {
  it('新快照读取不等待运行查询, 绑定地址并传递取消信号', async () => {
    const signal = new AbortController().signal;
    const snapshot = { revision: 'v1', runtime: { status: 'unconfirmed' } };
    get.mockResolvedValueOnce(snapshot);
    expect(await administratorsApi.read(signal, 'http://original-control')).toBe(snapshot);
    expect(apiClient.forService).toHaveBeenCalledWith('http://original-control', getControlApiUrl);
    expect(get).toHaveBeenCalledExactlyOnceWith('/config/administrator-groups/snapshot', signal);
  });
  it('仅 404 回退旧快照接口, 沿原地址与信号读取', async () => {
    const signal = new AbortController().signal;
    const snapshot = { revision: 'legacy', runtime: { status: 'applied' } };
    get.mockRejectedValueOnce(failure(404)).mockResolvedValueOnce(snapshot);
    expect(await administratorsApi.read(signal, 'http://original-control')).toBe(snapshot);
    expect(get.mock.calls.map(args => args[0])).toEqual(['/config/administrator-groups/snapshot', '/config/administrator-groups']);
    expect(get.mock.calls[1][1]).toBe(signal);
    expect(apiClient.forService).toHaveBeenCalledTimes(1);
  });
  it('拒绝或读取失败不回退绕过新接口', async () => {
    get.mockRejectedValueOnce(failure(403));
    await expect(administratorsApi.read()).rejects.toMatchObject({ status: 403 });
    expect(get).toHaveBeenCalledTimes(1);
  });
  it('旧服务没有独立状态接口时保留原快照状态, 不发写操作', async () => {
    get.mockRejectedValueOnce(failure(404));
    expect(await administratorsApi.runtime()).toBeNull();
    expect(get).toHaveBeenCalledExactlyOnceWith('/config/administrator-groups/runtime', undefined);
  });
  it('状态带实际区段修订, 无效状态与权限失败明确报错', async () => {
    const confirmed = { section_revision: 'saved-section', runtime: { status: 'applied' } };
    get.mockResolvedValueOnce(confirmed).mockResolvedValueOnce({}).mockRejectedValueOnce(failure(403));
    expect(await administratorsApi.runtime()).toBe(confirmed);
    await expect(administratorsApi.runtime()).rejects.toThrow('响应格式无效');
    await expect(administratorsApi.runtime()).rejects.toMatchObject({ status: 403 });
  });
});
