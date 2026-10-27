import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { BarChart3, Boxes, Coins, Cpu, Database, Gauge, Info, RotateCw, Timer, Zap } from 'lucide-react';
import {
  Badge, Button, Card, CountUp, EmptyState, PageHeader, Skeleton, Sparkline, StatusDot, Tabs, Tooltip,
  type Tone,
} from '@/components/kit';
import {
  sourceStateLabels, statsApi,
  type AggregateMeta, type MetricBucket, type ModelMetrics, type Overview, type Purpose,
  type Service, type SessionMetrics, type SourceCoverage, type StatsWindowQuery, type UsageTotals,
} from '@/api/contracts';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { cn } from '@/utils/cn';

type Range = 'today' | '7d' | '30d';
type PurposeFilter = Purpose | 'all';

const RANGE_LABEL: Record<Range, string> = { today: '今日', '7d': '近 7 日', '30d': '近 30 日' };
const SERVICE_LABELS: Record<Service, string> = { control: '控制服务', backend: '后端服务', chat: 'Chat 服务' };

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

const fmt = (value: number | null): string => value === null ? '—'
  : value >= 1e6 ? `${(value / 1e6).toFixed(2)}M`
    : value >= 1e3 ? `${(value / 1e3).toFixed(1)}K` : String(value);

const ms = (value: number | null): string => value === null ? '—'
  : value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;

const pct = (part: number, whole: number): number => whole ? Math.round((part / whole) * 100) : 0;

const formatPercent = (value: number): string => `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`;

/** 今日不传 from/to 用后端默认窗口; 跨天范围从本地今日零点回推 N-1 天 */
function statsWindowQuery(range: Range, purpose: PurposeFilter): StatsWindowQuery {
  if (range === 'today') return { bucket: 'hour', purpose };
  const days = range === '7d' ? 7 : 30;
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const from = Math.floor((midnight.getTime() - (days - 1) * 86_400_000) / 1000);
  return { from, to: Math.floor(Date.now() / 1000), bucket: 'day', purpose };
}

/** 趋势点只按已上报的总量作图, 未上报按 0 占位 */
function tokenTrend(buckets: MetricBucket[] | undefined): number[] {
  if (!Array.isArray(buckets)) return [];
  return buckets.map((bucket) => finite(bucket?.usage?.total_tokens) ?? 0);
}

function sourceTone(state: SourceCoverage['state']): Tone {
  if (state === 'available') return 'green';
  if (state === 'stale' || state === 'auth_required') return 'orange';
  return 'red';
}

function CoverageNotice({ meta }: { meta: AggregateMeta | null }) {
  if (!meta) return null;
  const sources = Array.isArray(meta.sources) ? meta.sources : [];
  const warnings = Array.isArray(meta.warnings) ? meta.warnings : [];
  const complete = meta.complete !== false;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs animate-rise" style={{ animationDelay: '40ms' }}>
      <Badge tone={complete ? 'green' : 'orange'} dot>{complete ? '统计完整' : '数据不完整'}</Badge>
      {sources.map((source) => (
        <span key={source.service} className="flex items-center gap-1.5 rounded-full border border-line bg-glass px-2.5 py-1 text-fg-3">
          <StatusDot tone={sourceTone(source.state)} className="!h-1.5 !w-1.5" />
          {SERVICE_LABELS[source.service] ?? source.service} · {sourceStateLabels[source.state] ?? source.state}
        </span>
      ))}
      {warnings.length > 0 && (
        <span title={warnings.join('\n')} className="cursor-help text-orange underline decoration-dotted underline-offset-2">
          {warnings.length} 条提示
        </span>
      )}
      <span className="ml-auto flex items-center gap-1.5 text-fg-4"><Info className="h-3.5 w-3.5" />缓存 Token 属于输入的一部分, 不重复计入总量</span>
    </div>
  );
}

