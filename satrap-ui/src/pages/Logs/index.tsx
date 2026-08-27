import { useEffect, useState, useRef, useCallback, useMemo, memo } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { Select } from '@/components/ui/Select';
import { LOG_LEVELS } from '@/utils/constants';
import { appendWithLimit, filterLogs, visibleLogs } from '@/utils/adminMigration';
import { useWebSocket } from '@/hooks/useWebSocket';
import { PageHeader } from '@/components/common';
import { Play, Pause, Trash2, Download, Search, Wifi, WifiOff } from 'lucide-react';

interface LogLine {
  id: number;
  content: string;
  level: string;
  timestamp: Date;
}

// 日志行组件 - 使用 memo 优化渲染
const LogLineItem = memo(function LogLineItem({ log }: { log: LogLine }) {
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

export function Logs() {
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [pausedLogs, setPausedLogs] = useState<LogLine[]>([]);
  const [paused, setPaused] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [levelFilter, setLevelFilter] = useState<string[]>(LOG_LEVELS.slice());
  const [historyLines, setHistoryLines] = useState(100);
  const [autoScroll, setAutoScroll] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);
  const logIdRef = useRef(0);

  const { connect, disconnect, isConnected } = useWebSocket(`/ws/logs?lines=${historyLines}`);

  const handleLog = useCallback((data: { content: string; level: string }) => {
    const newLog: LogLine = {
      id: logIdRef.current++,
      content: data.content,
      level: data.level,
      timestamp: new Date(),
    };
    setLogs((prev) => appendWithLimit(prev, newLog, 5000));
  }, []);

  useEffect(() => {
    setLogs([]);
    setPausedLogs([]);
    logIdRef.current = 0;
  }, [historyLines]);

  useEffect(() => {
    connect({
      onLog: handleLog,
      onError: (msg) => console.error('WebSocket error:', msg),
    });
    return () => disconnect();
  }, [connect, disconnect, handleLog]);

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs, autoScroll]);

  const handleScroll = useCallback(() => {
    if (!containerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
    const isAtBottom = scrollHeight - scrollTop - clientHeight < 50;
    setAutoScroll(isAtBottom);
  }, []);

  const clearLogs = useCallback(() => {
    setLogs([]);
    setPausedLogs([]);
  }, []);

  const togglePaused = useCallback(() => {
    setPaused((current) => {
      if (!current) setPausedLogs(logs);
      else setPausedLogs([]);
      return !current;
    });
  }, [logs]);

  const toggleLevel = useCallback((level: string) => {
    setLevelFilter((current) => (
      current.includes(level)
        ? current.filter((item) => item !== level)
        : [...current, level]
    ));
  }, []);

  const filteredLogs = useMemo(
    () => filterLogs(visibleLogs(logs, pausedLogs, paused), levelFilter, searchQuery),
    [levelFilter, logs, paused, pausedLogs, searchQuery]
  );

  const bufferedCount = paused ? Math.max(0, logs.length - pausedLogs.length) : 0;

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
        description="实时查看后端进程的标准输出和标准错误"
        actions={headerActions}
        className="shrink-0"
      />

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
        <div
          ref={containerRef}
          onScroll={handleScroll}
          className="flex-1 min-h-0 overflow-auto custom-scrollbar p-4 font-mono text-sm"
        >
          {filteredLogs.length === 0 ? (
            <div className="text-text-tertiary text-center py-8">
              {isConnected ? '等待日志...' : '未连接到日志服务'}
            </div>
          ) : (
            filteredLogs.map((log) => (
              <LogLineItem key={log.id} log={log} />
            ))
          )}
        </div>

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
