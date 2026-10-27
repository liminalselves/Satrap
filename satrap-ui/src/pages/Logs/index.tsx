import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  Activity, ArrowDownToLine, Bug, Clock, Download, Filter, Layers, MessageSquare, Pause, Play, RefreshCw, ScrollText,
  Search, Server, Terminal, Trash2, Wifi, WifiOff, WrapText, X,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { LOG_LEVELS } from '@/utils/constants';
import { Badge, Button, Card, CardHeader, Drawer, IconButton, Input, PageHeader, StatusDot, toast } from '@/components/kit';
import { Select } from '@/components/ui/Select';
import { LogBuffer, mergeLogSources } from '@/utils/logBuffer';
import { logServices, readLogSource } from '@/api/logging';
import { isPasswordRequiredError } from '@/api/auth';
import { statsApi, type LogStats } from '@/api/contracts';
import { VirtualLogList, type LogEntry } from './VirtualLogList';
import { LevelHistogram } from './Histogram';
import { LogDetailDrawer } from './DetailDrawer';
import { buildStatsView, isLogStats } from './logStats';
import {
  connectionState, formatTime, LEVELS, SOURCES, sourceMeta, statusTone, STATUS_TEXT_CLASS,
  type LogSource, type SourceMeta,
} from './data';

const HISTORY_OPTIONS = [50, 100, 200, 300, 500].map((value) => ({ value: String(value), label: `${value} 行` }));
const STAT_WINDOW_SECONDS = 120;
const STAT_BUCKET_SECONDS = 10;

const SOURCE_ICON: Record<LogSource, typeof Server> = {
  control: Server,
  backend: Terminal,
  chat: MessageSquare,
};

const rateText = (rate: number | null) => rate === null ? '—' : rate.toLocaleString();

function localRate(logs: LogEntry[], now: number): number {
  return logs.reduce((sum, log) => (
    typeof log.timestamp === 'number' && now - log.timestamp >= 0 && now - log.timestamp <= 60 ? sum + 1 : sum
  ), 0);
}

function SourceTile({ source, status, count, rate, active, onSelect }: {
  source: SourceMeta; status?: string; count: number; rate: number | null; active: boolean; onSelect: () => void;
}) {
  const Icon = SOURCE_ICON[source.id];
  const state = connectionState(status);
  const live = state === 'connected' || state === 'waiting';
  return (
    <button
      type="button"
      aria-label={source.name}
      aria-pressed={active}
      onClick={onSelect}
      className={cn(
        'group relative flex min-w-0 flex-col gap-2 rounded-xl border p-2.5 text-left transition-all duration-300',
        active
          ? 'tint-orange border-[rgb(var(--tint)/0.45)] bg-[rgb(var(--tint)/0.1)] shadow-[0_0_0_3px_rgb(var(--tint)/0.08)]'
          : 'border-line bg-glass hover:border-[var(--glass-border-strong)] hover:bg-glass-hover',
      )}
    >
      <span className="flex items-center gap-2">
        <span className={cn('grid h-7 w-7 flex-none place-items-center rounded-lg', `tint-${source.tint}`, 'bg-[rgb(var(--tint)/0.15)] text-tint')}>
          <Icon className="h-3.5 w-3.5" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12.5px] font-medium text-fg">{source.name}</span>
          <span className={cn('mt-0.5 flex items-center gap-1.5 text-[10.5px]', STATUS_TEXT_CLASS[state])}>
            <StatusDot tone={statusTone(state)} live={live} className="!h-1.5 !w-1.5" />
            <span className="truncate">{source.name}: {status ?? '等待连接'}</span>
          </span>
        </span>
      </span>
      <span className="flex items-center justify-between gap-2 border-t border-line pt-1.5 text-[10px] text-fg-4">
        <span className="num truncate">{count.toLocaleString()} 条 · {rateText(rate)}/分</span>
        <span className="num hidden flex-none font-mono sm:inline">{source.short}</span>
      </span>
      {active && <span aria-hidden className="absolute right-2 top-2 h-1.5 w-1.5 rounded-full bg-[rgb(var(--rgb-orange))] shadow-[0_0_8px_rgb(var(--rgb-orange)/0.8)]" />}
    </button>
  );
}

