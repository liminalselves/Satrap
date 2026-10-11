import axios, { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ backend: 'http://backend', control: 'http://control', auth: vi.fn(), reauth: vi.fn() }));
vi.mock('@/utils/constants', () => ({ getApiBaseUrl: () => mocks.backend, getControlApiUrl: () => mocks.control }));
vi.mock('./auth', () => ({ ensureApiSession: mocks.auth, establishApiSession: mocks.reauth }));

const originalCreate = axios.create.bind(axios);
const response = (config: InternalAxiosRequestConfig, data: unknown) => ({ config, status: 200, statusText: 'OK', headers: {}, data });
const final = { ok: true, runtime: [{ target: 'Chat', status: 'next_activation', sessions: [] }] };
let adapter: ReturnType<typeof vi.fn>;
let createPluginApplication: typeof import('./pluginApplications').createPluginApplication;
let savePluginConfigOnly: typeof import('./pluginApplications').savePluginConfigOnly;
function snapshot(id: string, result: unknown = null) {
  return { operation_id: id, status: result ? 'completed' : 'running', created_at: 1, elapsed_ms: 1,
    stage: { platform_id: '', session_id: '', status: result ? 'completed' : 'running', phase: 'dispatch', phase_elapsed_ms: 1, timings_ms: {} },
    instances: [], instances_limited: false, result, error: '' };
}
beforeEach(async () => {
  vi.resetModules(); vi.useFakeTimers();
  mocks.backend = 'http://backend'; mocks.control = 'http://control';
  mocks.auth.mockReset().mockResolvedValue(undefined); mocks.reauth.mockReset().mockResolvedValue(undefined);
  adapter = vi.fn(async (config) => response(config, final));
  vi.spyOn(axios, 'create').mockImplementation((config) => originalCreate({ ...config, adapter }));
  ({ createPluginApplication, savePluginConfigOnly } = await import('./pluginApplications'));
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('控制服务插件保存与应用', () => {
  it('仅保存沿控制地址鉴权, 保留事务 revision 与配置正文, 明确关闭同步应用', async () => {
    const saved = { saved: true, revision: 'new', overrides: { note: 'new' } };
    adapter.mockImplementation(async (config) => response(config, saved));
    expect(await savePluginConfigOnly('/config/plugins/probe/config', { config: { note: 'new' }, expected_revision: 'old' }, {})).toEqual(saved);
    const config = adapter.mock.calls[0][0];
    expect(config.method).toBe('put'); expect(config.baseURL).toBe(mocks.control); expect(config.timeout).toBe(120000);
    expect(JSON.parse(config.data)).toEqual({ config: { note: 'new' }, expected_revision: 'old', apply_runtime: false });
    expect(mocks.auth).toHaveBeenCalledWith(mocks.control);
    expect(adapter).toHaveBeenCalledTimes(1);
  });

  it('位置作用域与任务查询都沿控制服务, 后端地址变化不影响控制任务', async () => {
    const app = createPluginApplication({ plugin_name: 'probe', kind: 'edictum', location_id: 'assistant' });
    adapter.mockImplementation(async (config) => {
      mocks.backend = 'http://new-backend';
      return response(config, snapshot(app.id, config.method === 'get' ? final : null));
    });
    const pending = app.start(vi.fn()); await vi.advanceTimersByTimeAsync(1000);
    expect(await pending).toEqual(final);
    const [post, get] = adapter.mock.calls.map(([config]) => config);
    expect(post.baseURL).toBe('http://control'); expect(get.baseURL).toBe('http://control');
    expect(JSON.parse(post.data)).toEqual({ plugin_name: 'probe', kind: 'edictum', location_id: 'assistant', operation_id: app.id });
    expect(get.url).toBe('/config/plugin-operations/' + app.id);
    expect(app.label).toBe('probe / assistant');
  });

  it('旧服务最终结果无需查询, 保留逐服务失败和下次激活状态', async () => {
    const partial = { ok: false, runtime: [...final.runtime, { target: 'Edictum', status: 'error', sessions: [], error: '失败' }] };
    adapter.mockImplementation(async (config) => response(config, partial));
    expect(await createPluginApplication({ plugin_name: 'probe' }).start(vi.fn())).toEqual(partial);
    expect(adapter.mock.calls.map(([config]) => config.method)).toEqual(['post']);
  });

  it('保存被拒绝或响应丢失不重放写入, 不隐式申请应用', async () => {
    for (const status of [401, 409, undefined]) {
      adapter.mockImplementation(async (config) => { throw new AxiosError('失败', 'ERR_NETWORK', config, undefined,
        status ? { status, data: { error: '失败' } } as AxiosResponse : undefined); });
      await expect(savePluginConfigOnly('/config/plugins/probe/config', {}, {})).rejects.toThrow('失败');
    }
    expect(adapter.mock.calls.map(([config]) => config.method)).toEqual(['put', 'put', 'put']);
    expect(mocks.reauth).not.toHaveBeenCalled();
  });

  it('认证期间控制地址变化阻止写入', async () => {
    mocks.auth.mockImplementation(async () => { mocks.control = 'http://new-control'; });
    await expect(savePluginConfigOnly('/config/plugins/probe/config', {}, {})).rejects.toThrow('控制服务地址已变化');
    expect(adapter).not.toHaveBeenCalled();
  });

  it('保存迟到响应遇到服务切换或页面离开时不确认成功', async () => {
    const abort = new AbortController();
    adapter.mockImplementation(async (config) => { mocks.control = 'http://new-control'; return response(config, { saved: true }); });
    await expect(savePluginConfigOnly('/config/plugins/probe/config', {}, {})).rejects.toThrow('控制服务地址已变化');
    mocks.control = 'http://control';
    adapter.mockImplementation(async (config) => { abort.abort(); return response(config, { saved: true }); });
    await expect(savePluginConfigOnly('/config/plugins/probe/config', {}, { signal: abort.signal })).rejects.toMatchObject({ code: 'query_stopped' });
    expect(adapter).toHaveBeenCalledTimes(2);
  });

  it('应用接受响应丢失只查已知控制任务, 不重复 POST', async () => {
    const app = createPluginApplication({ plugin_name: 'probe', kind: 'chat', location_id: 'chat' });
    adapter.mockImplementation(async (config) => {
      if (config.method === 'post') throw new AxiosError('Network Error', 'ERR_NETWORK', config);
      return response(config, snapshot(app.id, final));
    });
    expect(await app.start(vi.fn())).toEqual(final);
    expect(adapter.mock.calls.map(([config]) => config.method)).toEqual(['post', 'get']);
    expect(app.label).toBe('probe / Chat');
  });

  it('任务不能在新控制地址上查询旧 ID', async () => {
    const app = createPluginApplication({ plugin_name: 'probe' }); mocks.control = 'http://new-control';
    await expect(app.query()).rejects.toThrow('地址已变化');
    expect(adapter).not.toHaveBeenCalled();
  });
});
