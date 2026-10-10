import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { startVisiblePoll } from './visiblePoll';

let page: EventTarget & { hidden: boolean };
beforeEach(() => { vi.useFakeTimers(); page = Object.assign(new EventTarget(), { hidden: false }); vi.stubGlobal('document', page); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('可见性轮询', () => {
  it('可延后首轮, 保留执行截止条件且恢复可见不重启已结束轮询', async () => {
    const startedAt = Date.now();
    const task = vi.fn(async () => undefined);
    const poll = startVisiblePoll(task, { interval: 2000, immediate: false,
      shouldContinue: () => Date.now() - startedAt < 60000 });
    expect(task).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1999);
    expect(task).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(1);
    page.hidden = true; page.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60000);
    page.hidden = false; page.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(1);
    poll.stop();
  });

  it('隐藏后停止定时读取, 恢复只补查一次, 停止时撤销读取', async () => {
    const task = vi.fn(async (_signal: AbortSignal) => undefined);
    const poll = startVisiblePoll(task, { interval: 5000 });
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1);
    page.hidden = true; page.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(1);
    page.hidden = false; page.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(2);
    poll.stop();
    expect(task.mock.calls[0]?.[0].aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(2);
  });

  it('慢请求不重叠, 失败退避且正常恢复原周期', async () => {
    let resolve!: () => void;
    let signal!: AbortSignal;
    const task = vi.fn((value: AbortSignal) => { signal = value; return new Promise<void>((done) => { resolve = done; }); });
    const onError = vi.fn();
    const poll = startVisiblePoll(task, { interval: 5000, onError });
    poll.request(); poll.request();
    await vi.advanceTimersByTimeAsync(20000);
    expect(task).toHaveBeenCalledTimes(1);
    task.mockRejectedValueOnce(new Error('temporarily unavailable')).mockResolvedValue(undefined);
    resolve();
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).toHaveBeenCalledTimes(2);
    expect(onError).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(9999);
    expect(task).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).toHaveBeenCalledTimes(4);
    poll.stop();
    expect(signal.aborted).toBe(true);
  });
});
