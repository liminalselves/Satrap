import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { useCachedState } from '@/hooks/useCachedState';
import { useNavigate } from 'react-router-dom';
import { useShallow } from 'zustand/react/shallow';
import {
  Activity, AlertCircle, ArrowUpRight, Bot, Check, Copy, Cpu, Globe, MessageSquare, Server, Terminal, Zap,
} from 'lucide-react';
import {
  Badge, Button, Card, CardHeader, CountUp, PageHeader, Sparkline, StatusDot, type Tone, type Tint,
} from '@/components/kit';
import { BackendControls } from '@/components/common/BackendControls';
import { AdapterStatus } from '@/components/common/AdapterStatus';
import { useBackendStore } from '@/stores/useBackendStore';
import { useApiSessionStore } from '@/stores/useApiSessionStore';
import { useServiceHealthStore } from '@/stores/useServiceHealthStore';
import {
  activityApi, approvalsApi, sourceStateLabels, statsApi,
  type ActivityEvent, type ActivityRef, type AggregateMeta, type ModelMetrics, type Overview,
  type Service, type SourceCoverage,
} from '@/api/contracts';
import { controlClient } from '@/api/control';
import type { AdapterInfo, PlatformConfigApplication } from '@/api/types';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { copyTextToClipboard } from '@/utils/clipboard';
import { cn } from '@/utils/cn';

interface AdapterUsage { messages: number | null; series: number[] }

const SERVICE_LABELS: Record<Service, string> = { control: '控制服务', backend: '后端服务', chat: 'Chat 服务' };

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function addNullable(total: number | null, value: number | null): number | null {
  if (value === null) return total;
  return (total ?? 0) + value;
}

/** 汇总每个平台的今日消息与桶序列, 同名平台的不同账号合并 */
function buildAdapterUsage(source: Overview | null): Map<string, AdapterUsage> {
  const merged = new Map<string, { inbound: number | null; outbound: number | null; buckets: Map<number, number> }>();
  for (const adapter of source?.by_adapter ?? []) {
    if (!adapter || typeof adapter !== 'object' || !adapter.platform_id) continue;
    const entry = merged.get(adapter.platform_id) ?? { inbound: null, outbound: null, buckets: new Map<number, number>() };
    entry.inbound = addNullable(entry.inbound, finite(adapter.messages?.inbound));
    entry.outbound = addNullable(entry.outbound, finite(adapter.messages?.outbound));
    for (const bucket of adapter.series ?? []) {
      const from = finite(bucket?.from);
      if (from === null) continue;
      const count = (finite(bucket.messages?.inbound) ?? 0) + (finite(bucket.messages?.outbound) ?? 0);
      entry.buckets.set(from, (entry.buckets.get(from) ?? 0) + count);
    }
    merged.set(adapter.platform_id, entry);
  }
  return new Map([...merged.entries()].map(([key, entry]) => [key, {
    messages: entry.inbound === null && entry.outbound === null ? null : (entry.inbound ?? 0) + (entry.outbound ?? 0),
    series: [...entry.buckets.entries()].sort((a, b) => a[0] - b[0]).map(([, value]) => value),
  }]));
}

/** 模型调用趋势: 按日汇总所有模型的实际调用次数 */
function aggregateModelTrend(models: ModelMetrics[]): number[] {
  const buckets = new Map<number, number>();
  for (const model of models) {
    for (const bucket of model?.trend ?? []) {
      const from = finite(bucket?.from);
      if (from === null) continue;
      buckets.set(from, (buckets.get(from) ?? 0) + (finite(bucket.usage?.calls) ?? 0));
    }
  }
  return [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([, value]) => value);
}

function countModelConfigs(modelCounts: unknown): number | null {
  if (!modelCounts || typeof modelCounts !== 'object') return null;
  const values = Object.values(modelCounts as Record<string, unknown>);
  if (!values.length || values.some((value) => typeof value !== 'number')) return null;
  return (values as number[]).reduce((total, value) => total + value, 0);
}

