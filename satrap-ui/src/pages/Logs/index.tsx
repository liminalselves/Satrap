import { useEffect, useState, useRef, useCallback, useMemo, memo } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { Select } from '@/components/ui/Select';
import { LOG_LEVELS } from '@/utils/constants';
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
  const [paused, setPaused] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [levelFilter, setLevelFilter] = useState<string[]>(['INFO', 'WARNING', 'ERROR', 'CRITICAL']);
  const [autoScroll, setAutoScroll] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);
  const logIdRef = useRef(0);
  const pausedRef = useRef(paused);
  
  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  const { connect, disconnect, isConnected } = useWebSocket('/ws/logs');

  const handleLog = useCallback((data: { content: string; level: string }) => {
    if (pausedRef.current) return;
    
    const newLog: LogLine = {
      id: logIdRef.current++,
      content: data.content,
      level: data.level,
      timestamp: new Date(),
    };
    setLogs((prev) => [...prev.slice(-499), newLog]);
  }, []);

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
  }, []);

  const filteredLogs = useMemo(() => 
    logs.filter((log) => {
      if (levelFilter.length > 0 && !levelFilter.includes(log.level)) return false;
      if (searchQuery && !log.content.toLowerCase().includes(searchQuery.toLowerCase())) return false;
      return true;
    }),
    [logs, levelFilter, searchQuery]
  );

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
    <div className="space-y-4 h-full flex flex-col">
      <PageHeader
        title="日志监控"
        description="实时查看系统运行日志"
        actions={headerActions}
      />

      {/* 工具栏 */}
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <Button
              variant={paused ? 'primary' : 'default'}
              size="sm"
              onClick={() => setPaused(!paused)}
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

          <div className="flex items-center gap-2">
            <span className="text-sm text-text-secondary">级别:</span>
            <Select
              options={LOG_LEVELS.map((l) => ({ value: l, label: l }))}
              value={levelFilter[0] || ''}
              onChange={(e) => {
                const val = e.target.value;
                setLevelFilter(val ? [val] : LOG_LEVELS.slice());
              }}
              className="w-32"
            />
          </div>
        </div>
      </Card>

      {/* 日志终端 */}
      <Card className="flex-1 min-h-[500px] p-0 overflow-hidden flex flex-col">
        <div
          ref={containerRef}
          onScroll={handleScroll}
          className="flex-1 overflow-auto custom-scrollbar p-4 font-mono text-sm"
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
        <div className="px-4 py-2 border-t border-glass-border flex items-center justify-between text-xs text-text-tertiary">
          <span>共 {filteredLogs.length} 条日志</span>
          <span>{autoScroll ? '自动滚动' : '已暂停滚动'}</span>
        </div>
      </Card>
    </div>
  );
}