function Coverage({ usage }: { usage: UsageTotals | null }) {
  if (!usage) return <span className="num text-[11px] text-fg-4">—</span>;
  const calls = finite(usage.calls) ?? 0;
  const reported = finite(usage.usage_reported_calls) ?? 0;
  const covered = pct(reported, calls);
  return (
    <Tooltip content={`${reported}/${calls} 次调用返回了用量; 其余调用的 Token 未知, 未计入总量`}>
      <span className={cn('num inline-flex items-center gap-1 text-[11px]', covered < 100 ? 'text-orange' : 'text-fg-4')}>
        <span className="h-1 w-8 overflow-hidden rounded-full bg-glass-hover">
          <span
            className={cn('block h-full rounded-full', covered < 100 ? 'bg-[rgb(var(--rgb-orange))]' : 'bg-[rgb(var(--rgb-green))]')}
            style={{ width: `${covered}%` }}
          />
        </span>
        {covered}%
      </span>
    </Tooltip>
  );
}

function ShareBar({ input, output, total }: { input: number | null; output: number | null; total: number | null }) {
  if (input === null && output === null) return <span className="block h-1.5 rounded-full border border-dashed border-line" />;
  const whole = total && total > 0 ? total : (input ?? 0) + (output ?? 0);
  return (
    <span className="flex h-1.5 overflow-hidden rounded-full bg-glass-hover">
      <span className="bg-[rgb(var(--rgb-accent))]" style={{ width: `${pct(input ?? 0, whole)}%` }} />
      <span className="bg-[rgb(var(--rgb-purple))]" style={{ width: `${pct(output ?? 0, whole)}%` }} />
    </span>
  );
}

/** 输入/输出堆叠柱: 悬停显示该时段明细, 未知时段画虚线占位而不是 0 */
function TokenChart({ buckets, range }: { buckets: MetricBucket[]; range: Range }) {
  const [hover, setHover] = useState<number | null>(null);
  const values = buckets.map((bucket) => {
    const input = finite(bucket?.usage?.input_tokens);
    const output = finite(bucket?.usage?.output_tokens);
    const total = finite(bucket?.usage?.total_tokens);
    const stacked = (input ?? 0) + (output ?? 0);
    return { input, output, reported: input !== null || output !== null || total !== null, stacked, basis: stacked > 0 ? stacked : total ?? 0 };
  });
  const max = Math.max(1, ...values.map((value) => value.basis));
  const label = (bucket: MetricBucket) => {
    const from = finite(bucket?.from);
    if (from === null) return '—';
    const date = new Date(from * 1000);
    return range === 'today'
      ? `${String(date.getHours()).padStart(2, '0')}:00`
      : `${date.getMonth() + 1}/${date.getDate()}`;
  };
  const activeIndex = hover !== null && hover < buckets.length ? hover : null;
  const active = activeIndex === null ? null : { bucket: buckets[activeIndex], ...values[activeIndex] };

  return (
    <div>
      <div className="mb-3 flex h-10 items-end justify-between gap-4">
        <div className="flex items-center gap-4 text-xs text-fg-3">
          <span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm bg-[rgb(var(--rgb-accent))]" />输入</span>
          <span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm bg-[rgb(var(--rgb-purple))]" />输出</span>
        </div>
        <div className={cn('text-right transition-opacity', active ? 'opacity-100' : 'opacity-0')} aria-live="polite">
          {active && <>
            <div className="num text-[11px] text-fg-4">{label(active.bucket)} · {fmt(finite(active.bucket?.usage?.calls))} 次调用</div>
            <div className="num text-sm text-fg">输入 <b>{fmt(active.input)}</b> · 输出 <b>{fmt(active.output)}</b></div>
          </>}
        </div>
      </div>
      <div className="flex h-44 items-end gap-[3px]" onMouseLeave={() => setHover(null)} role="img" aria-label="Token 用量趋势">
        {buckets.map((bucket, index) => {
          const value = values[index];
          const height = (value.basis / max) * 100;
          const outputShare = value.stacked > 0 ? ((value.output ?? 0) / value.stacked) * 100 : 0;
          return (
            <button
              key={bucket.from}
              type="button"
              aria-label={`${label(bucket)} 输入 ${fmt(value.input)} 输出 ${fmt(value.output)}`}
              onMouseEnter={() => setHover(index)}
              onFocus={() => setHover(index)}
              className="group relative flex h-full flex-1 flex-col justify-end rounded-t-md outline-none"
              style={{ '--i': index } as CSSProperties}
            >
              <span className={cn('absolute inset-0 rounded-md transition-colors', hover === index && 'bg-[rgb(var(--rgb-accent)/0.08)]')} />
              {!value.reported ? (
                <span className="relative h-1/4 rounded-t-md border border-dashed border-line" />
              ) : (
                <span className="relative flex flex-col justify-end overflow-hidden rounded-t-md animate-rise" style={{ height: `${height}%`, animationDelay: `${index * 18}ms` }}>
                  {value.stacked > 0 ? (
                    <>
                      <span className="bg-[linear-gradient(180deg,rgb(var(--rgb-purple)/0.95),rgb(var(--rgb-purple)/0.7))]" style={{ height: `${outputShare}%` }} />
                      <span className="flex-1 bg-[linear-gradient(180deg,rgb(var(--rgb-accent)/0.95),rgb(var(--rgb-accent)/0.65))]" />
                    </>
                  ) : (
                    <span className="flex-1 bg-[linear-gradient(180deg,rgb(var(--rgb-accent)/0.95),rgb(var(--rgb-accent)/0.65))]" />
                  )}
                </span>
              )}
            </button>
          );
        })}
      </div>
      <div className="mt-2 flex justify-between text-[10.5px] text-fg-4 num">
        <span>{label(buckets[0])}</span>
        {buckets.length > 2 && <span>{label(buckets[Math.floor(buckets.length / 2)])}</span>}
        <span>{label(buckets[buckets.length - 1])}</span>
      </div>
    </div>
  );
}

