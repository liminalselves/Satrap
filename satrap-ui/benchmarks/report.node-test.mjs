import assert from 'node:assert/strict';
import test from 'node:test';
import { summarizeRuns } from './metrics.mjs';
import { createComparisonReport } from './report.mjs';

function createResult(averageFps, p95FrameMs) {
  return {
    metadata: {
      generated_at: '2026-08-31T00:00:00.000Z',
      browser: 'benchmark-browser',
      iterations: 5,
      warmups: 1,
    },
    scenarios: [{
      id: 'example',
      label: '示例场景',
      summary: {
        average_fps: { median: averageFps },
        p95_frame_ms: { median: p95FrameMs },
      },
    }],
  };
}

test('汇总结果返回中位数、P95 和最差值', () => {
  const summary = summarizeRuns([
    { duration_ms: 10 },
    { duration_ms: 30 },
    { duration_ms: 20 },
  ]);
  assert.deepEqual(summary.duration_ms, { median: 20, p95: 30, worst: 30 });
});

test('首次基线报告包含场景摘要', () => {
  const report = createComparisonReport(createResult(60, 16.7), null);
  assert.match(report, /当前结果已作为首份基线/);
  assert.match(report, /示例场景/);
});

test('超过阈值的 FPS 和帧耗时变化被判定为回退', () => {
  const baseline = createResult(60, 16);
  const current = createResult(48, 20);
  const report = createComparisonReport(current, baseline);
  assert.match(report, /回退项: 2/);
});
