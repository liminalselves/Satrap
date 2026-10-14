import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { AxiosError, type AxiosResponse, type AxiosAdapter } from 'axios';
import { deferred } from '@/test/helpers';

vi.mock('@/utils/constants', () => ({ getApiBaseUrl: () => 'http://backend', getControlApiUrl: () => 'http://control', getChatApiUrl: () => 'http://chat' }));
let auth: typeof import('./auth');
let sessionStore: typeof import('@/stores/useApiSessionStore').useApiSessionStore;
const response = (status = 200) => ({ ok: status < 400, status } as Response);
beforeEach(async () => {
  vi.resetModules(); vi.useFakeTimers();
  vi.stubGlobal('window', { location: { hash: '#token=synthetic-test-token', pathname: '/plugins', search: '?tab=config' }, history: { replaceState: vi.fn() } });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response()));
  auth = await import('./auth');
  sessionStore = (await import('@/stores/useApiSessionStore')).useApiSessionStore;
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function backendClient(adapter: AxiosAdapter) {
  const axios = (await import('axios')).default;
  const create = axios.create;
  vi.spyOn(axios, 'create').mockImplementationOnce((config) => {
    const client = create(config);
    client.defaults.adapter = adapter;
    return client;
  });
  return (await import('./client')).apiClient;
}

