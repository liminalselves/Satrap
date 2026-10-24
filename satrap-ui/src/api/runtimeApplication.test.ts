import axios, { AxiosError, type AxiosResponse } from 'axios';
import { response } from '@/test/helpers';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ base: 'http://backend', auth: vi.fn(), reauth: vi.fn() }));
vi.mock('@/utils/constants', () => ({ getApiBaseUrl: () => mocks.base }));
vi.mock('./auth', () => ({ ensureApiSession: mocks.auth, establishApiSession: mocks.reauth }));

let RuntimeApplication: typeof import('./runtimeApplication').RuntimeApplication;
let adapter: ReturnType<typeof vi.fn>;
const result = { ok: true, edictum_sessions: [] };
const originalCreate = axios.create.bind(axios);
function snapshot(id: string, status = 'running', value: unknown = null, phase = 'install') {
  return { operation_id: id, status, created_at: 1, elapsed_ms: 2,
    stage: { platform_id: '', session_id: '', status, phase, phase_elapsed_ms: 1, timings_ms: {} },
    instances: [], instances_limited: false, result: value, error: '' };
}
function application() {
  return new RuntimeApplication('/api/edictum/plugins/reconcile', { config_name: 'a' },
    (value): value is typeof result => Boolean(value && typeof value === 'object' && Array.isArray((value as typeof result).edictum_sessions)));
}
beforeEach(async () => {
  vi.resetModules(); vi.useFakeTimers();
  mocks.base = 'http://backend'; mocks.auth.mockReset().mockResolvedValue(undefined); mocks.reauth.mockReset().mockResolvedValue(undefined);
  adapter = vi.fn(async (config) => response(config, result));
  vi.spyOn(axios, 'create').mockImplementation((config) => originalCreate({ ...config, adapter }));
  ({ RuntimeApplication } = await import('./runtimeApplication'));
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('运行时应用任务', () => {
  it('HTTP 环境缺少 randomUUID 时仍以安全随机 UUID 提交并按同一 ID 核查', async () => {
    const crypto = globalThis.crypto;
    vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) });
    const apps = Array.from({ length: 100 }, application);
    expect(new Set(apps.map((app) => app.id)).size).toBe(100);
    expect(apps.every((app) => /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(app.id))).toBe(true);
    const app = apps[0];
    adapter.mockImplementation(async (config) => {
      if (config.method === 'post') throw new AxiosError('Network Error', 'ERR_NETWORK', config);
      return response(config, snapshot(app.id, 'completed', result));
    });
    expect(await app.start(vi.fn())).toEqual(result);
    expect(adapter.mock.calls.map(([config]) => config.method)).toEqual(['post', 'get']);
    expect(JSON.parse(adapter.mock.calls[0][0].data).operation_id).toBe(app.id);
    expect(adapter.mock.calls[1][0].url).toBe('/api/runtime-operations/' + app.id);
  });

  it('没有安全随机数时明确拒绝创建任务, 不发出写请求', () => {
    vi.stubGlobal('crypto', undefined);
    expect(application).toThrow('不支持安全随机数');
    expect(adapter).not.toHaveBeenCalled();
  });

  it('用预先生成的 ID 提交一次, 真阶段查询至完成, 再次 start 不能重放', async () => {
    const app = application(), progress = vi.fn();
    adapter.mockImplementation(async (config) => response(config, snapshot(app.id,
      config.method === 'post' ? 'running' : 'completed', config.method === 'post' ? null : result)));
    const pending = app.start(progress);
    await vi.advanceTimersByTimeAsync(1000);
    expect(await pending).toEqual(result);
    expect(progress.mock.calls.map(([value]) => value.status)).toEqual(['running', 'completed']);
    const [post, get] = adapter.mock.calls.map(([config]) => config);
    expect(post.baseURL).toBe('http://backend');
    expect(JSON.parse(post.data)).toEqual({ config_name: 'a', operation_id: app.id });
    expect(get.url).toBe('/api/runtime-operations/' + app.id);
    await expect(app.start(progress)).rejects.toThrow('不要重复提交');
    expect(adapter).toHaveBeenCalledTimes(2);
  });

  it('接受响应丢失时仅 GET 查询, 返回部分失败原文, 不再次 POST', async () => {
    const app = application();
    adapter.mockImplementation(async (config) => {
      if (config.method === 'post') throw new AxiosError('Network Error', 'ERR_NETWORK', config);
      return response(config, snapshot(app.id, 'failed', { ...result, ok: false, edictum_sessions: [{ ok: false }] }));
    });
    expect(await app.start(vi.fn())).toMatchObject({ ok: false });
    expect(adapter.mock.calls.map(([config]) => config.method)).toEqual(['post', 'get']);
  });

  it('新服务/迟到旧响应不能确认当前结果, 不对新地址查询旧 ID', async () => {
    const app = application();
    adapter.mockImplementation(async (config) => { mocks.base = 'http://new-backend'; return response(config, result); });
    await expect(app.start(vi.fn())).rejects.toThrow('后端地址已变化');
    await expect(app.query()).rejects.toThrow('后端地址已变化');
    expect(adapter).toHaveBeenCalledTimes(1);
  });

  it('认证等待时换服务不能发出写请求', async () => {
    const app = application();
    mocks.auth.mockImplementation(async () => { mocks.base = 'http://new-backend'; });
    await expect(app.start(vi.fn())).rejects.toThrow('后端地址已变化');
    expect(adapter).not.toHaveBeenCalled();
  });

  it('旧服务同步响应保持兼容且不读取未知任务', async () => {
    expect(await application().start(vi.fn())).toEqual(result);
    expect(adapter).toHaveBeenCalledTimes(1);
  });

  it('服务重启/淘汰 404 明确未确认, 只读查询不提交任务', async () => {
    const app = application();
    adapter.mockImplementation(async (config) => { throw new AxiosError('404', 'ERR_BAD_RESPONSE', config, undefined, { status: 404, data: {} } as AxiosResponse); });
    await expect(app.query()).rejects.toThrow('生效状态未确认');
    expect(adapter.mock.calls.map(([config]) => config.method)).toEqual(['get']);
  });

  it('400/401/409/503 提交拒绝均不重放或假称接受', async () => {
    for (const status of [400, 401, 409, 503]) {
      adapter.mockImplementation(async (config) => { throw new AxiosError(String(status), 'ERR_BAD_RESPONSE', config, undefined, { status, data: {} } as AxiosResponse); });
      await expect(application().start(vi.fn())).rejects.toMatchObject({ status });
    }
    expect(adapter.mock.calls.map(([config]) => config.method)).toEqual(['post', 'post', 'post', 'post']);
    expect(mocks.reauth).not.toHaveBeenCalled();
  });

  it('读取 401 只沿原地址重新认证一次, 写操作不跟随重放', async () => {
    const app = application();
    adapter.mockImplementation(async (config) => {
      if (adapter.mock.calls.length === 1) throw new AxiosError('401', 'ERR_BAD_RESPONSE', config, undefined, { status: 401, data: {} } as AxiosResponse);
      return response(config, snapshot(app.id, 'completed', result));
    });
    expect((await app.query()).status).toBe('completed');
    expect(mocks.reauth).toHaveBeenCalledWith('http://backend');
    expect(adapter.mock.calls.map(([config]) => [config.method, config.baseURL])).toEqual([['get', 'http://backend'], ['get', 'http://backend']]);
  });

  it('截止后可重新只读查询实际完成结果, 不重复提交', async () => {
    const app = application();
    adapter.mockImplementation(async (config) => response(config, snapshot(app.id)));
    const pending = app.start(vi.fn());
    const rejected = expect(pending).rejects.toThrow('120 秒');
    await vi.advanceTimersByTimeAsync(120000); await rejected;
    adapter.mockImplementation(async (config) => response(config, snapshot(app.id, 'completed', result)));
    const current = await app.query();
    expect(await app.observe(current, vi.fn())).toEqual(result);
    expect(adapter.mock.calls.filter(([config]) => config.method === 'post')).toHaveLength(1);
  });

  it('隐藏停止只读轮询, 恢复后补查, 离开取消定时器且不撤销后台任务', async () => {
    const app = application(), controller = new AbortController(), page = Object.assign(new EventTarget(), { hidden: true });
    vi.stubGlobal('document', page);
    adapter.mockImplementation(async (config) => response(config, snapshot(app.id)));
    const pending = app.start(vi.fn(), controller.signal);
    const rejected = expect(pending).rejects.toThrow('查询已停止');
    await vi.advanceTimersByTimeAsync(5000); expect(adapter).toHaveBeenCalledTimes(1);
    page.hidden = false; page.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0); expect(adapter).toHaveBeenCalledTimes(2);
    controller.abort(); await rejected;
    expect(vi.getTimerCount()).toBe(0);
    expect(adapter.mock.calls.filter(([config]) => config.method === 'post')).toHaveLength(1);
  });

  it('在途状态 GET 卡住也在总截止停止, 迟到结果不覆盖, 后续仍可查询已知任务', async () => {
    const app = application(), progress = vi.fn();
    let release!: () => void;
    adapter.mockImplementation(async (config) => {
      if (config.method === 'post') return response(config, snapshot(app.id));
      await new Promise<void>((resolve) => { release = resolve; });
      return response(config, snapshot(app.id, 'completed', result));
    });
    const pending = app.start(progress), rejected = expect(pending).rejects.toThrow('120 秒');
    await vi.advanceTimersByTimeAsync(1000);
    expect(adapter.mock.calls.map(([config]) => config.method)).toEqual(['post', 'get']);
    await vi.advanceTimersByTimeAsync(119000); await rejected;
    expect(adapter.mock.calls[1][0].signal.aborted).toBe(true);
    release(); await vi.advanceTimersByTimeAsync(0);
    expect(progress.mock.calls.map(([value]) => value.status)).toEqual(['running']);
    expect(vi.getTimerCount()).toBe(0);
    adapter.mockImplementation(async (config) => response(config, snapshot(app.id, 'completed', result)));
    expect((await app.query()).result).toEqual(result);
    expect(adapter.mock.calls.filter(([config]) => config.method === 'post')).toHaveLength(1);
  });

  it('认证等待也受总截止限制, 迟到认证不能发出过期的写请求', async () => {
    const app = application();
    let release!: () => void;
    mocks.auth.mockImplementation(() => new Promise<void>((resolve) => { release = resolve; }));
    const pending = app.start(vi.fn()), rejected = expect(pending).rejects.toThrow('120 秒');
    await vi.advanceTimersByTimeAsync(120000); await rejected;
    expect(adapter).not.toHaveBeenCalled();
    release(); await vi.advanceTimersByTimeAsync(0);
    expect(adapter).not.toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0);
  });

  it('不接受其它任务 ID/未知终态/缺失结果, 不能误报成功', async () => {
    for (const value of [snapshot('other-id'), snapshot('other-id', 'completed', {}), { operation_id: 'invalid' }]) {
      adapter.mockImplementation(async (config) => response(config, value));
      await expect(application().start(vi.fn())).rejects.toThrow('响应无效');
    }
    const app = application();
    adapter.mockImplementation(async (config) => response(config, snapshot(app.id, 'cancelled')));
    await expect(app.start(vi.fn())).rejects.toThrow('没有可确认的应用结果');
  });
});
