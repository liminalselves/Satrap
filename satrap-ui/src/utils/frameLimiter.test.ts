import { describe, expect, it } from 'vitest';
import { createAnimationFrameLimiter } from './frameLimiter';

function createFrameHarness(maxFps = 90) {
  let nextHandle = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  const limiter = createAnimationFrameLimiter({
    maxFps,
    requestFrame: (callback) => {
      const handle = nextHandle;
      nextHandle += 1;
      callbacks.set(handle, callback);
      return handle;
    },
    cancelFrame: (handle) => {
      callbacks.delete(handle);
    },
  });

  return {
    limiter,
    runFrame(timestamp: number) {
      const entry = callbacks.entries().next().value as [number, FrameRequestCallback] | undefined;
      if (!entry) return;
      callbacks.delete(entry[0]);
      entry[1](timestamp);
    },
    pendingFrames: () => callbacks.size,
  };
}

describe('createAnimationFrameLimiter', () => {
  it('在高刷新率下将提交频率限制在 90 FPS 以内', () => {
    const harness = createFrameHarness();
    let commits = 0;
    for (let index = 0; index < 144; index += 1) {
      harness.limiter.schedule(() => {
        commits += 1;
      });
      harness.runFrame(index * (1000 / 144));
    }
    expect(commits).toBeLessThanOrEqual(90);
  });

  it('在 60Hz 刷新率下不额外降低提交频率', () => {
    const harness = createFrameHarness();
    let commits = 0;
    for (let index = 0; index < 60; index += 1) {
      harness.limiter.schedule(() => {
        commits += 1;
      });
      harness.runFrame(index * (1000 / 60));
    }
    expect(commits).toBe(60);
  });

  it('合并等待期间的请求并执行最新回调', () => {
    const harness = createFrameHarness();
    const results: string[] = [];
    harness.limiter.schedule(() => results.push('first'));
    harness.runFrame(0);
    harness.limiter.schedule(() => results.push('stale'));
    harness.runFrame(5);
    harness.limiter.schedule(() => results.push('latest'));
    harness.runFrame(12);
    expect(results).toEqual(['first', 'latest']);
  });

  it('取消后不会执行等待中的回调', () => {
    const harness = createFrameHarness();
    let commits = 0;
    harness.limiter.schedule(() => {
      commits += 1;
    });
    harness.limiter.cancel();
    expect(harness.pendingFrames()).toBe(0);
    harness.runFrame(20);
    expect(commits).toBe(0);
  });
});