describe('每服务认证', () => {
  it('绑定后端的写请求收到 401 时不重放, 保留错误原因码', async () => {
    const adapter = vi.fn<AxiosAdapter>(async (config) => {
      throw new AxiosError('401', 'ERR_BAD_RESPONSE', config, undefined,
        { status: 401, data: { reason: 'auth_required' } } as AxiosResponse);
    });
    const client = await backendClient(adapter);
    const request = client.forService('http://backend').post('/api/applications', {});
    const rejected = expect(request).rejects.toMatchObject({ status: 401, code: 'auth_required' });
    await vi.advanceTimersByTimeAsync(0);
    await rejected;
    expect(adapter).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('绑定后端的读取只重放一次并解包结果, 两次调用保持同一服务地址', async () => {
    const adapter = vi.fn<AxiosAdapter>(async (config) => {
      if (adapter.mock.calls.length === 1) {
        throw new AxiosError('401', 'ERR_BAD_RESPONSE', config, undefined, { status: 401, data: {} } as AxiosResponse);
      }
      return { status: 200, statusText: 'OK', data: { state: 'succeeded' }, headers: {}, config };
    });
    const client = await backendClient(adapter);
    const request = client.forService('http://backend').get('/api/applications/test');
    await vi.advanceTimersByTimeAsync(0);
    expect(await request).toEqual({ state: 'succeeded' });
    expect(adapter).toHaveBeenCalledTimes(2);
    expect(adapter.mock.calls.map(([config]) => config.baseURL)).toEqual(['http://backend', 'http://backend']);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('认证等待期间服务地址变化时阻止原任务请求', async () => {
    const pending = deferred<Response>();
    vi.mocked(fetch).mockReturnValueOnce(pending.promise);
    const adapter = vi.fn<AxiosAdapter>(async (config) => ({ status: 200, statusText: 'OK', data: {}, headers: {}, config }));
    const client = await backendClient(adapter);
    let service = 'http://backend';
    const request = client.forService(service, () => service).get('/api/applications/test');
    const rejected = expect(request).rejects.toMatchObject({ code: 'service_changed' });
    await vi.advanceTimersByTimeAsync(0);
    service = 'http://replacement';
    pending.resolve(response());
    await rejected;
    expect(adapter).not.toHaveBeenCalled();
  });

  it('同服务合并, 提前消费 URL 令牌, 成功后业务读取不再反复认证', async () => {
    const pending = deferred<Response>();
    vi.mocked(fetch).mockReturnValueOnce(pending.promise);
    const first = auth.establishApiSession('http://control/');
    const second = auth.ensureApiSession('http://control');
    expect(window.history.replaceState).toHaveBeenCalledWith(null, '', '/plugins?tab=config');
    await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(vi.mocked(fetch).mock.calls[0][1]).toMatchObject({ credentials: 'include', headers: { Authorization: 'Bearer synthetic-test-token' } });
    pending.resolve(response()); await Promise.all([first, second]);
    await auth.ensureApiSession('http://control');
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(sessionStore.getState().sessions['http://control'].state).toBe('ready');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('服务独立成功或失败, 最慢服务有明确截止时间', async () => {
    vi.mocked(fetch).mockImplementation(async (url) => String(url).startsWith('http://chat') ? new Promise(() => undefined)
      : response(String(url).startsWith('http://control') ? 403 : 200));
    const bootstrap = auth.establishApiSessions();
    await vi.advanceTimersByTimeAsync(0);
    expect(sessionStore.getState().sessions).toMatchObject({ 'http://backend': { state: 'ready' }, 'http://control': { state: 'error' }, 'http://chat': { state: 'checking' } });
    await auth.ensureApiSession('http://backend');
    await vi.advanceTimersByTimeAsync(auth.API_SESSION_TIMEOUT_MS);
    await bootstrap;
    expect(sessionStore.getState().sessions['http://chat']).toMatchObject({ state: 'error', error: expect.stringContaining('8 秒') });
    expect(vi.getTimerCount()).toBe(0);
  });

  it('超时释放在途请求, 重试成功不被旧请求迟到覆盖', async () => {
    const old = deferred<Response>();
    vi.mocked(fetch).mockReturnValueOnce(old.promise);
    const first = auth.ensureApiSession('http://control');
    const rejected = expect(first).rejects.toThrow('8 秒');
    await vi.advanceTimersByTimeAsync(auth.API_SESSION_TIMEOUT_MS); await rejected;
    await auth.ensureApiSession('http://control');
    old.resolve(response(403)); await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(sessionStore.getState().sessions['http://control'].state).toBe('ready');
  });

  it('认证未完成或失败时不会发送受保护的控制请求', async () => {
    const pending = deferred<Response>();
    vi.mocked(fetch).mockReturnValueOnce(pending.promise);
    const { controlClient } = await import('./control');
    const adapter = vi.fn(async (config) => ({ status: 200, statusText: 'OK', data: {}, headers: {}, config }));
    controlClient.defaults.adapter = adapter;
    const request = controlClient.get('/config/plugins');
    await vi.advanceTimersByTimeAsync(0);
    expect(adapter).not.toHaveBeenCalled();
    const rejected = expect(request).rejects.toThrow('HTTP 403');
    pending.resolve(response(403)); await rejected;
    expect(adapter).not.toHaveBeenCalled();
    await controlClient.get('/config/plugins');
    expect(adapter).toHaveBeenCalledTimes(1);
  });

  it('401 强制重新认证后只重试一次, 403 不重试', async () => {
    const { controlClient } = await import('./control');
    const adapter = vi.fn(async (config) => {
      if (adapter.mock.calls.length === 1) throw new AxiosError('401', 'ERR_BAD_RESPONSE', config, undefined, { status: 401, data: {} } as AxiosResponse);
      return { status: 200, statusText: 'OK', data: {}, headers: {}, config };
    });
    controlClient.defaults.adapter = adapter;
    await controlClient.get('/config/plugins');
    expect(adapter).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenCalledTimes(2);
    adapter.mockImplementation(async (config) => { throw new AxiosError('403', 'ERR_BAD_RESPONSE', config, undefined, { status: 403, data: {} } as AxiosResponse); });
    await expect(controlClient.get('/config/plugins')).rejects.toMatchObject({ response: { status: 403 } });
    expect(adapter).toHaveBeenCalledTimes(3);
    expect(fetch).toHaveBeenCalledTimes(2);
    adapter.mockImplementation(async (config) => { throw new AxiosError('401', 'ERR_BAD_RESPONSE', config, undefined, { status: 401, data: {} } as AxiosResponse); });
    await expect(controlClient.get('/config/plugins')).rejects.toMatchObject({ response: { status: 401 } });
    expect(adapter).toHaveBeenCalledTimes(5);
    expect(fetch).toHaveBeenCalledTimes(3);
  });
});