function UsageSkeleton() {
  return (
    <>
      <div className="stagger grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((index) => (
          <Card key={index} padding="md" style={{ '--i': index } as CSSProperties}>
            <Skeleton className="h-9 w-9 rounded-xl" />
            <Skeleton className="mt-4 h-3 w-24" />
            <Skeleton className="mt-2 h-6 w-32" />
            <Skeleton className="mt-3 h-3 w-full" />
          </Card>
        ))}
      </div>
      <Card padding="lg">
        <Skeleton className="h-4 w-28" />
        <Skeleton className="mt-2 h-3 w-48" />
        <Skeleton className="mt-6 h-44 w-full" />
      </Card>
      <Card padding="lg">
        <Skeleton className="h-4 w-28" />
        <div className="mt-5 space-y-3">
          {[0, 1, 2, 3, 4].map((index) => <Skeleton key={index} className="h-10 w-full" />)}
        </div>
      </Card>
    </>
  );
}

export function Usage() {
  const [range, setRange] = useState<Range>('today');
  const [purpose, setPurpose] = useState<PurposeFilter>('all');
  const [view, setView] = useState<'models' | 'sessions'>('models');
  const [overview, setOverview] = useState<Overview | null>(null);
  const [models, setModels] = useState<ModelMetrics[] | null>(null);
  const [sessions, setSessions] = useState<SessionMetrics[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const pollRef = useRef<ReturnType<typeof startVisiblePoll> | null>(null);

  useEffect(() => {
    setOverview(null);
    setModels(null);
    setSessions(null);
    setLoading(true);
    const poll = startVisiblePoll(async (signal) => {
      const query = statsWindowQuery(range, purpose);
      const [nextOverview, nextModels, nextSessions] = await Promise.all([
        statsApi.overview(query, signal).catch(() => null),
        statsApi.models({ ...query, limit: 100 }, signal).catch(() => null),
        statsApi.sessions({ ...query, limit: 100 }, signal).catch(() => null),
      ]);
      if (signal.aborted) return;
      if (nextOverview) setOverview(nextOverview);
      if (nextModels) setModels(Array.isArray(nextModels.models) ? nextModels.models : []);
      if (nextSessions) setSessions(Array.isArray(nextSessions.sessions) ? nextSessions.sessions : []);
      setLoading(false);
      setRefreshing(false);
    }, { interval: 30000 });
    pollRef.current = poll;
    return () => {
      poll.stop();
      pollRef.current = null;
    };
  }, [range, purpose]);

  const refresh = () => {
    setRefreshing(true);
    pollRef.current?.request();
  };

  const usage = overview?.usage ?? null;
  const totalTokens = finite(usage?.total_tokens);
  const inputTokens = finite(usage?.input_tokens);
  const outputTokens = finite(usage?.output_tokens);
  const cachedTokens = finite(usage?.cached_tokens);
  const calls = finite(usage?.calls);
  const succeeded = finite(usage?.succeeded);
  const failed = finite(usage?.failed);
  const cancelled = finite(usage?.cancelled);
  const p50 = finite(usage?.duration_p50_ms);
  const p95 = finite(usage?.duration_p95_ms);
  const cachedShare = cachedTokens !== null && inputTokens !== null && inputTokens > 0 ? pct(cachedTokens, inputTokens) : null;

  const comparison = range === 'today' ? overview?.comparison?.tokens : null;
  const percentDelta = finite(comparison?.percent_delta);
  const previous = finite(comparison?.previous);
  const buckets = Array.isArray(overview?.series) ? overview.series : [];

  const modelRows = models ?? [];
  const sessionRows = sessions ?? [];
  const modelTotal = useMemo(
    () => Math.max(1, ...(models ?? []).map((model) => finite(model.usage?.total_tokens) ?? 0)),
    [models],
  );
  const sessionTotal = useMemo(
    () => Math.max(1, ...(sessions ?? []).map((session) => finite(session.usage?.total_tokens) ?? 0)),
    [sessions],
  );

  return (
    <div className="space-y-6 tint-green">
      <PageHeader
        eyebrow={<span className="flex items-center gap-1.5 text-xs font-medium text-tint"><BarChart3 className="h-3.5 w-3.5" />数据 · 模型调用与 Token</span>}
        title="用量统计"
        description="按时间、模型与会话实例查看模型调用次数、Token 消耗与耗时"
        actions={<>
          <Tabs label="时间范围" size="sm" value={range} onChange={setRange} items={[
            { value: 'today', label: '今日' }, { value: '7d', label: '近 7 日' }, { value: '30d', label: '近 30 日' },
          ]} />
          <Button size="sm" variant="ghost" icon={<RotateCw className="h-3.5 w-3.5" />} loading={refreshing} onClick={refresh}>刷新</Button>
        </>}
      />

      <CoverageNotice meta={overview} />

      {loading ? <UsageSkeleton /> : (
        <>
          <div className="stagger grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Card tint="accent" padding="md" style={{ '--i': 0 } as CSSProperties}>
              <div className="flex items-center justify-between">
                <span className="grid h-9 w-9 place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint"><Coins className="h-[18px] w-[18px]" /></span>
                <Coverage usage={usage} />
              </div>
              <div className="mt-4 text-[13px] text-fg-3">{RANGE_LABEL[range]}总 Token</div>
              <div className="mt-0.5 flex items-baseline gap-2">
                <span className="display text-[28px] font-semibold leading-none text-fg num">{fmt(totalTokens)}</span>
                {range === 'today' && percentDelta !== null && (
                  <span className={cn('text-xs font-medium', percentDelta >= 0 ? 'text-green' : 'text-red')}>{formatPercent(percentDelta)}</span>
                )}
              </div>
              <div className="mt-1 text-[11px] text-fg-4">{range === 'today' ? `较昨日同期 ${fmt(previous)}` : '跨天范围不提供同比'}</div>
            </Card>
            <Card tint="purple" padding="md" style={{ '--i': 1 } as CSSProperties}>
              <span className="grid h-9 w-9 place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint"><Database className="h-[18px] w-[18px]" /></span>
              <div className="mt-4 text-[13px] text-fg-3">输入 / 输出</div>
              <div className="mt-0.5 display text-[22px] font-semibold leading-none text-fg num">{fmt(inputTokens)} <span className="text-fg-4">/</span> {fmt(outputTokens)}</div>
              <div className="mt-2"><ShareBar input={inputTokens} output={outputTokens} total={totalTokens} /></div>
              <div className="mt-1.5 text-[11px] text-fg-4 num">其中缓存命中 {fmt(cachedTokens)} · {cachedShare === null ? '—' : `${cachedShare}%`}</div>
            </Card>
            <Card tint="teal" padding="md" style={{ '--i': 2 } as CSSProperties}>
              <span className="grid h-9 w-9 place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint"><Zap className="h-[18px] w-[18px]" /></span>
              <div className="mt-4 text-[13px] text-fg-3">模型调用</div>
              <div className="mt-0.5 display text-[28px] font-semibold leading-none text-fg">{calls === null ? '—' : <CountUp value={calls} />}</div>
              <div className="mt-1.5 flex flex-wrap gap-x-3 text-[11px] num">
                <span className="text-green">成功 {fmt(succeeded)}</span>
                <span className="text-red">失败 {fmt(failed)}</span>
                <span className="text-fg-4">取消 {fmt(cancelled)}</span>
              </div>
            </Card>
            <Card tint="orange" padding="md" style={{ '--i': 3 } as CSSProperties}>
              <span className="grid h-9 w-9 place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint"><Timer className="h-[18px] w-[18px]" /></span>
              <div className="mt-4 text-[13px] text-fg-3">调用耗时</div>
              <div className="mt-0.5 display text-[22px] font-semibold leading-none text-fg num">P50 {ms(p50)} <span className="text-fg-4">·</span> P95 {ms(p95)}</div>
              <div className="mt-1.5 text-[11px] text-fg-4">
                {usage === null ? '—' : usage.duration_method === 'histogram' ? '按固定直方图估算分位数' : '精确分位数'}
              </div>
            </Card>
          </div>

          <Card padding="lg" tint="accent" className="animate-rise" style={{ animationDelay: '120ms' }}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 items-start gap-3">
                <div className="mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint"><BarChart3 className="h-4 w-4" /></div>
                <div className="min-w-0">
                  <h3 className="text-[15px] font-semibold leading-6 text-fg">Token 趋势</h3>
                  <p className="mt-0.5 text-[13px] text-fg-3">{range === 'today' ? '按小时, 包含当前小时已过去部分' : '按天, 包含今日已过去部分'}</p>
                </div>
              </div>
              <div className="scroll max-w-full overflow-x-auto">
                <Tabs label="调用用途" size="sm" value={purpose} onChange={setPurpose} items={[
                  { value: 'all', label: '全部' }, { value: 'conversation', label: '对话' }, { value: 'summary', label: '摘要' },
                  { value: 'approval', label: '审批' }, { value: 'other', label: '其它' },
                ]} />
              </div>
            </div>
            <div className="mt-5">
              {buckets.length > 0
                ? <TokenChart key={`${range}:${purpose}`} buckets={buckets} range={range} />
                : <EmptyState tint="accent" icon={<BarChart3 className="h-6 w-6" />} title="暂无用量数据" description="当前时间范围与用途下没有记录到调用" />}
            </div>
          </Card>

          <Card padding="none" className="animate-rise overflow-hidden" style={{ animationDelay: '160ms' }}>
            <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-4">
              <div className="min-w-[200px] flex-1">
                <h2 className="text-base font-semibold text-fg">用量拆分</h2>
                <p className="mt-0.5 text-[13px] text-fg-3">{view === 'models' ? '按调用时使用的模型配置与实际模型' : '按平台会话实例, 含最近一次请求的上下文占用'}</p>
              </div>
              <Tabs label="拆分方式" size="sm" value={view} onChange={setView} items={[
                { value: 'models', label: '按模型', icon: <Cpu className="h-3.5 w-3.5" />, count: models?.length },
                { value: 'sessions', label: '按会话实例', icon: <Boxes className="h-3.5 w-3.5" />, count: sessions?.length },
              ]} />
            </div>

            {view === 'models' ? (
              modelRows.length === 0 ? (
                <EmptyState tint="purple" icon={<Cpu className="h-6 w-6" />} title="暂无模型用量" description="当前范围内没有可展示的模型调用记录" />
              ) : (
                <div className="stagger divide-y divide-[var(--glass-border)]" key="models">
                  <div className="hidden grid-cols-[minmax(0,2.2fr)_minmax(0,1.6fr)_90px_90px_110px_90px] gap-4 px-5 py-2 text-[11px] text-fg-4 md:grid">
                    <span>模型</span><span>Token (输入 / 输出)</span><span className="text-right">调用</span><span className="text-right">P95</span><span>近 14 日</span><span className="text-right">用量覆盖</span>
                  </div>
                  {modelRows.map((model, index) => {
                    const rowUsage = model.usage;
                    const rowTotal = finite(rowUsage?.total_tokens);
                    const rowInput = finite(rowUsage?.input_tokens);
                    const rowOutput = finite(rowUsage?.output_tokens);
                    const trend = tokenTrend(model.trend);
                    return (
                      <div key={`${model.model}:${model.model_config ?? ''}`} className="grid grid-cols-1 gap-2 px-5 py-3.5 transition-colors hover:bg-glass-hover md:grid-cols-[minmax(0,2.2fr)_minmax(0,1.6fr)_90px_90px_110px_90px] md:items-center md:gap-4" style={{ '--i': index } as CSSProperties}>
                        <div className="flex min-w-0 items-center gap-3">
                          <span className="tint-purple grid h-9 w-9 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.14)] text-sm font-semibold text-tint">
                            {(model.model_config ?? model.model ?? '?').charAt(0).toUpperCase() || '?'}
                          </span>
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="truncate text-sm font-medium text-fg">{model.model_config ?? '未记录配置'}</span>
                              {model.provider && <Badge>{model.provider}</Badge>}
                            </div>
                            <div className="truncate font-mono text-[11.5px] text-fg-3">{model.model}</div>
                          </div>
                        </div>
                        <div className="min-w-0 space-y-1.5">
                          <div className="num text-sm text-fg">{fmt(rowTotal)} <span className="text-[11px] text-fg-4">{fmt(rowInput)} / {fmt(rowOutput)}</span></div>
                          <span className="block" style={{ width: `${Math.max(6, pct(rowTotal ?? 0, modelTotal))}%` }}>
                            <ShareBar input={rowInput} output={rowOutput} total={rowTotal} />
                          </span>
                        </div>
                        {/* 窄屏把调用/耗时/覆盖合并为一行, 趋势线只在宽屏显示 */}
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 md:contents">
                          <span className="num text-sm text-fg-2 md:text-right"><span className="mr-1 text-[11px] text-fg-4 md:hidden">调用</span>{fmt(finite(rowUsage?.calls))}</span>
                          <span className="num text-sm text-fg-2 md:text-right"><span className="mr-1 text-[11px] text-fg-4 md:hidden">P95</span>{ms(finite(rowUsage?.duration_p95_ms))}</span>
                          <span className="tint-purple hidden md:block">{trend.length > 1 && <Sparkline data={trend} className="!h-7" />}</span>
                          <span className="ml-auto md:ml-0 md:text-right"><Coverage usage={rowUsage} /></span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )
            ) : sessionRows.length === 0 ? (
              <EmptyState tint="green" icon={<Boxes className="h-6 w-6" />} title="暂无会话用量" description="当前范围内没有可展示的会话实例记录" />
            ) : (
              <div className="stagger divide-y divide-[var(--glass-border)]" key="sessions">
                <div className="hidden grid-cols-[minmax(0,2fr)_minmax(0,1.5fr)_minmax(0,1.4fr)_90px_110px] gap-4 px-5 py-2 text-[11px] text-fg-4 md:grid">
                  <span>会话实例</span><span>Token (输入 / 输出)</span><span>上下文占用</span><span className="text-right">调用</span><span>近 14 日</span>
                </div>
                {sessionRows.map((session, index) => {
                  const rowUsage = session.usage;
                  const rowTotal = finite(rowUsage?.total_tokens);
                  const rowInput = finite(rowUsage?.input_tokens);
                  const rowOutput = finite(rowUsage?.output_tokens);
                  const trend = tokenTrend(session.trend);
                  const used = finite(session.context?.used);
                  const window = finite(session.context?.window);
                  const ratio = used !== null && window !== null && window > 0 ? used / window : null;
                  return (
                    <div key={`${session.platform_id}:${session.session_id}`} className="grid grid-cols-1 gap-2 px-5 py-3.5 transition-colors hover:bg-glass-hover md:grid-cols-[minmax(0,2fr)_minmax(0,1.5fr)_minmax(0,1.4fr)_90px_110px] md:items-center md:gap-4" style={{ '--i': index } as CSSProperties}>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-sm font-medium text-fg">{session.agent ?? 'Chat 对话'}</span>
                          <Badge tone={session.platform_id === 'chat' ? 'accent' : 'pink'}>{session.platform_id}</Badge>
                        </div>
                        <div className="truncate font-mono text-[11.5px] text-fg-3">{session.session_id}</div>
                      </div>
                      <div className="min-w-0 space-y-1.5">
                        <div className="num text-sm text-fg">{fmt(rowTotal)} <span className="text-[11px] text-fg-4">{fmt(rowInput)} / {fmt(rowOutput)}</span></div>
                        <span className="block" style={{ width: `${Math.max(6, pct(rowTotal ?? 0, sessionTotal))}%` }}>
                          <ShareBar input={rowInput} output={rowOutput} total={rowTotal} />
                        </span>
                      </div>
                      <div className="min-w-0">
                        {ratio === null ? (
                          <span className="text-[12px] text-fg-4">未知 · 窗口 {fmt(window)}</span>
                        ) : (
                          <>
                            <div className="num flex items-center justify-between text-[12px] text-fg-2">
                              <span>{fmt(used)} / {fmt(window)}</span>
                              <span className={cn(ratio > 0.85 ? 'text-red' : ratio > 0.6 ? 'text-orange' : 'text-fg-3')}>
                                {Math.round(ratio * 100)}%{session.context?.source === 'estimated' && ' · 估算'}
                              </span>
                            </div>
                            <span className="mt-1 block h-1.5 overflow-hidden rounded-full bg-glass-hover">
                              <span
                                className={cn('block h-full rounded-full', ratio > 0.85 ? 'bg-[rgb(var(--rgb-red))]' : ratio > 0.6 ? 'bg-[rgb(var(--rgb-orange))]' : 'bg-[rgb(var(--rgb-green))]')}
                                style={{ width: `${Math.min(100, ratio * 100)}%` }}
                              />
                            </span>
                          </>
                        )}
                      </div>
                      <span className="num text-sm text-fg-2 md:text-right"><span className="mr-1 text-[11px] text-fg-4 md:hidden">调用</span>{fmt(finite(rowUsage?.calls))}</span>
                      <span className="tint-green hidden md:block">{trend.length > 1 && <Sparkline data={trend} className="!h-7" />}</span>
                    </div>
                  );
                })}
              </div>
            )}

            <div className="flex items-center gap-2 border-t border-line px-5 py-2.5 text-[11px] text-fg-4">
              <Gauge className="h-3.5 w-3.5" />
              未返回用量的调用仍计入调用次数, 其 Token 视为未知, 不按 0 计入; 上下文占用取最近一次实际请求的输入用量
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
