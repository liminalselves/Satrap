import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '@/api/backend';
import { controlApi } from '@/api/control';
import { useBackendStore } from './useBackendStore';
import type { BackendHealth } from '@/api/types';
import type { BackendStatus, ControlResult } from '@/api/control';

vi.mock('@/api/backend', () => ({ backendApi: { health: vi.fn(), reloadConfig: vi.fn(), shutdown: vi.fn() } }));
vi.mock('@/api/control', () => ({ controlApi: { status: vi.fn(), start: vi.fn(), stop: vi.fn(), restart: vi.fn() } }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetAllMocks();
  useBackendStore.setState({ health: null, healthAvailable: false, controlStatus: null,
    isRunning: false, runState: 'unknown', operation: null, loading: false, lastError: null });
});
afterEach(() => vi.useRealTimers());

describe('后端状态同步', () => {
  it('最新停止状态覆盖旧 health, 清除适配器并忽略旧推送', async () => {
    useBackendStore.getState().setHealth({ running: true, adapters: { bot: { started: true, status: 'running' } } });
    vi.mocked(controlApi.status).mockResolvedValue({ running: false, managed: false });
    await useBackendStore.getState().refreshControlStatus();
    useBackendStore.getState().setHealth({ running: true, adapters: { bot: { started: true, status: 'running' } } });
    expect(useBackendStore.getState()).toMatchObject({ isRunning: false, runState: 'stopped', health: { adapters: {} } });
  });

  it('WebSocket 部分快照保留完整 health 的配置应用结果', () => {
    useBackendStore.getState().setHealth({ running: true, runtime_id: 'r1',
      platform_config: [{ id: 'bot', status: 'failed', saved_revision: 'new', active_revision: 'old' }] });
    useBackendStore.getState().setHealth({ running: true, adapters: {} });
    expect(useBackendStore.getState().health?.platform_config?.[0].status).toBe('failed');
    useBackendStore.getState().setHealth({ running: true, runtime_id: 'r2', adapters: {} });
    expect(useBackendStore.getState().health?.platform_config).toBeUndefined();
  });

  it('旧控制请求晚到时不能覆盖新控制快照', async () => {
    const old = deferred<BackendStatus>();
    vi.mocked(controlApi.status).mockReturnValueOnce(old.promise).mockResolvedValueOnce({ running: false, managed: false });
    const first = useBackendStore.getState().refreshControlStatus();
    await useBackendStore.getState().refreshControlStatus();
    old.resolve({ running: true, managed: true });
    await first;
    expect(useBackendStore.getState().runState).toBe('stopped');
  });

  it('旧 health 晚到时不能覆盖新推送或恢复旧配置报告', async () => {
    const old = deferred<BackendHealth>();
    vi.mocked(backendApi.health).mockReturnValueOnce(old.promise);
    const first = useBackendStore.getState().refreshHealth();
    useBackendStore.getState().setHealth({ running: true, adapters: { new: { status: 'running', started: true } } });
    old.resolve({ running: false, adapters: {} });
    await first;
    expect(useBackendStore.getState().health?.adapters?.new).toBeDefined();
  });

  it('停止状态拒绝尚在途中的旧 health 响应', async () => {
    const old = deferred<BackendHealth>();
    vi.mocked(backendApi.health).mockReturnValueOnce(old.promise);
    const first = useBackendStore.getState().refreshHealth();
    vi.mocked(controlApi.status).mockResolvedValue({ running: false, managed: false });
    await useBackendStore.getState().refreshControlStatus();
    old.resolve({ running: true, adapters: { old: { started: true, status: 'running' } } });
    await first;
    expect(useBackendStore.getState().health?.adapters?.old).toBeUndefined();
    expect(useBackendStore.getState().loading).toBe(false);
  });

  it('控制服务不可用时回退到可达 health, 两者失败则显示未知', async () => {
    vi.mocked(controlApi.status).mockRejectedValue(new Error('offline'));
    vi.mocked(backendApi.health).mockResolvedValue({ running: true });
    await useBackendStore.getState().refreshAll();
    expect(useBackendStore.getState().runState).toBe('running');
    vi.mocked(backendApi.health).mockRejectedValue(new Error('offline'));
    await useBackendStore.getState().refreshAll();
    expect(useBackendStore.getState().runState).toBe('unknown');
  });
});

