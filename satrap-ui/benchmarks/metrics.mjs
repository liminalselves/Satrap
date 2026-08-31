const CDP_DURATION_METRICS = [
  'TaskDuration',
  'ScriptDuration',
  'LayoutDuration',
  'RecalcStyleDuration',
];

function percentile(values, ratio) {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(sorted.length - 1, Math.ceil(sorted.length * ratio) - 1);
  return sorted[Math.max(0, index)];
}

function round(value, digits = 3) {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

async function readCdpMetrics(session) {
  const response = await session.send('Performance.getMetrics');
  return Object.fromEntries(response.metrics.map((metric) => [metric.name, metric.value]));
}

async function startPageSampling(page) {
  await page.evaluate(() => {
    const samples = {
      active: true,
      frameIntervals: [],
      heapSamples: [],
      longTasks: [],
      mutationRecords: 0,
      lastFrame: null,
      rafHandle: 0,
      heapTimer: 0,
      longTaskObserver: null,
      mutationObserver: null,
    };

    const collectFrame = (timestamp) => {
      if (!samples.active) return;
      if (samples.lastFrame !== null) samples.frameIntervals.push(timestamp - samples.lastFrame);
      samples.lastFrame = timestamp;
      samples.rafHandle = requestAnimationFrame(collectFrame);
    };
    samples.rafHandle = requestAnimationFrame(collectFrame);

    const collectHeap = () => {
      if (performance.memory?.usedJSHeapSize !== undefined) {
        samples.heapSamples.push(performance.memory.usedJSHeapSize);
      }
    };
    collectHeap();
    samples.heapTimer = window.setInterval(collectHeap, 50);

    try {
      samples.longTaskObserver = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) samples.longTasks.push(entry.duration);
      });
      samples.longTaskObserver.observe({ entryTypes: ['longtask'] });
    } catch {
      samples.longTaskObserver = null;
    }

    samples.mutationObserver = new MutationObserver((records) => {
      samples.mutationRecords += records.length;
    });
    samples.mutationObserver.observe(document.documentElement, {
      attributes: true,
      characterData: true,
      childList: true,
      subtree: true,
    });
    window.__satrapBenchmarkSamples = samples;
  });
}

async function resetPageSampling(page) {
  await page.evaluate(() => {
    const samples = window.__satrapBenchmarkSamples;
    if (!samples) return;
    samples.frameIntervals = [];
    samples.heapSamples = [];
    samples.longTasks = [];
    samples.mutationRecords = 0;
    samples.lastFrame = null;
    if (performance.memory?.usedJSHeapSize !== undefined) {
      samples.heapSamples.push(performance.memory.usedJSHeapSize);
    }
  });
}

async function stopPageSampling(page) {
  return page.evaluate(() => {
    const samples = window.__satrapBenchmarkSamples;
    if (!samples) return null;
    samples.active = false;
    cancelAnimationFrame(samples.rafHandle);
    clearInterval(samples.heapTimer);
    samples.longTaskObserver?.disconnect();
    samples.mutationObserver?.disconnect();
    if (performance.memory?.usedJSHeapSize !== undefined) {
      samples.heapSamples.push(performance.memory.usedJSHeapSize);
    }
    const result = {
      frameIntervals: samples.frameIntervals,
      heapSamples: samples.heapSamples,
      longTasks: samples.longTasks,
      mutationRecords: samples.mutationRecords,
      domNodes: document.getElementsByTagName('*').length,
    };
    delete window.__satrapBenchmarkSamples;
    return result;
  });
}

/** 采集一次真实浏览器场景的性能数据 */
export async function measureScenario(page, session, action, settleMs = 250) {
  await session.send('Performance.enable');
  await startPageSampling(page);
  await page.waitForTimeout(300);   // 排除页面首次进入前台时的异常长帧
  await resetPageSampling(page);
  const cdpStart = await readCdpMetrics(session);
  const wallStart = performance.now();
  const custom = await action();
  if (settleMs > 0) await page.waitForTimeout(settleMs);
  const wallDuration = performance.now() - wallStart;
  const cdpEnd = await readCdpMetrics(session);
  const samples = await stopPageSampling(page);
  if (!samples) throw new Error('页面采样器未返回结果');

  const frameIntervals = samples.frameIntervals;
  const meanFrame = frameIntervals.length > 0
    ? frameIntervals.reduce((sum, value) => sum + value, 0) / frameIntervals.length
    : 0;
  const p50Frame = percentile(frameIntervals, 0.5);
  const droppedThreshold = Math.max(20, p50Frame * 1.5);
  const heapSamples = samples.heapSamples;
  const result = {
    duration_ms: round(wallDuration),
    average_fps: meanFrame > 0 ? round(1000 / meanFrame) : 0,
    p95_frame_ms: round(percentile(frameIntervals, 0.95)),
    longest_frame_ms: round(Math.max(0, ...frameIntervals)),
    dropped_frames: frameIntervals.filter((value) => value > droppedThreshold).length,
    frame_samples: frameIntervals.length,
    long_tasks: samples.longTasks.length,
    long_task_total_ms: round(samples.longTasks.reduce((sum, value) => sum + value, 0)),
    renderer_mutations: samples.mutationRecords,
    dom_nodes: samples.domNodes,
    js_heap_start_mb: heapSamples.length > 0 ? round(heapSamples[0] / 1_048_576) : null,
    js_heap_peak_mb: heapSamples.length > 0 ? round(Math.max(...heapSamples) / 1_048_576) : null,
    js_heap_end_mb: heapSamples.length > 0 ? round(heapSamples.at(-1) / 1_048_576) : null,
  };

  for (const name of CDP_DURATION_METRICS) {
    const key = `${name.replace(/Duration$/, '').replace(/([A-Z])/g, '_$1').toLowerCase().replace(/^_/, '')}_ms`;
    result[key] = round(((cdpEnd[name] ?? 0) - (cdpStart[name] ?? 0)) * 1000);
  }
  return { ...result, ...(custom ?? {}) };
}

export function summarizeRuns(runs) {
  const numericKeys = new Set();
  for (const run of runs) {
    for (const [key, value] of Object.entries(run)) {
      if (typeof value === 'number' && Number.isFinite(value)) numericKeys.add(key);
    }
  }
  return Object.fromEntries([...numericKeys].map((key) => {
    const values = runs.map((run) => run[key]).filter((value) => typeof value === 'number');
    return [key, {
      median: round(percentile(values, 0.5)),
      p95: round(percentile(values, 0.95)),
      worst: round(Math.max(...values)),
    }];
  }));
}
