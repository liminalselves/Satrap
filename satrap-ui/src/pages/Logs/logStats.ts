import type { LogStats } from '@/api/contracts';
import { emptyLevelCounts, formatTime, isLogLevel, type LogBucket, type LogLevel } from './data';

export interface BufferLog {
  level: string;
  source?: string;
  timestamp?: number;
}

export interface StatsView {
  buckets: LogBucket[];
  byLevel: Record<LogLevel, number>;
  windowTotal: number;
  errorCount: number | null;
  linesPerMinute: number | null;
  coverageFrom: number | null;
  windowFrom: number | null;
  windowTo: number | null;
  bucketSeconds: number;
  complete: boolean;
  partial: boolean;
  mode: 'server' | 'local';
  notes: string[];
}

/** 时间可能来自不同宿主, 统一成秒, 避免毫秒值把窗口拉成几十年 */
export function toSeconds(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  return value > 1e11 ? value / 1000 : value;
}

/** 严格校验新契约响应; 旧宿主返回的通用 ok 对象按不可用处理 */
export function isLogStats(value: unknown): value is LogStats {
  if (!value || typeof value !== 'object') return false;
  const data = value as Partial<LogStats>;
  return typeof data.stream_id === 'string'
    && toSeconds(data.generated_at) !== null
    && toSeconds(data.coverage_from) !== null
    && typeof data.complete === 'boolean'
    && typeof data.window_seconds === 'number'
    && typeof data.bucket_seconds === 'number' && data.bucket_seconds > 0
    && toSeconds(data.window_from) !== null
    && toSeconds(data.window_to) !== null
    && typeof data.error_count === 'number'
    && !!data.by_level && typeof data.by_level === 'object'
    && Array.isArray(data.series);
}

const zeroCounts = (): Record<LogLevel, number> => emptyLevelCounts();

function addCounts(target: Record<LogLevel, number>, source: Record<string, number>) {
  for (const [level, count] of Object.entries(source)) {
    if (isLogLevel(level) && typeof count === 'number') target[level] += count;
  }
}

/** 从有效的服务统计构建 2 分钟直方图; 重启前的时间保持未知 */
function fromServer(stats: LogStats[], requestedSources: number): StatsView {
  const bucketSeconds = Math.max(1, Math.min(300, stats[0].bucket_seconds));
  const rawFrom = Math.min(...stats.map((item) => toSeconds(item.window_from) ?? 0));
  const rawTo = Math.max(...stats.map((item) => toSeconds(item.window_to) ?? 0));
  const start = Math.floor(rawFrom / bucketSeconds) * bucketSeconds;
  const end = Math.max(start + bucketSeconds, Math.ceil(rawTo / bucketSeconds) * bucketSeconds);
  const count = Math.min(720, Math.max(1, Math.round((end - start) / bucketSeconds)));
  const buckets: LogBucket[] = Array.from({ length: count }, (_, index) => ({
    start: start + index * bucketSeconds,
    end: start + (index + 1) * bucketSeconds,
    total: 0,
    counts: zeroCounts(),
    covered: false,
    partial: false,
  }));
  const coverage = new Array<number>(count).fill(0);
  const byLevel = zeroCounts();
  let complete = true;
  const notes: string[] = [];

  for (const stat of stats) {
    const windowFrom = toSeconds(stat.window_from) ?? start;
    const windowTo = toSeconds(stat.window_to) ?? end;
    const coverageFrom = Math.max(windowFrom, toSeconds(stat.coverage_from) ?? windowFrom);
    if (coverageFrom > windowFrom + bucketSeconds) {
      notes.push(`${formatTime(coverageFrom, false)} 重启后的统计起点`);
    }
    if (!stat.complete) {
      complete = false;
      notes.push('来源报告统计不完整');
    }
    for (const point of stat.series) {
      const from = toSeconds(point.from);
      const to = toSeconds(point.to);
      if (from === null || to === null) continue;
      // 重启前的桶即使被补齐也不能当成 0
      if (from < coverageFrom - 0.5 || from >= windowTo + 0.5) continue;
      const index = Math.floor((from - start) / bucketSeconds);
      if (index < 0 || index >= count) continue;
      buckets[index].covered = true;
      buckets[index].total += Object.values(point.by_level).reduce((sum, value) => sum + (typeof value === 'number' ? value : 0), 0);
      addCounts(buckets[index].counts, point.by_level);
      addCounts(byLevel, point.by_level);
    }
    for (let index = 0; index < count; index += 1) {
      const bucket = buckets[index];
      if (bucket.end > coverageFrom + 0.001 && bucket.start < windowTo - 0.001) coverage[index] += 1;
    }
  }

  let windowTotal = 0;
  for (let index = 0; index < count; index += 1) {
    const bucket = buckets[index];
    bucket.covered = coverage[index] > 0;
    bucket.partial = bucket.covered && coverage[index] < requestedSources;
    if (bucket.partial) complete = false;
    windowTotal += bucket.covered ? bucket.total : 0;
  }
  if (stats.length < requestedSources) {
    complete = false;
    notes.push(`${requestedSources - stats.length} 个来源统计不可用`);
  }

  const coverageFrom = Math.max(...stats.map((item) => toSeconds(item.coverage_from) ?? 0));
  const errorCount = stats.reduce((sum, item) => sum + (typeof item.error_count === 'number' ? item.error_count : 0), 0);
  const rates = stats.map((item) => item.lines_per_minute);
  const knownRates = rates.filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
  const linesPerMinute = rates.length && knownRates.length === rates.length
    ? knownRates.reduce((sum, value) => sum + value, 0)
    : knownRates.length ? knownRates.reduce((sum, value) => sum + value, 0) : null;
  if (rates.some((value) => value === null)) {
    complete = false;
    notes.push('部分来源速率未知');
  }

  return {
    buckets, byLevel, windowTotal, errorCount, linesPerMinute,
    coverageFrom: coverageFrom || null,
    windowFrom: rawFrom || null,
    windowTo: rawTo || null,
    bucketSeconds, complete, partial: !complete, mode: 'server',
    notes: [...new Set(notes)],
  };
}