function AggregateTile({ connected, count, rate, active, onSelect }: {
  connected: number; count: number; rate: number | null; active: boolean; onSelect: () => void;
}) {
  const tone = connected === SOURCES.length ? 'green' : connected > 0 ? 'orange' : 'red';
  return (
    <button
      type="button"
      aria-label="全部来源"
      aria-pressed={active}
      onClick={onSelect}
      className={cn(
        'group relative flex min-w-0 flex-col gap-2 rounded-xl border p-2.5 text-left transition-all duration-300',
        active
          ? 'tint-orange border-[rgb(var(--tint)/0.45)] bg-[rgb(var(--tint)/0.1)] shadow-[0_0_0_3px_rgb(var(--tint)/0.08)]'
          : 'border-line bg-glass hover:border-[var(--glass-border-strong)] hover:bg-glass-hover',
      )}
    >
      <span className="flex items-center gap-2">
        <span className="grid h-7 w-7 flex-none place-items-center rounded-lg tint-orange bg-[rgb(var(--tint)/0.15)] text-tint">
          <Layers className="h-3.5 w-3.5" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12.5px] font-medium text-fg">全部来源</span>
          <span className={cn('mt-0.5 flex items-center gap-1.5 text-[10.5px]', connected === SOURCES.length ? 'text-green' : connected > 0 ? 'text-orange' : 'text-red')}>
            <StatusDot tone={tone} live={connected > 0} className="!h-1.5 !w-1.5" />
            <span className="truncate">{connected}/{SOURCES.length} 个服务已连接</span>
          </span>
        </span>
      </span>
      <span className="flex items-center justify-between gap-2 border-t border-line pt-1.5 text-[10px] text-fg-4">
        <span className="num truncate">{count.toLocaleString()} 条 · {rateText(rate)}/分</span>
        <span className="hidden flex-none sm:inline">合并排序</span>
      </span>
      {active && <span aria-hidden className="absolute right-2 top-2 h-1.5 w-1.5 rounded-full bg-[rgb(var(--rgb-orange))] shadow-[0_0_8px_rgb(var(--rgb-orange)/0.8)]" />}
    </button>
  );
}

function TerminalEmpty({ connected, hasFilters, sourceLabel, onClearFilters }: {
  connected: boolean; hasFilters: boolean; sourceLabel: string; onClearFilters: () => void;
}) {
  const title = hasFilters ? '没有匹配的日志' : connected ? '等待日志...' : '未连接到日志服务';
  const description = hasFilters
    ? '当前级别或搜索条件没有命中, 试试放宽筛选'
    : connected
      ? `${sourceLabel} 的新日志会实时追加到这里`
      : '点击「重新连接」, 或检查对应服务的访问状态';
  return (
    <div className="flex max-w-sm flex-col items-center text-center">
      <div className="relative mb-5">
        <span aria-hidden className="absolute inset-0 -m-4 rounded-full bg-[radial-gradient(circle,rgb(255_140_0/0.26),transparent_70%)] blur-md" />
        <div className="relative grid h-12 w-12 place-items-center rounded-2xl border border-[rgb(255_140_0/0.28)] bg-[rgb(255_140_0/0.12)] text-[#ffb454]">
          {hasFilters ? <Filter className="h-5 w-5" /> : connected ? <Terminal className="h-5 w-5" /> : <WifiOff className="h-5 w-5" />}
        </div>
      </div>
      <p className="font-mono text-[13px] text-[#c9d1d9]">{title}</p>
      <p className="mt-1.5 text-[11.5px] leading-5 text-[#6e7681]">{description}</p>
      {hasFilters && (
        <button
          type="button"
          onClick={onClearFilters}
          className="mt-4 rounded-full border border-[rgb(255_140_0/0.3)] bg-[rgb(255_140_0/0.12)] px-3 py-1 text-[11.5px] font-medium text-[#ffb454] transition hover:bg-[rgb(255_140_0/0.2)]"
        >
          清除筛选
        </button>
      )}
    </div>
  );
}

function TermButton({ label, active, onClick, children }: {
  label: string; active?: boolean; onClick: () => void; children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      onClick={onClick}
      title={label}
      className={cn(
        'grid h-7 w-7 place-items-center rounded-md transition-colors',
        active
          ? 'bg-[rgb(255_140_0/0.16)] text-[#ffb454]'
          : 'text-[#8b949e] hover:bg-[rgb(255_255_255/0.06)] hover:text-[#e6edf3]',
      )}
    >
      {children}
    </button>
  );
}