function formatPercent(value: number): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`;
}

function formatClock(timestamp: number): string {
  if (!Number.isFinite(timestamp)) return '—';
  return new Date(timestamp * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function toneForActivity(tone: ActivityEvent['tone']): Tint | 'red' {
  return tone === 'error' ? 'red' : tone === 'warning' ? 'orange' : 'accent';
}

function sourceTone(state: SourceCoverage['state']): Tone {
  if (state === 'available') return 'green';
  if (state === 'stale' || state === 'auth_required') return 'orange';
  return 'red';
}

function activityPath(ref: ActivityRef | null): string | null {
  if (!ref) return null;
  switch (ref.kind) {
    case 'approval': {
      const query = new URLSearchParams({ platform: ref.key.platform_id, action: ref.key.action_id, state: 'all' });
      return `/platforms/approvals?${query.toString()}`;
    }
    case 'rag_job': return '/rag';
    case 'plugin_application': return '/plugins';
    case 'platform': return '/platforms';
    case 'session': return `/conversations?${new URLSearchParams({ platform: ref.platform_id, conversation: ref.session_id }).toString()}`;
    default: return null;
  }
}

function useUptime(startedAt: number | null): string | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
  if (startedAt === null) return null;
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  const hh = String(Math.floor(seconds / 3600)).padStart(2, '0');
  const mm = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
  const ss = String(seconds % 60).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}

function CoverageChips({ meta, label }: { meta: AggregateMeta | null; label: string }) {
  if (!meta || meta.complete !== false) return null;
  const sources = Array.isArray(meta.sources)
    ? meta.sources.filter((source) => source && (source.state !== 'available' || source.complete === false))
    : [];
  const warnings = Array.isArray(meta.warnings) ? meta.warnings : [];
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px] text-fg-3">
      <Badge tone="orange" dot>{label}数据不完整</Badge>
      {sources.map((source) => (
        <Badge key={source.service} tone={sourceTone(source.state)}>
          {SERVICE_LABELS[source.service] ?? source.service} · {sourceStateLabels[source.state] ?? source.state}
        </Badge>
      ))}
      {warnings.length > 0 && (
        <span title={warnings.join('\n')} className="cursor-help underline decoration-dotted underline-offset-2">查看提示</span>
      )}
    </div>
  );
}

function BackendHero({ adapterCount }: { adapterCount: number }) {
  const { healthAvailable, isRunning, runState, operation, controlStatus, statusConnected, health } = useBackendStore(useShallow((state) => ({
    healthAvailable: state.healthAvailable, isRunning: state.isRunning, runState: state.runState, operation: state.operation,
    controlStatus: state.controlStatus, statusConnected: state.statusConnected, health: state.health })));
  const sample = useServiceHealthStore((state) => state.services.backend);
  const info = sample.health;
  const startedSeconds = finite(info?.started_at);
  const uptimeSeconds = finite(info?.uptime_seconds);
  const pid = finite(info?.pid);
  const startedAt = startedSeconds !== null ? startedSeconds * 1000
    : uptimeSeconds !== null && sample.checked_at !== null ? sample.checked_at - uptimeSeconds * 1000
      : null;
  const uptime = useUptime(isRunning ? startedAt : null);
  const busy = operation !== null;
  const unknown = !controlStatus && !isRunning;
  const tone: Tint | 'red' = isRunning ? 'green' : unknown || runState === 'unknown' ? 'orange' : busy ? 'accent' : 'red';
  const title = unknown ? '暂时无法确认后端状态'
    : busy ? { start: '正在启动…', stop: '正在停止…', restart: '正在重启…' }[operation]
      : isRunning ? '后端运行中' : '后端已停止';
  const hint = isRunning
    ? uptime
      ? <>已持续运行 <span className="num font-mono text-fg-2">{uptime}</span>{pid !== null ? ` · PID ${pid}` : ''}{info?.python ? ` · Python ${info.python}` : ''}</>
      : `${adapterCount} 个适配器在线 · 状态${statusConnected ? '实时推送' : '轮询更新'}`
    : unknown ? '控制服务没有响应, 请在顶栏状态中逐项重试'
      : busy ? '正在等待后端状态确认…'
        : runState === 'stopped' ? '启动后即可接收各平台消息' : '正在确认后端运行状态';
  const statusText = isRunning ? 'HEALTHY' : busy ? 'WORKING' : unknown || runState === 'unknown' ? 'UNKNOWN' : 'STOPPED';

  return (
    <Card tint={tone} padding="lg" flowing={isRunning || busy} className="relative overflow-hidden lg:col-span-2" style={{ '--i': 0 } as CSSProperties}>
      <div aria-hidden className="pointer-events-none absolute -right-16 -top-24 h-72 w-72 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.35),transparent_65%)] blur-2xl" />
      <div className="relative flex flex-col gap-5">
        <div className="flex min-w-0 items-center gap-5">
          <div className="relative grid h-16 w-16 flex-none place-items-center">
            <span aria-hidden className={cn('absolute inset-0 rounded-2xl bg-[rgb(var(--tint)/0.18)]', isRunning && 'animate-[ping_2.4s_var(--ease-out)_infinite] opacity-40')} />
            <div className="relative grid h-16 w-16 place-items-center rounded-2xl border border-[rgb(var(--tint)/0.3)] bg-[linear-gradient(135deg,rgb(var(--tint)/0.3),rgb(var(--tint)/0.08))] text-tint shadow-[inset_0_1px_0_var(--glass-highlight),0_12px_32px_-10px_rgb(var(--tint)/0.7)]">
              {unknown ? <AlertCircle className="h-7 w-7" /> : <Server className="h-7 w-7" />}
            </div>
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs font-medium text-tint">
              <StatusDot tone={tone} live={isRunning || busy} />
              {statusText}
            </div>
            <h2 className="display mt-1 whitespace-nowrap text-2xl font-semibold text-fg">{title}</h2>
            <p className="mt-1 text-sm text-fg-3">{hint}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:justify-end"><BackendControls /></div>
        {isRunning && !healthAvailable && health?.error && (
          <div role="alert" className="rounded-xl border border-[rgb(var(--rgb-orange)/0.28)] bg-[rgb(var(--rgb-orange)/0.08)] px-3.5 py-3 text-sm text-fg-2">
            <span className="font-medium text-orange">后端已启动, 暂时无法读取运行状态</span>
            <span className="mt-0.5 block text-xs text-fg-3">{health.error}</span>
          </div>
        )}
      </div>
    </Card>
  );
}

function QuickStart() {
  const [copied, setCopied] = useState('');
  const commands = [
    { label: '控制服务', command: 'python -m satrap.core.backend.control_server' },
    { label: '或直接运行后端', command: 'python -m satrap.main run' },
  ];
  const copy = (command: string) => {
    void copyTextToClipboard(command).then(() => {
      setCopied(command);
      window.setTimeout(() => setCopied(''), 1600);
    }).catch(() => undefined);
  };
  return (
    <Card tint="orange" padding="lg" className="lg:col-span-3" style={{ '--i': 2 } as CSSProperties}>
      <CardHeader icon={<Terminal className="h-4 w-4" />} title="快速开始"
        description="控制服务没有响应. 在项目根目录执行以下命令之一, 然后在顶栏状态中点击重试." />
      <div className="grid gap-3 md:grid-cols-2">
        {commands.map((item, index) => (
          <div key={item.command} className="group relative rounded-xl border border-line bg-[rgb(0_0_0/0.18)] p-3.5 [[data-theme=light]_&]:bg-white/50">
            <div className="mb-1.5 flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-fg-4">
              <span className="grid h-4 w-4 place-items-center rounded-full bg-[rgb(var(--tint)/0.2)] text-[10px] text-tint">{index + 1}</span>
              {item.label}
            </div>
            <code className="block truncate pr-10 font-mono text-[13px] text-fg"><span className="text-fg-4">$ </span>{item.command}</code>
            <button type="button" onClick={() => copy(item.command)} aria-label={`复制命令 ${item.command}`}
              className="absolute bottom-3 right-3 grid h-7 w-7 place-items-center rounded-lg border border-line bg-glass text-fg-3 transition hover:text-fg">
              {copied === item.command ? <Check className="h-3.5 w-3.5 text-green animate-pop" /> : <Copy className="h-3.5 w-3.5" />}
            </button>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ServicesCard() {
  const sessions = useApiSessionStore((state) => state.sessions);
  const samples = useServiceHealthStore((state) => state.services);
  const services: { id: Service; name: string; url: string }[] = [
    { id: 'backend', name: '后端服务', url: getApiBaseUrl() },
    { id: 'control', name: '控制服务', url: getControlApiUrl() },
    { id: 'chat', name: 'Chat 服务', url: getChatApiUrl() },
  ];
  return (
    <Card padding="lg" style={{ '--i': 1 } as CSSProperties}>
      <CardHeader title="服务健康" description="三项依赖服务" />
      <ul className="space-y-2.5">
        {services.map((service) => {
          const session = sessions[service.url.replace(/\/$/, '')];
          const sample = samples[service.id];
          const online = session?.state === 'ready';
          const locked = session?.state === 'password_required';
          const connecting = !session || session.state === 'checking';
          const tone: Tone = online ? 'green' : locked || connecting ? 'orange' : 'red';
          const label = online ? '已连接' : locked ? '需要密码' : connecting ? '连接中' : '连接失败';
          return (
            <li key={service.id} className="flex items-center gap-3">
              <StatusDot tone={tone} live={connecting} />
              <span className="min-w-0 flex-1 text-sm text-fg-2">{service.name}</span>
              {online && finite(sample.rtt_ms) !== null && <span className="num font-mono text-xs text-fg-3">{sample.rtt_ms}ms</span>}
              <span className={cn('text-xs font-medium text-tint', `tint-${tone}`)}>{label}</span>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function Stat({ tint, icon, label, value, delta, trend, to, i }: {
  tint: Tint; icon: ReactNode; label: string; value: number | null; delta?: number | null; trend: number[]; to: string; i: number;
}) {
  const navigate = useNavigate();
  return (
    <Card tint={tint} interactive padding="md" onClick={() => navigate(to)}
      aria-label={`${label}: ${value === null ? '未知' : value}, 查看详情`} className="group" style={{ '--i': i } as CSSProperties}>
      <div className="flex items-start justify-between">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">{icon}</div>
        <ArrowUpRight className="h-4 w-4 -translate-x-1 translate-y-1 text-fg-4 opacity-0 transition-all duration-300 group-hover:translate-x-0 group-hover:translate-y-0 group-hover:text-tint group-hover:opacity-100" />
      </div>
      <div className="mt-4 text-[13px] text-fg-3">{label}</div>
      <div className="mt-0.5 flex items-baseline gap-2">
        <span className="display text-[28px] font-semibold leading-none text-fg" title={value === null ? '未知' : undefined}>
          {value === null ? '—' : <CountUp value={value} />}
        </span>
        {delta !== undefined && delta !== null && (
          <span className={cn('text-xs font-medium', delta >= 0 ? 'text-green' : 'text-red')} title="与昨日同期比较">{formatPercent(delta)}</span>
        )}
      </div>
      {trend.length > 1 && <Sparkline data={trend} className="mt-3 opacity-80 transition-opacity group-hover:opacity-100" />}
    </Card>
  );
}

function AdaptersCard({ adapters, today, minute, platformConfig }: {
  adapters: [string, AdapterInfo][];
  today: Map<string, AdapterUsage>;
  minute: Map<string, AdapterUsage>;
  platformConfig?: PlatformConfigApplication[];
}) {
  const navigate = useNavigate();
  const maxMessages = Math.max(1, ...[...today.values()].map((usage) => usage.messages ?? 0));
  return (
    <Card padding="none" className="lg:col-span-2" style={{ '--i': 6 } as CSSProperties}>
      <div className="p-5 pb-3">
        <CardHeader className="mb-0" title="运行中的适配器" description={`今日消息量与连接状态 · ${adapters.length} 个平台`}
          actions={<Button size="sm" variant="ghost" onClick={() => navigate('/platforms')}>全部平台<ArrowUpRight className="h-3.5 w-3.5" /></Button>} />
      </div>
      <div className="space-y-0.5 px-2 pb-2">
        {adapters.map(([id, info]) => {
          const usage = today.get(id);
          const series = minute.get(id)?.series ?? [];
          const ok = info.status === 'running';
          const pct = usage?.messages === null || usage?.messages === undefined ? 0 : Math.min(100, (usage.messages / maxMessages) * 100);
          return (
            <div key={id} className="rounded-xl transition-colors hover:bg-glass-hover">
              <button type="button" onClick={() => navigate('/platforms')} className="group flex w-full items-center gap-4 rounded-xl px-3 py-3 text-left">
                <span className={cn('grid h-9 w-9 flex-none place-items-center rounded-xl text-tint', ok ? 'tint-pink' : 'tint-orange', 'bg-[rgb(var(--tint)/0.14)]')}>
                  <Globe className="h-4 w-4" />
                </span>
                <div className="min-w-0 flex-1 sm:w-40 sm:flex-none">
                  <div className="truncate text-sm font-medium text-fg">{id}</div>
                  <div className="truncate font-mono text-[11px] text-fg-4">{info.config_type || info.type || info.status}</div>
                </div>
                <div className="hidden flex-1 items-center gap-3 sm:flex">
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-glass-hover">
                    <div className={cn('h-full rounded-full bg-[linear-gradient(90deg,rgb(var(--tint)/0.5),rgb(var(--tint)))]', ok ? 'tint-pink' : 'tint-orange')}
                      style={{ width: `${pct}%`, animation: 'rise-in 900ms var(--ease-out) both' }} />
                  </div>
                  {usage?.messages !== null && usage?.messages !== undefined
                    ? <span className="num w-14 text-right text-xs text-fg-3">{usage.messages.toLocaleString()}</span>
                    : <span className="num w-14 text-right text-xs text-fg-3" title="未知">—</span>}
                </div>
                {series.length > 1 && <Sparkline data={series} className="hidden h-6 w-16 flex-none opacity-80 md:block" />}
                <Badge tone={ok ? 'green' : 'orange'} dot live={ok}>{ok ? '在线' : '降级'}</Badge>
              </button>
              <div className="px-3 pb-3 pl-[60px]">
                <AdapterStatus info={info} application={platformConfig?.find((item) => item.id === id)} />
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function ActivityCard({ items, pending, className }: { items: ActivityEvent[]; pending: number | null; className?: string }) {
  const navigate = useNavigate();
  return (
    <Card padding="lg" className={className} style={{ '--i': 7 } as CSSProperties}>
      <CardHeader title="最近动态" icon={<Activity className="h-4 w-4" />} className="tint-accent"
        actions={(
          <button type="button" onClick={() => navigate('/platforms/approvals')} aria-label={`待审批 ${pending === null ? '未知' : pending} 项`}
            className="rounded-full transition-opacity hover:opacity-80">
            <Badge tone={pending ? 'orange' : 'neutral'} dot live={(pending ?? 0) > 0}>{pending === null ? '待审批 —' : `${pending} 待审批`}</Badge>
          </button>
        )} />
      {items.length === 0
        ? <p className="py-8 text-center text-sm text-fg-3">暂无动态</p>
        : (
          <ol className="relative space-y-4 before:absolute before:bottom-2 before:left-[5px] before:top-2 before:w-px before:bg-[var(--glass-border-strong)]">
            {items.map((event) => {
              const tone = toneForActivity(event.tone);
              const path = activityPath(event.ref);
              const meta = [
                SERVICE_LABELS[event.service] ?? event.service,
                event.object ? `${event.object.platform_id}${event.object.self_id ? ` · ${event.object.self_id}` : ''}` : '',
                (finite(event.count) ?? 1) > 1 ? `合并 ${event.count} 次` : '',
              ].filter(Boolean).join(' · ');
              return (
                <li key={event.event_id} className={cn('relative flex gap-3', `tint-${tone}`)}>
                  <span aria-hidden className="relative z-[1] mt-1.5 h-[11px] w-[11px] flex-none rounded-full border-2 border-[var(--bg-base)] bg-tint shadow-[0_0_10px_rgb(var(--tint)/0.7)]" />
                  <div className="min-w-0 flex-1">
                    {path
                      ? <button type="button" onClick={() => navigate(path)} className="text-left text-[13px] leading-5 text-fg hover:text-tint hover:underline">{event.title}</button>
                      : <p className="text-[13px] leading-5 text-fg">{event.title}</p>}
                    <p className="mt-0.5 text-[11px] text-fg-4"><span className="num font-mono">{formatClock(event.occurred_at)}</span> · {meta}</p>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
    </Card>
  );
}

export function Dashboard() {
  const { health, isRunning, controlStatus, statusConnected } = useBackendStore(useShallow((state) => ({
    health: state.health, isRunning: state.isRunning, controlStatus: state.controlStatus, statusConnected: state.statusConnected })));
  const [overview, setOverview] = useCachedState<Overview | null>('dashboard:overview', null);
  const [minuteOverview, setMinuteOverview] = useCachedState<Overview | null>('dashboard:minute', null);
  const [models, setModels] = useCachedState<ModelMetrics[] | null>('dashboard:models', null);
  const [activities, setActivities] = useCachedState<ActivityEvent[] | null>('dashboard:activities', null);
  const [activityMeta, setActivityMeta] = useCachedState<AggregateMeta | null>('dashboard:activity-meta', null);
  const [pendingApprovals, setPendingApprovals] = useCachedState<number | null>('dashboard:pending', null);
  const [counts, setCounts] = useCachedState<{ model: number | null; session: number | null }>('dashboard:counts', { model: null, session: null });
  const [configError, setConfigError] = useState(false);

  useEffect(() => {
    const poll = startVisiblePoll(async (signal) => {
      const now = Math.floor(Date.now() / 1000);
      const [nextOverview, nextMinute, nextActivity, nextSummary, nextModels] = await Promise.all([
        statsApi.overview({}, signal).catch(() => null),
        statsApi.overview({ from: now - 14 * 60, to: now, bucket: 'minute' }, signal).catch(() => null),
        activityApi.list({ limit: 12 }, signal).catch(() => null),
        approvalsApi.summary({}, signal).catch(() => null),
        statsApi.models({ limit: 100 }, signal).catch(() => null),
      ]);
      let nextCounts: { model: number | null; session: number | null } | null = null;
      try {
        const { data } = await controlClient.get<{ model_counts?: Record<string, number>; session_count?: number }>('/config/overview', { signal });
        nextCounts = { model: countModelConfigs(data?.model_counts), session: finite(data?.session_count) };
      } catch {
        if (!signal.aborted) setConfigError(true);
      }
      if (signal.aborted) return;
      if (nextOverview) setOverview(nextOverview);
      if (nextMinute) setMinuteOverview(nextMinute);
      if (nextActivity) {
        setActivities(Array.isArray(nextActivity.items) ? nextActivity.items : []);
        setActivityMeta(nextActivity);
      }
      if (nextSummary) setPendingApprovals(finite(nextSummary.pending_count));
      if (nextModels) setModels(Array.isArray(nextModels.models) ? nextModels.models : []);
      if (nextCounts) { setCounts(nextCounts); setConfigError(false); }
    }, { interval: 30000 });
    return () => poll.stop();
  }, [setOverview, setMinuteOverview, setActivities, setActivityMeta, setPendingApprovals, setModels, setCounts]);

  const adapters = useMemo<[string, AdapterInfo][]>(
    () => (isRunning && health?.adapters ? Object.entries(health.adapters) : []),
    [isRunning, health?.adapters],
  );
  const todayAdapters = useMemo(() => buildAdapterUsage(overview), [overview]);
  const minuteAdapters = useMemo(() => buildAdapterUsage(minuteOverview), [minuteOverview]);
  const modelTrend = useMemo(() => aggregateModelTrend(models ?? []), [models]);

  const series = Array.isArray(overview?.series) ? overview.series : [];
  const messageTrend = series.map((bucket) => (finite(bucket?.messages?.inbound) ?? 0) + (finite(bucket?.messages?.outbound) ?? 0));
  const tokenTrend = series.map((bucket) => finite(bucket?.usage?.total_tokens) ?? 0);
  const inbound = finite(overview?.messages?.inbound);
  const outbound = finite(overview?.messages?.outbound);
  const totalMessages = inbound === null && outbound === null ? null : (inbound ?? 0) + (outbound ?? 0);
  const totalTokens = finite(overview?.usage?.total_tokens);
  const messageDelta = finite(overview?.comparison?.messages?.percent_delta);
  const tokenDelta = finite(overview?.comparison?.tokens?.percent_delta);
  const unknown = !controlStatus && !isRunning;

  return (
    <div className="space-y-6">
      <PageHeader
        title="仪表盘"
        description="系统运行状态与今日概览"
        actions={<Badge tone={statusConnected ? 'green' : 'neutral'} dot live={statusConnected}>{statusConnected ? '实时推送' : '轮询中'}</Badge>}
      />

      {configError && (
        <p role="alert" className="text-sm text-orange">配置概览暂不可用, 部分计数显示为未知</p>
      )}

      <div className="stagger grid gap-4 lg:grid-cols-3">
        <BackendHero adapterCount={adapters.length} />
        <ServicesCard />
        {unknown && <QuickStart />}
      </div>

      <div className="stagger grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat i={2} tint="purple" icon={<Cpu className="h-[18px] w-[18px]" />} label="模型配置" value={counts.model} trend={modelTrend} to="/models" />
        <Stat i={3} tint="teal" icon={<Bot className="h-[18px] w-[18px]" />} label="Agent 会话类" value={counts.session} trend={[]} to="/agents" />
        <Stat i={4} tint="pink" icon={<MessageSquare className="h-[18px] w-[18px]" />} label="今日消息" value={totalMessages} delta={messageDelta} trend={messageTrend} to="/conversations" />
        <Stat i={5} tint="accent" icon={<Zap className="h-[18px] w-[18px]" />} label="今日 Token (千)"
          value={totalTokens === null ? null : Math.round(totalTokens / 1000)} delta={tokenDelta} trend={tokenTrend} to="/usage" />
      </div>

      <CoverageChips meta={overview} label="今日统计" />

      {!unknown && (
        <div className="stagger grid gap-4 lg:grid-cols-3">
          {adapters.length > 0 && (
            <AdaptersCard adapters={adapters} today={todayAdapters} minute={minuteAdapters} platformConfig={health?.platform_config} />
          )}
          <ActivityCard items={activities ?? []} pending={pendingApprovals} className={adapters.length === 0 ? 'lg:col-span-3' : undefined} />
        </div>
      )}

      {!unknown && <CoverageChips meta={activityMeta} label="动态" />}

      {unknown && (
        <p className="flex items-center gap-2 text-xs text-fg-3">
          <Terminal className="h-3.5 w-3.5" />控制服务恢复后, 页面会自动刷新状态
        </p>
      )}
    </div>
  );
}