describe('后端控制与配置应用', () => {
  it('启动请求受理后仍等待 health 确认并拒绝重复操作', async () => {
    vi.useFakeTimers();
    const accepted = deferred<ControlResult>();
    vi.mocked(controlApi.start).mockReturnValue(accepted.promise);
    vi.mocked(controlApi.status).mockResolvedValue({ running: true, managed: true });
    vi.mocked(backendApi.health).mockResolvedValueOnce({ running: false }).mockResolvedValue({ running: true });
    const pending = useBackendStore.getState().controlBackend('start');
    expect(useBackendStore.getState().operation).toBe('start');
    expect((await useBackendStore.getState().controlBackend('start')).ok).toBe(false);
    accepted.resolve({ ok: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(useBackendStore.getState().operation).toBe('start');
    await vi.advanceTimersByTimeAsync(500);
    expect(await pending).toEqual({ ok: true });
    expect(controlApi.start).toHaveBeenCalledTimes(1);
    expect(useBackendStore.getState().operation).toBeNull();
  });

  it('重启必须确认新的运行时身份', async () => {
    vi.useFakeTimers();
    useBackendStore.getState().setHealth({ running: true, runtime_id: 'old' });
    vi.mocked(controlApi.restart).mockResolvedValue({ ok: true });
    vi.mocked(controlApi.status).mockResolvedValue({ running: true, managed: true });
    vi.mocked(backendApi.health).mockResolvedValueOnce({ running: true, runtime_id: 'old' })
      .mockResolvedValue({ running: true, runtime_id: 'new' });
    const pending = useBackendStore.getState().controlBackend('restart');
    await vi.advanceTimersByTimeAsync(0);
    expect(useBackendStore.getState().operation).toBe('restart');
    await vi.advanceTimersByTimeAsync(500);
    expect((await pending).ok).toBe(true);
  });

  it('启停确认超时显示未确认, 释放操作锁', async () => {
    vi.useFakeTimers();
    vi.mocked(controlApi.stop).mockResolvedValue({ ok: true });
    vi.mocked(controlApi.status).mockResolvedValue({ running: true, managed: true });
    vi.mocked(backendApi.health).mockResolvedValue({ running: true });
    const pending = useBackendStore.getState().controlBackend('stop');
    await vi.advanceTimersByTimeAsync(30000);
    expect(await pending).toMatchObject({ ok: false, error: expect.stringContaining('未在 30 秒内确认') });
    expect(useBackendStore.getState().operation).toBeNull();
  });

  it('控制请求被拒绝时不宣称停止成功', async () => {
    useBackendStore.getState().setHealth({ running: true });
    vi.mocked(controlApi.stop).mockResolvedValue({ ok: false, error: 'denied' });
    expect(await useBackendStore.getState().controlBackend('stop')).toEqual({ ok: false, error: 'denied' });
    expect(useBackendStore.getState().isRunning).toBe(true);
  });

  it('仅未受管且 health 可达的后端允许 shutdown 回退', async () => {
    useBackendStore.getState().setHealth({ running: true });
    vi.mocked(controlApi.stop).mockRejectedValue(new Error('offline'));
    vi.mocked(backendApi.shutdown).mockResolvedValue({ ok: true });
    vi.mocked(controlApi.status).mockResolvedValue({ running: false, managed: false });
    vi.mocked(backendApi.health).mockRejectedValue(new Error('stopped'));
    expect((await useBackendStore.getState().controlBackend('stop')).ok).toBe(true);
    expect(backendApi.shutdown).toHaveBeenCalledOnce();
  });

  it('重载报告中的部分失败和待重启不能被 ok 掩盖', async () => {
    vi.mocked(backendApi.reloadConfig).mockResolvedValue({ ok: true, edictum_sessions: [],
      platforms: [{ id: 'bot', status: 'failed', error: 'binding failed', saved_revision: 'new', active_revision: 'old' }] });
    vi.mocked(controlApi.status).mockResolvedValue({ running: true, managed: true });
    vi.mocked(backendApi.health).mockResolvedValue({ running: true });
    expect(await useBackendStore.getState().reloadConfig()).toBe(false);
    expect(useBackendStore.getState().lastError).toBe('binding failed');
    vi.mocked(backendApi.reloadConfig).mockResolvedValue({ ok: true,
      edictum_sessions: [{ ok: true, platform_id: 'bot', session_id: 's', restart_required: true }] });
    expect(await useBackendStore.getState().reloadConfig()).toBe(false);
  });
});