/** 统计接口不可用时只按当前缓冲给出诚实的近似值, 不伪装成完整历史 */
function fromBuffer(logs: BufferLog[], now: number, windowSeconds: number, bucketSeconds: number): StatsView {
  const from = now - windowSeconds;
  const start = Math.floor(from / bucketSeconds) * bucketSeconds;
  const end = Math.ceil(now / bucketSeconds) * bucketSeconds;
  const count = Math.max(1, Math.round((end - start) / bucketSeconds));
  const buckets: LogBucket[] = Array.from({ length: count }, (_, index) => ({
    start: start + index * bucketSeconds,
    end: start + (index + 1) * bucketSeconds,
    total: 0,
    counts: zeroCounts(),
    covered: false,
    partial: false,
  }));
  const byLevel = zeroCounts();
  let oldest = Infinity;
  let windowTotal = 0;
  let errorCount = 0;
  let lastMinute = 0;
  for (const log of logs) {
    const timestamp = log.timestamp;
    if (typeof timestamp !== 'number' || !Number.isFinite(timestamp)) continue;
    if (timestamp >= from && timestamp <= now) {
      oldest = Math.min(oldest, timestamp);
      const index = Math.floor((timestamp - start) / bucketSeconds);
      if (index >= 0 && index < count) {
        buckets[index].total += 1;
        windowTotal += 1;
        if (isLogLevel(log.level)) {
          buckets[index].counts[log.level] += 1;
          byLevel[log.level] += 1;
          if (log.level === 'ERROR' || log.level === 'CRITICAL') errorCount += 1;
        }
      }
      if (now - timestamp <= 60) lastMinute += 1;
    }
  }
  const coverageFrom = Number.isFinite(oldest) ? Math.max(start, oldest) : now;
  for (const bucket of buckets) bucket.covered = bucket.end > coverageFrom + 0.001;
  return {
    buckets, byLevel, windowTotal, errorCount, linesPerMinute: lastMinute,
    coverageFrom, windowFrom: from, windowTo: now, bucketSeconds,
    complete: false, partial: false, mode: 'local',
    notes: ['服务统计不可用, 按当前缓冲估算'],
  };
}

export function buildStatsView(
  stats: LogStats[],
  requestedSources: number,
  logs: BufferLog[],
  now: number,
  windowSeconds = 120,
  bucketSeconds = 10,
): StatsView {
  const valid = stats.filter(isLogStats);
  if (valid.length) return fromServer(valid, requestedSources);
  return fromBuffer(logs, now, windowSeconds, bucketSeconds);
}