export function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [bufferedCount, setBufferedCount] = useState(0);
  const [paused, setPaused] = useState(false);
  const [pausedAt, setPausedAt] = useState(0);
  const [searchQuery, setSearchQuery] = useState('');
  const [levelFilter, setLevelFilter] = useState<string[]>(() => [...LOG_LEVELS]);
  const [historyLines, setHistoryLines] = useState(100);
  const [autoScroll, setAutoScroll] = useState(true);
  const [source, setSource] = useState<LogSource | 'all'>('all');
  const [statuses, setStatuses] = useState<Partial<Record<LogSource, string>>>({});
  const [reconnect, setReconnect] = useState(0);
  const [wrap, setWrap] = useState(false);
  const [showTime, setShowTime] = useState(true);
  const [detail, setDetail] = useState<LogEntry | null>(null);
  const [stats, setStats] = useState<Partial<Record<LogSource, LogStats>>>({});
  const [clock, setClock] = useState(() => Date.now() / 1000);
  const cursors = useRef<Partial<Record<LogSource, { after: number; stream_id: string }>>>({});
  const seen = useRef(new Set<string>());
  const logIdRef = useRef(0);
  const buffer = useRef(new LogBuffer<LogEntry>());
  const flushTimer = useRef<ReturnType<typeof setTimeout>>();

  const flush = useCallback(() => {
    if (flushTimer.current !== undefined) clearTimeout(flushTimer.current);
    flushTimer.current = undefined;
    const snapshot = buffer.current.read();
    setLogs(mergeLogSources(snapshot.logs));
    setBufferedCount(snapshot.bufferedCount);
  }, []);

  const handleLog = useCallback((data: Omit<LogEntry, 'id'>) => {
    buffer.current.append({ id: logIdRef.current++, ...data });
    if (flushTimer.current === undefined) flushTimer.current = setTimeout(flush, 100);
  }, [flush]);

  // 只用于本地估算的秒级时钟, 避免每次批次 flush 都重算 5000 行
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now() / 1000), 5000);
    return () => clearInterval(timer);
  }, []);

  // 服务端日志统计: 2 分钟窗口 / 10 秒桶; 旧宿主无此接口时回退到当前缓冲
  useEffect(() => {
    let disposed = false;
    let running = false;
    const controllers = new Set<AbortController>();
    const load = async () => {
      if (running || document.hidden) return;
      running = true;
      const controller = new AbortController();
      controllers.add(controller);
      const entries = await Promise.all(logServices().map(async (service) => {
        try {
          const value = await statsApi.logs(service.id, { window_seconds: 120, bucket_seconds: 10 }, controller.signal);
          return isLogStats(value) ? [service.id, value] as const : null;
        } catch {
          return null;
        }
      }));
      controllers.delete(controller);
      running = false;
      if (disposed) return;
      const next: Partial<Record<LogSource, LogStats>> = {};
      for (const entry of entries) if (entry) next[entry[0]] = entry[1];
      setStats(next);
    };
    void load();
    const timer = setInterval(() => void load(), 5000);
    const resume = () => { if (!document.hidden) void load(); };
    document.addEventListener('visibilitychange', resume);
    return () => {
      disposed = true;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', resume);
      for (const controller of controllers) controller.abort();
    };
  }, [reconnect]);

  useEffect(() => {
    buffer.current.clear();
    seen.current.clear();
    cursors.current = {};
    flush();
  }, [historyLines, flush]);

  useEffect(() => {
    const visible = () => { if (!document.hidden) flush(); };
    document.addEventListener('visibilitychange', visible);
    return () => {
      document.removeEventListener('visibilitychange', visible);
      if (flushTimer.current !== undefined) clearTimeout(flushTimer.current);
      flushTimer.current = undefined;
    };
  }, [flush]);

  useEffect(() => {
    let disposed = false;
    const timers = new Map<LogSource, ReturnType<typeof setTimeout>>();
    const pending = new Map<LogSource, AbortController>();
    const services = logServices().filter((service) => source === 'all' || service.id === source);
    const poll = async (service: typeof services[number]) => {
      if (disposed || pending.has(service.id)) return;
      if (document.hidden) {
        timers.set(service.id, setTimeout(() => void poll(service), 5000));
        return;
      }
      const controller = new AbortController();
      pending.set(service.id, controller);
      const deadline = setTimeout(() => controller.abort(), 8000);
      let delay = 1000;
      try {
        const cursor = cursors.current[service.id];
        const result = await readLogSource(service.url, { limit: cursor ? 500 : historyLines, ...cursor }, controller.signal);
        // 历史行数只控制初始快照, 增量使用有界大批次避免积压时逐百条追赶
        if (disposed) return;
        if (result.reset) handleLog({
          content: `[${service.name}] 服务已重启或日志游标已过期, 已重新取得有限快照, 中间日志可能未保留`,
          level: 'WARNING', source: service.id, timestamp: result.entries[0]?.timestamp ?? Date.now() / 1000,
        });
        for (const entry of result.entries) {
          const key = `${service.id}:${result.stream_id}:${entry.sequence}`;
          if (seen.current.has(key)) continue;
          seen.current.add(key);
          if (seen.current.size > 5000) seen.current.delete(seen.current.values().next().value!);
          handleLog({
            content: `[${service.name}] ${entry.content}${entry.truncated ? ' [内容已截断]' : ''}`,
            level: entry.level, source: service.id, timestamp: entry.timestamp, sequence: entry.sequence,
            pid: entry.pid, stream_id: result.stream_id, truncated: entry.truncated,
          });
        }
        cursors.current[service.id] = { after: result.next_cursor, stream_id: result.stream_id };
        setStatuses((current) => ({ ...current, [service.id]: '已连接' }));
        if (result.has_more) delay = 250;
      } catch (error) {
        if (!disposed) setStatuses((current) => ({
          ...current,
          [service.id]: isPasswordRequiredError(error) ? '需要认证' : `离线: ${error instanceof Error ? error.message : '连接失败'}`,
        }));
        delay = 5000;
      } finally {
        clearTimeout(deadline);
        pending.delete(service.id);
        if (!disposed) timers.set(service.id, setTimeout(() => void poll(service), delay));
      }
    };
    const resume = () => {
      if (document.hidden) return;
      for (const service of services) {
        clearTimeout(timers.get(service.id));
        void poll(service);
      }
    };
    document.addEventListener('visibilitychange', resume);
    for (const service of services) void poll(service);
    return () => {
      disposed = true;
      document.removeEventListener('visibilitychange', resume);
      for (const timer of timers.values()) clearTimeout(timer);
      for (const controller of pending.values()) controller.abort();
    };
  }, [source, historyLines, reconnect, handleLog]);

  const togglePaused = useCallback(() => {
    const next = !paused;
    buffer.current.pause(next);
    setPaused(next);
    if (next) setPausedAt(Date.now() / 1000);
    else setPausedAt(0);
    flush();
  }, [paused, flush]);

  const clearLogs = useCallback(() => {
    buffer.current.clear();
    setDetail(null);
    flush();
    toast.success('日志已清空', '缓冲已重置, 新日志会继续追加');
  }, [flush]);

  const toggleLevel = useCallback((level: string) => {
    setLevelFilter((current) => (
      current.includes(level) ? current.filter((item) => item !== level) : [...current, level]
    ));
  }, []);

  const toggleAllLevels = useCallback(() => {
    setLevelFilter((current) => (current.length === LEVELS.length ? [] : [...LEVELS]));
  }, []);

  const clearFilters = useCallback(() => {
    setSearchQuery('');
    setLevelFilter([...LEVELS]);
  }, []);

  const selectSource = useCallback((next: LogSource | 'all') => {
    setSource(next);
    setDetail(null);
    setAutoScroll(true);
  }, []);

  const viewLogs = logs;
  const viewNow = paused ? pausedAt : clock;
  const sourceLogs = useMemo(
    () => (source === 'all' ? viewLogs : viewLogs.filter((line) => line.source === source)),
    [viewLogs, source],
  );
  const searchedLogs = useMemo(() => {
    const needle = searchQuery.trim().toLowerCase();
    return needle ? sourceLogs.filter((line) => line.content.toLowerCase().includes(needle)) : sourceLogs;
  }, [sourceLogs, searchQuery]);
  const levelCounts = useMemo(() => {
    const counts: Record<string, number> = Object.fromEntries(LEVELS.map((level) => [level, 0]));
    for (const line of searchedLogs) counts[line.level] = (counts[line.level] ?? 0) + 1;
    return counts;
  }, [searchedLogs]);
  const filteredLogs = useMemo(
    () => searchedLogs.filter((line) => levelFilter.includes(line.level)),
    [searchedLogs, levelFilter],
  );

  const selectedSources = useMemo<LogSource[]>(
    () => (source === 'all' ? SOURCES.map((item) => item.id) : [source]),
    [source],
  );
  const selectedStats = useMemo(
    () => selectedSources.map((id) => stats[id]).filter(isLogStats),
    [selectedSources, stats],
  );
  const statsView = useMemo(
    () => buildStatsView(selectedStats, selectedSources.length, sourceLogs, viewNow, STAT_WINDOW_SECONDS, STAT_BUCKET_SECONDS),
    [selectedStats, selectedSources.length, sourceLogs, viewNow],
  );
  const perSource = useMemo(() => {
    const result = {} as Record<LogSource, { count: number; rate: number | null }>;
    for (const meta of SOURCES) {
      const items = viewLogs.filter((line) => line.source === meta.id);
      const stat = stats[meta.id];
      result[meta.id] = { count: items.length, rate: stat ? stat.lines_per_minute : localRate(items, viewNow) };
    }
    return result;
  }, [viewLogs, stats, viewNow]);

  const connectedCount = SOURCES.filter((meta) => connectionState(statuses[meta.id]) === 'connected').length;
  const someConnected = SOURCES.some((meta) => (source === 'all' || source === meta.id) && connectionState(statuses[meta.id]) === 'connected');
  const sourceLabel = source === 'all' ? '全部来源' : sourceMeta(source).name;
  const hasFilters = searchQuery.trim() !== '' || levelFilter.length !== LEVELS.length;
  const allLevelsOn = levelFilter.length === LEVELS.length;
  const totalRate = statsView.linesPerMinute;
  const windowErrors = statsView.errorCount;
  const coalescedRate = source === 'all' ? statsView.linesPerMinute : perSource[source].rate;

  const neighbors = useMemo(() => {
    if (!detail) return [];
    const index = viewLogs.findIndex((line) => line.id === detail.id);
    if (index < 0) return [];
    return viewLogs
      .slice(Math.max(0, index - 4), Math.min(viewLogs.length, index + 5))
      .filter((line) => line.id !== detail.id);
  }, [detail, viewLogs]);

  const exportLogs = useCallback(() => {
    if (!filteredLogs.length) {
      toast.warning('没有可下载的日志', '请先调整级别或搜索条件');
      return;
    }
    const content = filteredLogs.map((line) => line.content).join('\n');
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    const filename = `satrap-logs-${new Date().toISOString().replace(/[:.]/g, '-')}.txt`;
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
    toast.success('已开始下载', `${filename} · ${filteredLogs.length} 行`);
  }, [filteredLogs]);

  const closeDetail = useCallback(() => setDetail(null), []);
  const empty = useMemo(() => (
    <TerminalEmpty
      connected={someConnected}
      hasFilters={hasFilters && sourceLogs.length > 0}
      sourceLabel={sourceLabel}
      onClearFilters={clearFilters}
    />
  ), [someConnected, hasFilters, sourceLogs.length, sourceLabel, clearFilters]);

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow={(
          <span className="inline-flex items-center gap-1.5 text-xs text-orange">
            <ScrollText className="h-3.5 w-3.5" />运维 · 实时日志流
          </span>
        )}
        title="日志监控"
        description="独立查看 Control、平台 Backend 和 Chat 的实时日志, 支持暂停、检索、堆叠统计与下载"
        actions={<>
          <Badge tone={someConnected ? 'green' : 'red'} dot live={someConnected}>
            {someConnected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
            {someConnected ? '已连接' : '未连接'}
          </Badge>
          <Badge tone={paused ? 'orange' : 'green'} dot>{paused ? '已暂停' : '实时更新中'}</Badge>
          <Button icon={<RefreshCw className="h-4 w-4" />} onClick={() => setReconnect((value) => value + 1)}>重新连接</Button>
          <Link to="/logs/debug" className="ctl" aria-label="打开调试日志 (高级 Debug)">
            <Bug className="h-4 w-4" />调试日志
          </Link>
        </>}
      />

      <div className="stagger grid gap-4 lg:grid-cols-[minmax(0,1.12fr)_minmax(0,1fr)]">
        <Card padding="none" tint="orange" style={{ '--i': 0 } as CSSProperties}>
          <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
            <div className="grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint">
              <Layers className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <h2 className="text-[14px] font-semibold text-fg">日志来源</h2>
              <p className="text-[11px] text-fg-4">
                {connectedCount}/{SOURCES.length} 已连接 · 增量轮询 /api/logs
              </p>
            </div>
            <span className="hidden flex-none text-[10.5px] text-fg-4 sm:inline">
              缓冲 <span className="num font-mono text-fg-3">{logs.length}</span> / 5000
            </span>
          </div>
          <div className="grid grid-cols-2 gap-2 p-3 lg:grid-cols-4">
            <AggregateTile
              connected={connectedCount}
              count={viewLogs.length}
              rate={coalescedRate}
              active={source === 'all'}
              onSelect={() => selectSource('all')}
            />
            {SOURCES.map((meta) => (
              <SourceTile
                key={meta.id}
                source={meta}
                status={statuses[meta.id]}
                count={perSource[meta.id].count}
                rate={perSource[meta.id].rate}
                active={source === meta.id}
                onSelect={() => selectSource(meta.id)}
              />
            ))}
          </div>
        </Card>

        <Card tint="orange" padding="lg" style={{ '--i': 1 } as CSSProperties}>
          <CardHeader
            icon={<Activity className="h-4 w-4" />}
            title="日志速率"
            description={statsView.mode === 'server'
              ? `最近 2 分钟 · ${sourceLabel} · 服务统计`
              : `最近 2 分钟 · ${sourceLabel} · 本地缓冲`}
            actions={(
              <div className="flex items-center gap-4">
                <div className="text-right">
                  <div className="display num text-[22px] font-semibold leading-none text-fg">{rateText(totalRate)}</div>
                  <div className="mt-1 text-[10px] text-fg-4">行 / 分</div>
                </div>
                <div className="text-right">
                  <div className={cn('display num text-[22px] font-semibold leading-none', windowErrors ? 'text-red' : 'text-fg')}>
                    {windowErrors === null ? '—' : windowErrors}
                  </div>
                  <div className="mt-1 text-[10px] text-fg-4">错误 / 2 分钟</div>
                </div>
              </div>
            )}
          />
          <div className="mb-2 flex flex-wrap items-center gap-2 text-[10.5px] text-fg-4">
            <Badge tone={statsView.mode === 'local' ? 'neutral' : statsView.complete ? 'green' : 'orange'} dot>
              {statsView.mode === 'local' ? '本地缓冲' : statsView.complete ? '统计完整' : '覆盖不完整'}
            </Badge>
            {statsView.windowFrom !== null && statsView.windowTo !== null && (
              <span className="num font-mono">
                {formatTime(statsView.windowFrom, false)} – {formatTime(statsView.windowTo, false)}
              </span>
            )}
            {statsView.coverageFrom !== null && statsView.windowFrom !== null && statsView.coverageFrom > statsView.windowFrom + 1 && (
              <span className="tint-orange rounded-full bg-[rgb(var(--tint)/0.12)] px-1.5 text-tint">
                覆盖自 {formatTime(statsView.coverageFrom, false)}
              </span>
            )}
          </div>
          <LevelHistogram
            buckets={statsView.buckets}
            activeLevels={levelFilter}
            onToggle={toggleLevel}
            bucketSeconds={statsView.bucketSeconds}
            mode={statsView.mode}
            notes={statsView.notes}
          />
        </Card>
      </div>

      <div className="stagger space-y-4">
        <Card padding="md" tint="orange" style={{ '--i': 2 } as CSSProperties}>
          <div className="flex flex-wrap items-center gap-2.5">
            <Button
              variant={paused ? 'tinted' : 'default'}
              tint={paused ? 'orange' : undefined}
              icon={paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
              onClick={togglePaused}
            >
              {paused ? '继续' : '暂停'}
            </Button>
            <Button icon={<Trash2 className="h-4 w-4" />} onClick={clearLogs}>清空</Button>
            <Button icon={<Download className="h-4 w-4" />} onClick={exportLogs}>导出</Button>
            <div className="relative min-w-[200px] flex-1">
              <Input
                leading={<Search className="h-4 w-4" />}
                trailing={searchQuery ? (
                  <IconButton label="清除搜索" size="sm" onClick={() => setSearchQuery('')}>
                    <X className="h-3.5 w-3.5" />
                  </IconButton>
                ) : undefined}
                placeholder="搜索日志..."
                aria-label="搜索日志内容"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
              />
            </div>
            <div className="flex flex-none items-center gap-2">
              <span className="text-[11px] text-fg-4">历史</span>
              <div className="w-28">
                <Select value={String(historyLines)} onChange={(event) => setHistoryLines(Number(event.target.value))} options={HISTORY_OPTIONS} />
              </div>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
            <span className="mr-0.5 text-[11px] font-medium text-fg-4">级别</span>
            {LEVELS.map((level) => {
              const on = levelFilter.includes(level);
              return (
                <button
                  key={level}
                  type="button"
                  aria-label={level}
                  aria-pressed={on}
                  onClick={() => toggleLevel(level)}
                  className={cn(
                    'inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[11.5px] font-medium transition-all duration-200',
                    on
                      ? 'tint-accent border-[rgb(var(--tint)/0.32)] bg-[rgb(var(--tint)/0.13)] text-tint'
                      : 'border-line bg-glass text-fg-3 hover:text-fg',
                  )}
                >
                  <span className={cn('h-1.5 w-1.5 rounded-full', on ? 'bg-tint' : 'bg-[var(--text-4)]')} />
                  {level}
                  <span className={cn(
                    'num rounded-full px-1.5 text-[10.5px] leading-[16px]',
                    on ? 'bg-[rgb(var(--tint)/0.18)] text-tint' : 'bg-glass-hover text-fg-4',
                  )}>
                    {levelCounts[level] ?? 0}
                  </span>
                </button>
              );
            })}
            <button
              type="button"
              onClick={toggleAllLevels}
              className="h-7 rounded-full px-2 text-[11.5px] text-fg-3 transition-colors hover:bg-glass-hover hover:text-fg"
            >
              {allLevelsOn ? '取消全选' : '全部'}
            </button>
            <span className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-fg-4">
              <span>共 <span className="num font-medium text-fg-2">{sourceLogs.length}</span> 条</span>
              <span>命中 <span className="num font-medium text-fg-2">{filteredLogs.length}</span> 条</span>
              {hasFilters && (
                <button type="button" onClick={clearFilters} className="text-fg-3 underline-offset-2 transition-colors hover:text-fg hover:underline">
                  清除筛选
                </button>
              )}
            </span>
          </div>
        </Card>

        <Card
          padding="none"
          tint="orange"
          className="relative flex min-h-0 flex-col overflow-hidden !rounded-2xl"
          style={{ height: 'clamp(380px, 56vh, 620px)', '--i': 3 } as CSSProperties}
        >
          <div className="flex flex-wrap items-center gap-2 border-b border-[rgb(255_255_255/0.08)] bg-[linear-gradient(180deg,#141821,#0e1219)] px-3 py-2">
            <div className="flex flex-none items-center gap-1.5" aria-hidden>
              <span className="h-2.5 w-2.5 rounded-full bg-[rgb(255_95_87/0.85)]" />
              <span className="h-2.5 w-2.5 rounded-full bg-[rgb(254_188_46/0.85)]" />
              <span className="h-2.5 w-2.5 rounded-full bg-[rgb(40_200_64/0.85)]" />
            </div>
            <span className="ml-1 flex min-w-0 items-center gap-1.5 font-mono text-[11px] text-[#8b949e]">
              <Terminal className="h-3.5 w-3.5 flex-none" />
              <span className="truncate">log stream / {sourceLabel}</span>
            </span>
            <span className="hidden items-center gap-1.5 text-[10.5px] text-[#6e7681] sm:flex">
              <span className={cn('h-1 w-1 rounded-full', someConnected ? 'bg-[#3fb950]' : 'bg-[#f85149]')} />
              {filteredLogs.length} lines
            </span>
            {paused && (
              <span className="rounded-full border border-[rgb(255_140_0/0.35)] bg-[rgb(255_140_0/0.15)] px-2 py-0.5 text-[10px] font-medium text-[#ffb454]">
                已暂停
              </span>
            )}
            <div className="ml-auto flex flex-none items-center gap-0.5">
              <TermButton label={paused ? '继续接收' : '暂停接收'} active={paused} onClick={togglePaused}>
                {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
              </TermButton>
              <span aria-hidden className="mx-0.5 h-4 w-px bg-[rgb(255_255_255/0.1)]" />
              <TermButton label="自动换行" active={wrap} onClick={() => setWrap((value) => !value)}>
                <WrapText className="h-3.5 w-3.5" />
              </TermButton>
              <TermButton label="显示时间戳" active={showTime} onClick={() => setShowTime((value) => !value)}>
                <Clock className="h-3.5 w-3.5" />
              </TermButton>
              <TermButton
                label={autoScroll ? '暂停自动滚动' : '恢复自动滚动'}
                active={autoScroll}
                onClick={() => setAutoScroll((value) => !value)}
              >
                <ArrowDownToLine className="h-3.5 w-3.5" />
              </TermButton>
            </div>
          </div>

          <VirtualLogList
            logs={filteredLogs}
            wrap={wrap}
            showTime={showTime}
            query={searchQuery}
            selectedId={detail?.id ?? null}
            onSelect={setDetail}
            autoScroll={autoScroll}
            onAutoScrollChange={setAutoScroll}
            empty={empty}
          />

          {paused && bufferedCount > 0 && (
            <button
              type="button"
              onClick={togglePaused}
              className="absolute bottom-12 right-4 z-10 animate-pop rounded-full border border-[rgb(255_140_0/0.32)] bg-[rgb(20_22_30/0.94)] px-3 py-1.5 text-[11px] font-medium text-[#ffb454] shadow-[0_8px_24px_-8px_rgb(0_0_0/0.8)] backdrop-blur transition hover:bg-[rgb(28_30_40/0.98)]"
            >
              暂停期间收到 {bufferedCount} 条 · 继续
            </button>
          )}
          {!autoScroll && !paused && (
            <button
              type="button"
              onClick={() => setAutoScroll(true)}
              className="absolute bottom-12 left-1/2 z-10 -translate-x-1/2 animate-pop rounded-full border border-[rgb(255_140_0/0.3)] bg-[rgb(20_22_30/0.92)] px-3 py-1.5 text-[11px] font-medium text-[#ffb454] shadow-[0_8px_24px_-8px_rgb(0_0_0/0.8)] backdrop-blur transition hover:bg-[rgb(28_30_40/0.98)]"
            >
              <ArrowDownToLine className="mr-1 inline h-3 w-3" />回到最新
            </button>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[rgb(255_255_255/0.08)] bg-[#0b0e15] px-4 py-2 text-[11px] text-[#8b949e]">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span>
                共 <span className="num font-mono text-[#e6edf3]">{filteredLogs.length}</span> 条日志
                {bufferedCount > 0 ? `, 暂停期间收到 ${bufferedCount} 条` : ''}
              </span>
              <span className="hidden sm:inline">
                · 缓冲 <span className="num font-mono text-[#c9d1d9]">{logs.length}</span> / 5000
              </span>
              {searchQuery.trim() && <span className="max-w-[220px] truncate">· 搜索 “{searchQuery.trim()}”</span>}
            </div>
            <div className="flex items-center gap-3">
              <span className="flex items-center gap-1.5">
                <span className={cn('h-1.5 w-1.5 rounded-full', autoScroll ? 'bg-[#3fb950]' : 'bg-[#f0883e]')} />
                {autoScroll ? '自动滚动' : '已暂停滚动'}
              </span>
              <span className="flex items-center gap-1.5">
                <span className={cn('h-1.5 w-1.5 rounded-full', paused ? 'bg-[#f0883e]' : 'bg-[#3fb950] animate-pulse')} />
                {paused ? '视图已冻结' : '实时更新中'}
              </span>
            </div>
          </div>
        </Card>
      </div>

      <Drawer open={!!detail} onClose={closeDetail} label="日志详情" width={640}>
        {detail && (
          <LogDetailDrawer
            line={detail}
            neighbors={neighbors}
            query={searchQuery}
            onSelect={setDetail}
            onClose={closeDetail}
            onFilterSource={selectSource}
          />
        )}
      </Drawer>
    </div>
  );
}
