/**
 * 延迟检查调度: schedule 在 delay 内反复调用只执行最后一次; 新的执行会中止在途请求
 *
 * 不依赖 DOM 和 React, 由 useFlowValidation 持有; 结果只在请求未被中止时交给 onResult
 */
export interface ValidationScheduler<T> {
  /** 安排一次延迟执行, 取消之前尚未开始的执行 */
  schedule: () => void;
  /** 立即执行, 中止在途请求; 被后续执行中止时返回 null */
  runNow: () => Promise<T | null>;
  /** 取消计时并中止在途请求 */
  dispose: () => void;
}

export function createValidationScheduler<T>({ delay, run, onResult, onError }: {
  /** 编辑停止后多久执行, 毫秒 */
  delay: number;
  /** 实际请求; 必须响应 signal 中止 */
  run: (signal: AbortSignal) => Promise<T>;
  /** 未被中止的成功结果 */
  onResult: (result: T) => void;
  /** 未被中止的失败 */
  onError: (error: unknown) => void;
}): ValidationScheduler<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let controller: AbortController | null = null;

  const runNow = async (): Promise<T | null> => {
    if (timer !== null) { clearTimeout(timer); timer = null; }
    controller?.abort();
    const own = new AbortController();
    controller = own;
    try {
      const result = await run(own.signal);
      if (own.signal.aborted) return null;
      onResult(result);
      return result;
    } catch (error) {
      if (!own.signal.aborted) onError(error);
      return null;
    }
  };

  return {
    schedule: () => {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => { timer = null; void runNow(); }, delay);
    },
    runNow,
    dispose: () => {
      if (timer !== null) { clearTimeout(timer); timer = null; }
      controller?.abort();
    },
  };
}
