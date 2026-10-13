import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { LogBuffer } from "@/utils/logBuffer";
import { createAnimationFrameLimiter } from "@/utils/frameLimiter";
import { startVisiblePoll } from "@/utils/visiblePoll";
import { calculateReflectState, getPointerCellKey, getReflectCellKeys } from "@/hooks/glassReflectGeometry";
import { updateReflectState } from "@/hooks/useGlassReflect";

describe("utils/logBuffer", () => {
  describe('有界日志缓冲', () => {
    it('突发输入只保留最新上限且顺序完整, 未更新的读取保留引用', () => {
      const buffer = new LogBuffer<number>(5000);
      for (let index = 0; index < 12000; index++) buffer.append(index);
      const snapshot = buffer.read();
      expect(snapshot.logs).toEqual(Array.from({ length: 5000 }, (_, index) => index + 7000));
      expect(buffer.read().logs).toBe(snapshot.logs);
      buffer.append(12000);
      expect(buffer.read().logs[0]).toBe(7001);
      expect(snapshot.logs[0]).toBe(7000);
    });

    it('暂停固定显示快照, 超过保留上限仍正确计数, 恢复显示最新完整缓冲', () => {
      const buffer = new LogBuffer<number>(3);
      [1, 2, 3].forEach((item) => buffer.append(item));
      buffer.pause(true);
      const frozen = buffer.read().logs;
      [4, 5, 6, 7].forEach((item) => buffer.append(item));
      expect(buffer.read()).toEqual({ logs: [1, 2, 3], bufferedCount: 4 });
      expect(buffer.read().logs).toBe(frozen);
      buffer.pause(false);
      expect(buffer.read()).toEqual({ logs: [5, 6, 7], bufferedCount: 0 });
    });

    it('暂停时清空包括未提交的缓冲, 后续输入不回放旧日志', () => {
      const buffer = new LogBuffer<string>(2);
      buffer.append('old'); buffer.pause(true); buffer.append('pending'); buffer.clear();
      buffer.append('new');
      expect(buffer.read()).toEqual({ logs: [], bufferedCount: 1 });
      buffer.pause(false);
      expect(buffer.read().logs).toEqual(['new']);
    });
  });
});

describe("utils/frameLimiter", () => {
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
});

describe("utils/visiblePoll", () => {
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
});

describe("hooks/glassReflectGeometry", () => {
  describe('glassReflectGeometry', () => {
    const rect = { left: 100, top: 100, right: 300, bottom: 200 };

    it('计算元素内部的指针坐标和悬浮状态', () => {
      expect(calculateReflectState(rect, 160, 140, 80)).toEqual({
        x: 60,
        y: 40,
        isHovering: true,
        isInRange: true,
      });
    });

    it('区分临近状态和范围外状态', () => {
      expect(calculateReflectState(rect, 340, 140, 80).isInRange).toBe(true);
      expect(calculateReflectState(rect, 400, 140, 80).isInRange).toBe(false);
    });

    it('元素扩展范围覆盖指针所在空间分区', () => {
      const keys = getReflectCellKeys(rect, 150);
      expect(keys).toContain(getPointerCellKey(50, 50));
      expect(keys).toContain(getPointerCellKey(350, 250));
      expect(keys).not.toContain(getPointerCellKey(800, 800));
    });
  });
});

describe("hooks/useGlassReflect", () => {
  describe('updateReflectState', () => {
    it('仅在坐标或视觉状态变化时写入 DOM', () => {
      const styleWrites: string[] = [];
      const classWrites: string[] = [];
      const classes = new Set<string>();
      const element = {
        style: {
          setProperty: (name: string, value: string) => styleWrites.push(`${name}:${value}`),
        },
        classList: {
          add: (...names: string[]) => {
            names.forEach((name) => classes.add(name));
            classWrites.push(`add:${names.join(',')}`);
          },
          remove: (...names: string[]) => {
            names.forEach((name) => classes.delete(name));
            classWrites.push(`remove:${names.join(',')}`);
          },
        },
      } as unknown as HTMLElement;
      const item: Parameters<typeof updateReflectState>[0] = {
        element,
        reflectRange: 80,
        reflectSize: 150,
        rect: { left: 100, top: 100, right: 300, bottom: 200 },
        visible: true,
        cellKeys: [],
        visualState: 'idle',
        lastReflectX: null,
        lastReflectY: null,
      };

      expect(updateReflectState(item, 160, 140)).toBe(true);
      expect(styleWrites).toEqual(['--mouse-x:60px', '--mouse-y:40px']);
      expect(classes.has('glass-hovering')).toBe(true);
      const initialClassWrites = classWrites.length;

      expect(updateReflectState(item, 160, 140)).toBe(true);
      expect(styleWrites).toHaveLength(2);
      expect(classWrites).toHaveLength(initialClassWrites);

      expect(updateReflectState(item, 340, 140)).toBe(true);
      expect(styleWrites).toHaveLength(3);
      expect(classes.has('glass-nearby')).toBe(true);

      expect(updateReflectState(item, 500, 140)).toBe(false);
      const clearedClassWrites = classWrites.length;
      expect(classes.size).toBe(0);
      expect(updateReflectState(item, 500, 140)).toBe(false);
      expect(styleWrites).toHaveLength(3);
      expect(classWrites).toHaveLength(clearedClassWrites);
    });
  });
});
