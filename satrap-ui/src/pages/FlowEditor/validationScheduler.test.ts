import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createValidationScheduler } from './validationScheduler';

/** 可手动完成的请求, 记录收到的中止信号 */
function deferredRun() {
  const calls: Array<{ signal: AbortSignal; resolve: (value: string) => void; reject: (error: unknown) => void }> = [];
  const run = (signal: AbortSignal) => new Promise<string>((resolve, reject) => { calls.push({ signal, resolve, reject }); });
  return { calls, run };
}

describe('createValidationScheduler', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('runs once 600 ms after the last schedule call', async () => {
    const { calls, run } = deferredRun();
    const onResult = vi.fn();
    const scheduler = createValidationScheduler({ delay: 600, run, onResult, onError: vi.fn() });
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(400);
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(599);
    expect(calls).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(calls).toHaveLength(1);
    calls[0].resolve('ok');
    await vi.runAllTimersAsync();
    expect(onResult).toHaveBeenCalledWith('ok');
  });

  it('aborts the in-flight request and ignores its late result', async () => {
    const { calls, run } = deferredRun();
    const onResult = vi.fn();
    const onError = vi.fn();
    const scheduler = createValidationScheduler({ delay: 600, run, onResult, onError });
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(600);
    scheduler.schedule();
    await vi.advanceTimersByTimeAsync(600);
    expect(calls).toHaveLength(2);
    expect(calls[0].signal.aborted).toBe(true);
    expect(calls[1].signal.aborted).toBe(false);
    calls[0].resolve('stale');
    calls[1].resolve('fresh');
    await vi.runAllTimersAsync();
    expect(onResult.mock.calls).toEqual([['fresh']]);
    expect(onError).not.toHaveBeenCalled();
  });

  it('runNow cancels the pending timer, and errors after abort are not reported', async () => {
    const { calls, run } = deferredRun();
    const onError = vi.fn();
    const scheduler = createValidationScheduler({ delay: 600, run, onResult: vi.fn(), onError });
    scheduler.schedule();
    const now = scheduler.runNow();
    expect(calls).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(calls).toHaveLength(1);
    scheduler.dispose();
    expect(calls[0].signal.aborted).toBe(true);
    calls[0].reject(new Error('canceled'));
    await expect(now).resolves.toBeNull();
    expect(onError).not.toHaveBeenCalled();
  });

  it('reports a failure that was not aborted', async () => {
    const { calls, run } = deferredRun();
    const onError = vi.fn();
    const scheduler = createValidationScheduler({ delay: 600, run, onResult: vi.fn(), onError });
    const now = scheduler.runNow();
    calls[0].reject(new Error('503'));
    await expect(now).resolves.toBeNull();
    expect(onError).toHaveBeenCalledTimes(1);
  });
});
