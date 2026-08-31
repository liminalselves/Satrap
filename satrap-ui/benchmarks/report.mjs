const HIGHER_IS_BETTER = new Set(['average_fps', 'stream_integrity']);
const REGRESSION_LIMITS = {
  average_fps: 0.1,
  p95_frame_ms: 0.1,
  longest_frame_ms: 0.15,
  dropped_frames: 0.15,
  long_task_total_ms: 0.1,
  task_ms: 0.1,
  script_ms: 0.1,
  layout_ms: 0.1,
  recalc_style_ms: 0.1,
  js_heap_peak_mb: 0.15,
  renderer_mutations: 0.1,
  stream_integrity: 0,
};

const REPORT_METRICS = [
  'average_fps',
  'p95_frame_ms',
  'longest_frame_ms',
  'dropped_frames',
  'long_tasks',
  'long_task_total_ms',
  'task_ms',
  'script_ms',
  'layout_ms',
  'recalc_style_ms',
  'js_heap_peak_mb',
  'dom_nodes',
  'renderer_mutations',
  'stream_integrity',
];

function formatNumber(value) {
  if (value === null || value === undefined) return 'null';
  return Number(value).toLocaleString('en-US', { maximumFractionDigits: 3 });
}

function calculateChange(baseline, current) {
  if (!Number.isFinite(baseline) || !Number.isFinite(current) || baseline === 0) return null;
  return (current - baseline) / Math.abs(baseline);
}

function isRegression(metric, change) {
  if (change === null) return false;
  const limit = REGRESSION_LIMITS[metric];
  if (limit === undefined) return false;
  return HIGHER_IS_BETTER.has(metric) ? change < -limit : change > limit;
}

/** 生成当前结果相对基线的 Markdown 报告 */
export function createComparisonReport(current, baseline) {
  const lines = [
    '# Satrap UI Benchmark 对比',
    '',
    `- 当前结果: ${current.metadata.generated_at}`,
    `- 基线结果: ${baseline?.metadata?.generated_at ?? '首次建立'}`,
    `- 浏览器: ${current.metadata.browser}`,
    `- 迭代: ${current.metadata.iterations} 次正式运行, ${current.metadata.warmups} 次预热`,
    '',
  ];
  if (!baseline) {
    lines.push(
      '当前结果已作为首份基线, 后续运行将显示性能变化',
      '',
      '| 场景 | 平均 FPS | P95 帧耗时 | Long Task | 脚本耗时 | 峰值内存 | DOM 节点 |',
      '|---|---:|---:|---:|---:|---:|---:|',
    );
    for (const scenario of current.scenarios) {
      const value = (metric) => formatNumber(scenario.summary[metric]?.median);
      lines.push(`| ${scenario.label} | ${value('average_fps')} | ${value('p95_frame_ms')} ms | ${value('long_tasks')} | ${value('script_ms')} ms | ${value('js_heap_peak_mb')} MB | ${value('dom_nodes')} |`);
    }
    lines.push('');
    return lines.join('\n');
  }

  let regressionCount = 0;
  lines.push('| 场景 | 指标 | 基线中位数 | 当前中位数 | 变化 | 判定 |', '|---|---:|---:|---:|---:|---:|');
  for (const scenario of current.scenarios) {
    const baselineScenario = baseline.scenarios.find((item) => item.id === scenario.id);
    if (!baselineScenario) continue;
    for (const metric of REPORT_METRICS) {
      const currentValue = scenario.summary[metric]?.median;
      const baselineValue = baselineScenario.summary[metric]?.median;
      if (!Number.isFinite(currentValue) || !Number.isFinite(baselineValue)) continue;
      const change = calculateChange(baselineValue, currentValue);
      const regression = isRegression(metric, change);
      if (regression) regressionCount += 1;
      lines.push(`| ${scenario.label} | ${metric} | ${formatNumber(baselineValue)} | ${formatNumber(currentValue)} | ${change === null ? 'n/a' : `${(change * 100).toFixed(1)}%`} | ${regression ? '回退' : '通过'} |`);
    }
  }
  lines.push('', `回退项: ${regressionCount}`, '');
  return lines.join('\n');
}
