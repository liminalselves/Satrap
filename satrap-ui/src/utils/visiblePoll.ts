interface PollOptions {
  interval: number | (() => number);
  onError?: (error: unknown) => void;
  maxBackoff?: number;
}

export function startVisiblePoll(task: (signal: AbortSignal) => Promise<unknown>, options: PollOptions) {
  let active = true;
  let running = false;
  let followup = false;
  let failures = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const controller = new AbortController();
  const request = async (afterRunning = false) => {
    if (!active || document.hidden) return;
    if (running) { followup ||= afterRunning; return; }
    if (timer) clearTimeout(timer);
    timer = undefined;
    running = true;
    try { await task(controller.signal); failures = 0; }
    catch (error) { if (active && !controller.signal.aborted) { failures++; options.onError?.(error); } }
    finally {
      running = false;
      if (active && !document.hidden) {
        const interval = typeof options.interval === 'function' ? options.interval() : options.interval;
        const delay = followup ? 0 : Math.min(options.maxBackoff || 60000, interval * 2 ** Math.min(failures, 4));
        followup = false;
        timer = setTimeout(() => void request(), delay);
      }
    }
  };
  const visibility = () => {
    if (document.hidden) { if (timer) clearTimeout(timer); timer = undefined; }
    else void request(true);
  };
  document.addEventListener('visibilitychange', visibility);
  void request();
  return {
    request: () => void request(),
    stop: () => {
      active = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', visibility);
      controller.abort();
    },
  };
}
