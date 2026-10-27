import { cn } from '@/utils/cn';
import { formatTime, LEVELS, type LogBucket, type LogLevel } from './data';

const BAR_CLASS: Record<LogLevel, string> = {
  DEBUG: 'bg-[rgb(var(--rgb-purple)/0.7)]',
  INFO: 'bg-[rgb(var(--rgb-green)/0.78)]',
  WARNING: 'bg-[rgb(var(--rgb-orange)/0.85)]',
  ERROR: 'bg-[rgb(var(--rgb-red)/0.85)]',
  CRITICAL: 'bg-[rgb(var(--rgb-red))]',
};

const DOT_CLASS: Record<LogLevel, string> = {
  DEBUG: 'bg-[rgb(var(--rgb-purple))]',
  INFO: 'bg-[rgb(var(--rgb-green))]',
  WARNING: 'bg-[rgb(var(--rgb-orange))]',
  ERROR: 'bg-[rgb(var(--rgb-red))]',
  CRITICAL: 'bg-[rgb(var(--rgb-red))]',
};

/** 迷你堆叠直方图: 最近 2 分钟, 按级别分色, 图例可当筛选器 */
export function LevelHistogram({ buckets, activeLevels, onToggle, bucketSeconds, mode, notes }: {
  buckets: LogBucket[];
  activeLevels: string[];
  onToggle: (level: LogLevel) => void;
  bucketSeconds: number;
  mode: 'server' | 'local';
  notes: string[];
}) {
  const active = new Set(activeLevels);
  const max = Math.max(1, ...buckets.filter((bucket) => bucket.covered).map((bucket) => bucket.total));
  const totals = LEVELS.reduce<Record<LogLevel, number>>((accumulator, level) => {
    accumulator[level] = buckets.reduce((sum, bucket) => sum + (bucket.covered ? bucket.counts[level] : 0), 0);
    return accumulator;
  }, { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 });
  const windowTotal = buckets.reduce((sum, bucket) => sum + (bucket.covered ? bucket.total : 0), 0);
  const first = buckets.find((bucket) => bucket.covered);
  const last = [...buckets].reverse().find((bucket) => bucket.covered);

  return (
    <div>
      <div className="flex h-[66px] items-end gap-[3px]" role="img"
        aria-label={`最近 2 分钟日志量直方图, 共 ${windowTotal} 条, 峰值 ${max} 条 / ${bucketSeconds} 秒`}>
        {buckets.map((bucket) => {
          const title = bucket.covered
            ? [
              `${formatTime(bucket.start, false)} · 共 ${bucket.total} 条`,
              ...LEVELS.filter((level) => bucket.counts[level] > 0).map((level) => `${level} ${bucket.counts[level]}`),
              ...(bucket.partial ? ['部分来源无覆盖'] : []),
            ].join('\n')
            : `${formatTime(bucket.start, false)} · 统计覆盖前, 未知`;
          return (
            <div
              key={bucket.start}
              title={title}
              className={cn(
                'relative flex h-full min-w-0 flex-1 flex-col-reverse gap-[1px] overflow-hidden rounded-[2px]',
                !bucket.covered && 'opacity-40',
              )}
            >
              {bucket.covered && LEVELS.map((level) => {
                const count = bucket.counts[level];
                if (!count) return null;
                return (
                  <div
                    key={level}
                    className={cn(
                      'w-full flex-none rounded-[1px] transition-[height,opacity] duration-500',
                      BAR_CLASS[level],
                      active.has(level) ? '' : 'opacity-25',
                      bucket.partial && 'opacity-60',
                    )}
                    style={{ height: `${(count / max) * 100}%` }}
                  />
                );
              })}
              {bucket.covered && bucket.total === 0 && (
                <span className="h-[1px] w-full flex-none rounded bg-[var(--glass-border-strong)] opacity-60" />
              )}
              {!bucket.covered && (
                <span aria-hidden className="absolute inset-x-0 top-1/2 border-t border-dashed border-[var(--glass-border-strong)]" />
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-1.5 flex justify-between text-[10px] text-fg-4">
        <span className="num font-mono">{first ? formatTime(first.start, false) : '—'}</span>
        <span>最近 2 分钟</span>
        <span className="num font-mono">{last ? formatTime(last.start, false) : '—'}</span>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-t border-line pt-2.5">
        {LEVELS.map((level) => {
          const on = active.has(level);
          return (
            <button
              key={level}
              type="button"
              aria-pressed={on}
              onClick={() => onToggle(level)}
              title={`${on ? '隐藏' : '显示'} ${level} 日志`}
              className={cn(
                'inline-flex items-center gap-1.5 text-[11px] transition-opacity duration-200',
                on ? 'text-fg-2' : 'text-fg-4 hover:text-fg-3',
              )}
            >
              <span className={cn('h-2 w-2 rounded-full transition-opacity', DOT_CLASS[level], !on && 'opacity-40')} />
              <span className={cn(!on && 'opacity-60')}>{level}</span>
              <span className="num font-mono text-[10.5px] text-fg-4">{totals[level]}</span>
            </button>
          );
        })}
        <span className="ml-auto flex items-center gap-1 text-[10.5px] text-fg-4">
          峰值
          <span className="num font-mono text-fg-3">{max}</span>
          条 / {bucketSeconds}s
        </span>
      </div>
      {notes.length > 0 && (
        <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10.5px] text-fg-4">
          <span className={cn('rounded-full px-1.5 leading-[17px]', mode === 'local' ? 'bg-glass-hover' : 'tint-orange bg-[rgb(var(--tint)/0.14)] text-tint')}>
            {mode === 'local' ? '本地缓冲' : '覆盖提示'}
          </span>
          <span className="min-w-0 break-words">{notes.join(' · ')}</span>
        </p>
      )}
    </div>
  );
}
