import { beforeEach, describe, expect, it, vi } from 'vitest';
import { controlClient } from '@/api/control';
import { modelApi } from '@/api/model';
import { sessionApi } from '@/api/session';
import { edictumApi } from '@/api/edictum';
import type { EdictumSessionConfig } from '@/api/types';

vi.mock('@/api/control', () => ({ controlClient: { get: vi.fn() } }));
vi.mock('@/api/model', () => ({ modelApi: { list: vi.fn(), update: vi.fn() } }));
vi.mock('@/api/session', () => ({ sessionApi: { list: vi.fn() } }));
vi.mock('@/api/edictum', () => ({ edictumApi: { list: vi.fn() } }));
let store: typeof import('./useConfigStore').useConfigStore;
let token: string;

beforeEach(async () => {
  vi.resetModules(); vi.resetAllMocks(); token = 'a';
  vi.mocked(controlClient.get).mockImplementation(async (url, options) => {
    const params = options?.params as { resources?: string; resource?: string; type?: string };
    const resources = params.resource === 'edictum_configs' ? ['edictum_configs', 'plugin_catalog'] : [params.resource || ''];
    const versions = Object.fromEntries((params.resource ? resources : String(params.resources).split(',')).map((name) => [name, { change_token: token, state: 'available' }]));
    if (url === '/config/snapshot') {
      const data = params.resource === 'session_classes' ? await sessionApi.list() : params.resource === 'edictum_configs' ? await edictumApi.list() : await modelApi.list('llm');
      return { data: { data, versions } };
    }
    return { data: { ok: true, resources: versions } };
  });
  vi.mocked(sessionApi.list).mockResolvedValue({ demo: { class_path: 'Demo', enabled: true } });
  vi.mocked(edictumApi.list).mockResolvedValue({});
  vi.mocked(modelApi.list).mockResolvedValue({});
  store = (await import('./useConfigStore')).useConfigStore;
});

describe('按使用范围刷新配置', () => {
  it('采用保存返回的单个配置, 保留其它配置引用并阻止在途旧全量读取回退', async () => {
    const previous: EdictumSessionConfig = { provider: 'edictum', description: '', model_name: '', edictum_type: 'simple', enabled: true, plugins: [], params: {} };
    const unrelated = { ...previous, description: 'other' };
    const saved = { ...previous, plugins: ['demo'] };
    store.setState({ edictumConfigs: { assistant: previous, other: unrelated } });
    let resolve!: (value: Record<string, typeof previous>) => void;
    vi.mocked(edictumApi.list).mockReturnValue(new Promise((done) => { resolve = done; }));
    const pending = store.getState().fetchEdictumConfigs();
    await vi.waitFor(() => expect(edictumApi.list).toHaveBeenCalledOnce());
    store.getState().acceptEdictumConfig('assistant', saved);
    saved.plugins.push('outside');
    resolve({ assistant: previous, other: unrelated });
    await pending;
    expect(store.getState().edictumConfigs.assistant.plugins).toEqual(['demo']);
    expect(store.getState().edictumConfigs.other).toBe(unrelated);
    store.getState().acceptEdictumConfig('renamed', previous, 'assistant');
    expect(store.getState().edictumConfigs.assistant).toBeUndefined();
    expect(store.getState().edictumConfigs.renamed).toEqual(previous);
  });

  it('无消费者不读取, 多消费者合并范围, 卸载后停止刷新', async () => {
    await store.getState().refreshWatchedConfigs();
    expect(controlClient.get).not.toHaveBeenCalled();
    const first = store.getState().watchConfigs(['sessions', 'edictum']);
    const second = store.getState().watchConfigs(['sessions']);
    await store.getState().refreshWatchedConfigs();
    const previous = store.getState().sessionClasses;
    expect(sessionApi.list).toHaveBeenCalledTimes(1);
    expect(edictumApi.list).toHaveBeenCalledTimes(1);
    expect(modelApi.list).not.toHaveBeenCalled();
    await store.getState().refreshWatchedConfigs();
    expect(store.getState().sessionClasses).toBe(previous);
    expect(sessionApi.list).toHaveBeenCalledTimes(1);
    first(); token = 'b';
    await store.getState().refreshWatchedConfigs();
    expect(sessionApi.list).toHaveBeenCalledTimes(2);
    expect(edictumApi.list).toHaveBeenCalledTimes(1);
    second();
    const count = vi.mocked(controlClient.get).mock.calls.length;
    await store.getState().refreshWatchedConfigs();
    expect(controlClient.get).toHaveBeenCalledTimes(count);
  });

  it('读取失败保留已展示配置并向调用方报错', async () => {
    const release = store.getState().watchConfigs(['sessions']);
    await store.getState().refreshWatchedConfigs();
    const previous = store.getState().sessionClasses;
    token = 'b';
    vi.mocked(sessionApi.list).mockRejectedValue(new Error('read failed'));
    await expect(store.getState().refreshWatchedConfigs()).rejects.toThrow('read failed');
    expect(store.getState().sessionClasses).toBe(previous);
    release();
  });
});
