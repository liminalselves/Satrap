import { useEffect, useState, useRef, useCallback, useMemo, memo } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { Select } from '@/components/ui/Select';
import { LOG_LEVELS } from '@/utils/constants';
import { filterLogs } from '@/utils/adminMigration';
import { LogBuffer, mergeLogSources } from '@/utils/logBuffer';
import { VirtualLogList, type LogEntry } from './VirtualLogList';
import { logServices, readLogSource, type LogSource } from '@/api/logging';
import { isPasswordRequiredError } from '@/api/auth';
import { PageHeader } from '@/components/common';
import { Play, Pause, Trash2, Download, Search, Wifi, WifiOff } from 'lucide-react';



// 日志行组件 - 使用 memo 优化渲染
const LogLineItem = memo(function LogLineItem({ log }: { log: LogEntry }) {
  const levelColor = useMemo(() => {
    switch (log.level) {
      case 'ERROR':
      case 'CRITICAL':
        return 'text-error';
      case 'WARNING':
        return 'text-warning';
      case 'INFO':
        return 'text-success';
      case 'DEBUG':
        return 'text-purple';
      default:
        return 'text-text-primary';
    }
  }, [log.level]);

  const levelDot = useMemo(() => {
    switch (log.level) {
      case 'ERROR':
      case 'CRITICAL':
        return 'status-dot-error';
      case 'WARNING':
        return 'status-dot-warning';
      case 'INFO':
        return 'status-dot-success';
      default:
        return '';
    }
  }, [log.level]);

  return (
    <div className={`py-1 hover:bg-glass-hover px-2 rounded flex items-start gap-2 ${levelColor}`}>
      <span className={`status-dot mt-1.5 ${levelDot}`} />
      <span className="text-text-tertiary mr-1">[{log.level}]</span>
      <span className="flex-1">{log.content}</span>
    </div>
  );
});

const renderLog = (log: LogEntry) => <LogLineItem log={log} />;

