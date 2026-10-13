import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { AxiosError, type AxiosResponse } from 'axios';
import { deferred } from '@/test/helpers';
import { controlClient } from './control';
import { getConfigVersions, VersionedConfigRead } from './configVersions';

vi.mock('./control', () => ({ controlClient: { get: vi.fn() } }));
let scope = 'http://control-a';
vi.mock('@/utils/constants', () => ({ getControlApiUrl: () => scope }));

function versions(token = 'a', state = 'available') {
  return { data: { ok: true, resources: { models: { change_token: token, state } } } };
}
function failed(status: number, data: unknown = {}) {
  return new AxiosError('failed', 'ERR_BAD_RESPONSE', undefined, undefined, { status, data } as AxiosResponse);
}
beforeEach(() => { vi.resetAllMocks(); scope = 'http://control-a'; vi.mocked(controlClient.get).mockResolvedValue(versions()); });
afterEach(() => vi.useRealTimers());

describe('配置版本读取', () => {
  it('拒绝损坏来源和错误信封, 不把它们变成空配置', async () => {
    vi.mocked(controlClient.get).mockResolvedValueOnce(versions('bad', 'damaged')).mockResolvedValueOnce({ data: '<html>' });
    await expect(getConfigVersions(['models'])).rejects.toThrow('损坏');
    await expect(getConfigVersions(['models'])).rejects.toThrow('格式无效');
    vi.mocked(controlClient.get).mockRejectedValueOnce(failed(403));
    await expect(getConfigVersions(['models'])).rejects.toBeInstanceOf(AxiosError);
  });

  it('合并并发读取, 未变化时保留对象引用且仍核验版本', async () => {
    const loader = vi.fn().mockResolvedValue({ name: 'current' });
    const resource = new VersionedConfigRead(['models'], loader);
    const [left, right] = await Promise.all([resource.read(), resource.read()]);
    expect(left).toBe(right);
    expect(loader).toHaveBeenCalledTimes(1);
    const before = vi.mocked(controlClient.get).mock.calls.length;
    expect(await resource.read()).toBe(left);
    expect(loader).toHaveBeenCalledTimes(1);
    expect(controlClient.get).toHaveBeenCalledTimes(before + 1);
  });

  it('正文读取期间版本变化会重读, 不将旧正文标为新版本', async () => {
    vi.mocked(controlClient.get).mockResolvedValueOnce(versions('a')).mockResolvedValueOnce(versions('b'))
      .mockResolvedValueOnce(versions('b')).mockResolvedValueOnce(versions('b'));
    const loader = vi.fn().mockResolvedValueOnce('old').mockResolvedValueOnce('new');
    const resource = new VersionedConfigRead(['models'], loader);
    expect(await resource.read()).toBe('new');
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('只兼容 404 旧版本服务, 每次读取正文且不持久缓存', async () => {
    vi.mocked(controlClient.get).mockRejectedValue(failed(404));
    const loader = vi.fn().mockResolvedValueOnce('first').mockResolvedValueOnce('second');
    const resource = new VersionedConfigRead(['models'], loader);
    expect(await resource.read()).toBe('first');
    expect(await resource.read()).toBe('second');
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('单个消费者取消不影响其它消费者, 全部取消释放共享读取', async () => {
    const pending = deferred<string>();
    let shared!: AbortSignal;
    const resource = new VersionedConfigRead(['models'], (signal) => { shared = signal; return pending.promise; });
    const a = new AbortController(), b = new AbortController();
    const first = resource.read({ signal: a.signal });
    const second = resource.read({ signal: b.signal });
    await vi.waitFor(() => expect(shared).toBeDefined());
    a.abort();
    await expect(first).rejects.toMatchObject({ name: 'AbortError' });
    expect(shared.aborted).toBe(false);
    b.abort();
    await expect(second).rejects.toMatchObject({ name: 'AbortError' });
    expect(shared.aborted).toBe(true);
    pending.resolve('discarded');
    await new Promise((done) => setTimeout(done, 0));
  });

  it('失效前的迟到读取不能覆盖新缓存', async () => {
    const old = deferred<string>();
    const loader = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce('new');
    const resource = new VersionedConfigRead(['models'], loader);
    const first = resource.read();
    await vi.waitFor(() => expect(loader).toHaveBeenCalledTimes(1));
    resource.invalidate();
    expect(await resource.read()).toBe('new');
    old.resolve('old');
    expect(await first).toBe('old');
    expect(await resource.read()).toBe('new');
  });

  it('地址切换丢弃旧服务正文, 相同版本标识也不复用另一服务缓存', async () => {
    const old = deferred<string>();
    const loader = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce('b-data');
    const resource = new VersionedConfigRead(['models'], loader);
    const first = resource.read();
    await vi.waitFor(() => expect(loader).toHaveBeenCalledTimes(1));
    scope = 'http://control-b';
    old.resolve('a-data');
    await expect(first).rejects.toThrow('地址已改变');
    expect(await resource.read()).toBe('b-data');
  });

  it('无版本的后端状态读取合并在途请求, 地址变化拒收旧数据且不缓存正文', async () => {
    let backend = 'http://backend-a';
    const old = deferred<string>();
    const loader = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce('fresh').mockResolvedValueOnce('latest');
    const resource = new VersionedConfigRead([], loader, () => backend);
    const first = resource.read();
    const joined = resource.read();
    await vi.waitFor(() => expect(loader).toHaveBeenCalledTimes(1));
    backend = 'http://backend-b';
    expect(await resource.read()).toBe('fresh');
    old.resolve('obsolete');
    await expect(first).rejects.toThrow('地址已改变');
    await expect(joined).rejects.toThrow('地址已改变');
    expect(await resource.read()).toBe('latest');
    expect(loader).toHaveBeenCalledTimes(3);
    expect(controlClient.get).not.toHaveBeenCalled();
  });

  it('人工刷新强制核验并重读, 连续变化有界失败后可恢复', async () => {
    const loader = vi.fn().mockResolvedValue('good');
    const resource = new VersionedConfigRead(['models'], loader);
    await resource.read();
    await resource.read({ force: true });
    expect(loader).toHaveBeenCalledTimes(2);
    expect(vi.mocked(controlClient.get).mock.calls.some(([, options]) => (options?.params as { force?: boolean })?.force === true)).toBe(true);
    resource.invalidate();
    let token = 0;
    vi.mocked(controlClient.get).mockImplementation(async () => versions(String(token++)));
    await expect(resource.read()).rejects.toThrow('持续变化');
    vi.mocked(controlClient.get).mockResolvedValue(versions());
    expect(await resource.read()).toBe('good');
  });
});