export function Logs() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [bufferedCount, setBufferedCount] = useState(0);
  const [paused, setPaused] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [levelFilter, setLevelFilter] = useState<string[]>(LOG_LEVELS.slice());
  const [historyLines, setHistoryLines] = useState(100);
  const [autoScroll, setAutoScroll] = useState(true);
  const [source, setSource] = useState<LogSource | 'all'>('all');
  const [statuses, setStatuses] = useState<Partial<Record<LogSource, string>>>({});
  const [reconnect, setReconnect] = useState(0);
  const cursors = useRef<Partial<Record<LogSource, { after: number; stream_id: string }>>>({});
  const seen = useRef(new Set<string>());
  const logIdRef = useRef(0);
  const buffer = useRef(new LogBuffer<LogEntry>());
  const flushTimer = useRef<ReturnType<typeof setTimeout>>();
  const flush = useCallback(() => {
    if (flushTimer.current !== undefined) clearTimeout(flushTimer.current);
    flushTimer.current = undefined;
    const snapshot = buffer.current.read();
    setLogs(mergeLogSources(snapshot.logs)); setBufferedCount(snapshot.bufferedCount);
  }, []);

  const isConnected = logServices().some((service) => (source === 'all' || service.id === source) && statuses[service.id] === '已连接');

  const handleLog = useCallback((data: Omit<LogEntry, 'id'>) => {
    const newLog: LogEntry = {
      id: logIdRef.current++,
      ...data,
    };
    buffer.current.append(newLog);
    if (flushTimer.current === undefined) flushTimer.current = setTimeout(flush, 100);
  }, [flush]);

  useEffect(() => {
    buffer.current.clear(); seen.current.clear(); cursors.current = {}; flush();
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
        if (result.reset) handleLog({ content: `[${service.name}] 服务已重启或日志游标已过期, 已重新取得有限快照, 中间日志可能未保留`,
          level: 'WARNING', source: service.id, timestamp: result.entries[0]?.timestamp ?? Date.now() / 1000 });
        for (const entry of result.entries) {
          const key = `${service.id}:${result.stream_id}:${entry.sequence}`;
          if (seen.current.has(key)) continue;
          seen.current.add(key);
          if (seen.current.size > 5000) seen.current.delete(seen.current.values().next().value!);
          handleLog({ content: `[${service.name}] ${entry.content}${entry.truncated ? ' [内容已截断]' : ''}`,
            level: entry.level, source: service.id, timestamp: entry.timestamp, sequence: entry.sequence });
        }
        cursors.current[service.id] = { after: result.next_cursor, stream_id: result.stream_id };
        setStatuses((current) => ({ ...current, [service.id]: '已连接' }));
        if (result.has_more) delay = 250;
      } catch (error) {
        if (!disposed) setStatuses((current) => ({ ...current, [service.id]: isPasswordRequiredError(error) ? '需要认证'
          : `离线: ${error instanceof Error ? error.message : '连接失败'}` }));
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

  const clearLogs = useCallback(() => {
    buffer.current.clear(); flush();
  }, [flush]);

  const togglePaused = useCallback(() => {
    buffer.current.pause(!paused); setPaused(!paused); flush();
  }, [paused, flush]);

  const toggleLevel = useCallback((level: string) => {
    setLevelFilter((current) => (
      current.includes(level)
        ? current.filter((item) => item !== level)
        : [...current, level]
    ));
  }, []);

  const filteredLogs = useMemo(
    () => filterLogs(logs.filter((entry) => source === 'all' || entry.source === source), levelFilter, searchQuery),
    [levelFilter, logs, searchQuery, source]
  );
  const empty = useMemo(() => <div className="text-text-tertiary text-center py-8">
    {isConnected ? '等待日志...' : '未连接到日志服务'}
  </div>, [isConnected]);

  const exportLogs = useCallback(() => {
    const content = filteredLogs.map((l) => l.content).join('\n');
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `satrap-logs-${new Date().toISOString()}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }, [filteredLogs]);

  const headerActions = useMemo(() => (
    <>
      <Badge variant={isConnected ? 'success' : 'error'} className="flex items-center gap-1">
        {isConnected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
        {isConnected ? '已连接' : '未连接'}
      </Badge>
      <Badge variant={paused ? 'warning' : 'success'}>
        {paused ? '已暂停' : '实时更新中'}
      </Badge>
    </>
  ), [isConnected, paused]);

  return (
    <div className="h-full min-h-0 overflow-hidden flex flex-col gap-4">
      <PageHeader
        title="日志监控"
        description="独立查看 Control、平台 Backend 和 Chat 的实时日志"
        actions={headerActions}
        className="shrink-0"
      />

      <Card className="p-3 shrink-0">
        <div className="flex flex-wrap items-center gap-3">
          <Select value={source} onChange={(event) => setSource(event.target.value as LogSource | 'all')}
            options={[{ value: 'all', label: '全部来源' }, ...logServices().map((service) => ({ value: service.id, label: service.name }))]} className="w-40" />
          {logServices().map((service) => <span key={service.id} className="text-xs text-text-secondary">{service.name}: {statuses[service.id] ?? '等待连接'}</span>)}
          <Button size="sm" onClick={() => setReconnect((value) => value + 1)}>重新连接</Button>
        </div>
      </Card>
      {/* 工具栏 */}
      <Card className="p-4 shrink-0">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <Button
              variant={paused ? 'primary' : 'default'}
              size="sm"
              onClick={togglePaused}
            >
              {paused ? <Play className="h-4 w-4 mr-1" /> : <Pause className="h-4 w-4 mr-1" />}
              {paused ? '继续' : '暂停'}
            </Button>
            <Button variant="default" size="sm" onClick={clearLogs}>
              <Trash2 className="h-4 w-4 mr-1" />
              清空
            </Button>
            <Button variant="default" size="sm" onClick={exportLogs}>
              <Download className="h-4 w-4 mr-1" />
              导出
            </Button>
          </div>

          <div className="flex-1 min-w-[200px]">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
              <Input
                placeholder="搜索日志..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-10"
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-text-secondary">历史:</span>
            <Select
              options={[50, 100, 200, 300, 500].map((value) => ({ value: String(value), label: `${value} 行` }))}
              value={String(historyLines)}
              onChange={(e) => setHistoryLines(Number(e.target.value))}
              className="w-28"
            />
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-glass-border pt-3">
          <span className="text-sm text-text-secondary">级别:</span>
          {LOG_LEVELS.map((level) => (
            <Button
              key={level}
              variant={levelFilter.includes(level) ? 'primary' : 'subtle'}
              size="sm"
              onClick={() => toggleLevel(level)}
            >
              {level}
            </Button>
          ))}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setLevelFilter(levelFilter.length === LOG_LEVELS.length ? [] : LOG_LEVELS.slice())}
          >
            {levelFilter.length === LOG_LEVELS.length ? '取消全选' : '全部'}
          </Button>
        </div>
      </Card>

      {/* 日志终端 */}
      <Card className="flex-1 min-h-0 p-0 overflow-hidden flex flex-col">
        <VirtualLogList logs={filteredLogs} render={renderLog} empty={empty} autoScroll={autoScroll} onBottomChange={setAutoScroll} />

        {/* 状态栏 */}
        <div className="shrink-0 px-4 py-2 border-t border-glass-border flex items-center justify-between text-xs text-text-tertiary">
          <span>
            共 {filteredLogs.length} 条日志
            {bufferedCount > 0 ? `, 暂停期间收到 ${bufferedCount} 条` : ''}
          </span>
          <span>{autoScroll ? '自动滚动' : '已暂停滚动'}</span>
        </div>
      </Card>
    </div>
  );
}
