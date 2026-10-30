import { useMemo, useState, type ReactNode } from 'react';
import {
  AlertTriangle, Braces, ChevronRight, Clock, Copy, FileJson, Hash, Radio, Server, Terminal, X,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, IconButton, Tabs, toast } from '@/components/kit';
import { formatTime, levelMeta, sourceMeta, type LogSource } from './data';
import { Highlight, type LogEntry } from './VirtualLogList';

const SOURCE_ICON: Record<string, typeof Server> = {
  control: Server,
  backend: Terminal,
  chat: Radio,
};

function MetaCell({ icon, label, value, mono }: { icon: ReactNode; label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0 rounded-xl border border-line bg-glass px-3 py-2">
      <div className="flex items-center gap-1.5 text-[10.5px] text-fg-4">{icon}{label}</div>
      <div className={cn('mt-0.5 truncate text-[12.5px] text-fg-2', mono && 'num font-mono')} title={value}>{value}</div>
    </div>
  );
}

/** 单行日志检查器: 正文 / 原始记录 / 元信息 / 上下文 */
export function LogDetailDrawer({ line, neighbors, query, onSelect, onClose, onFilterSource }: {
  line: LogEntry;
  neighbors: LogEntry[];
  query: string;
  onSelect: (line: LogEntry) => void;
  onClose: () => void;
  onFilterSource: (source: LogSource) => void;
}) {
  const [tab, setTab] = useState<'content' | 'raw'>('content');
  const meta = levelMeta(line.level);
  const source = sourceMeta(line.source);
  const Icon = SOURCE_ICON[line.source ?? ''] ?? Server;
  const raw = useMemo(() => JSON.stringify({
    timestamp: line.timestamp ?? null,
    level: line.level,
    source: line.source ?? null,
    sequence: line.sequence ?? null,
    pid: line.pid ?? null,
    stream_id: line.stream_id ?? null,
    truncated: !!line.truncated,
    content: line.content,
  }, null, 2), [line]);

  const copy = () => {
    const text = tab === 'raw' ? raw : line.content;
    const pending = navigator.clipboard?.writeText(text);
    pending?.catch(() => undefined);
    toast.success(tab === 'raw' ? '已复制原始记录' : '已复制日志内容');
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className={cn('relative overflow-hidden rounded-t-[22px] px-5 pb-4 pt-5', `tint-${meta.tint}`)}>
        <div aria-hidden className="pointer-events-none absolute -right-12 -top-16 h-48 w-48 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.32),transparent_65%)] blur-xl" />
        <div className="relative flex items-start gap-3">
          <div className="grid h-10 w-10 flex-none place-items-center rounded-xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.14)] text-tint">
            <Icon className="h-4 w-4" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-semibold text-fg">日志详情</h2>
              <Badge tone={meta.tint} dot>{line.level}</Badge>
              {line.truncated && <Badge tone="orange">内容已截断</Badge>}
            </div>
            <p className="num mt-1 font-mono text-[11.5px] text-fg-4">
              {formatTime(line.timestamp)} · {source.name} · #{line.sequence ?? '—'}
            </p>
          </div>
          <IconButton label="关闭" shortcut="Esc" size="sm" onClick={onClose}><X className="h-4 w-4" /></IconButton>
        </div>
      </div>

      <div className="scroll min-h-0 flex-1 space-y-4 px-5 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <Tabs size="sm" label="日志视图" value={tab} onChange={setTab} items={[
            { value: 'content', label: '正文' },
            { value: 'raw', label: '原始记录' },
          ]} />
          <span className="ml-auto" />
          <IconButton label="复制" size="sm" onClick={copy}><Copy className="h-3.5 w-3.5" /></IconButton>
        </div>

        {line.truncated && (
          <div className="flex gap-2 rounded-xl border border-[rgb(var(--rgb-orange)/0.24)] bg-[rgb(var(--rgb-orange)/0.08)] px-3 py-2 text-[12.5px] text-fg-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-none text-orange" />
            原始日志超过单条上限, 服务端只保留了有限内容. 完整内容请查看对应日志文件.
          </div>
        )}

        <pre
          key={tab}
          className="scroll max-h-[300px] overflow-auto whitespace-pre-wrap break-words rounded-xl border border-[rgb(255_255_255/0.09)] bg-[linear-gradient(180deg,rgb(12_15_23/0.98),rgb(8_10_16/1))] p-4 font-mono text-[12.5px] leading-[1.75] text-[#c9d1d9] animate-fade"
        >
          {tab === 'raw' ? raw : <Highlight text={line.content} query={query} />}
        </pre>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <MetaCell icon={<Clock className="h-3 w-3" />} label="时间" value={formatTime(line.timestamp)} mono />
          <MetaCell icon={<Hash className="h-3 w-3" />} label="序号" value={line.sequence === undefined ? '—' : `#${line.sequence}`} mono />
          <MetaCell icon={<Server className="h-3 w-3" />} label="来源" value={source.name} />
          <MetaCell icon={<Radio className="h-3 w-3" />} label="PID" value={line.pid === undefined ? '—' : String(line.pid)} mono />
          <MetaCell icon={<Braces className="h-3 w-3" />} label="流 ID" value={line.stream_id || '—'} mono />
          <MetaCell icon={<FileJson className="h-3 w-3" />} label="内容状态" value={line.truncated ? '已截断' : '完整'} />
          <MetaCell icon={<Terminal className="h-3 w-3" />} label="Unix 时间" value={line.timestamp === undefined ? '—' : line.timestamp.toFixed(3)} mono />
          <MetaCell icon={<Server className="h-3 w-3" />} label="服务" value={source.short} mono />
        </div>

        <div>
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-[12.5px] font-medium text-fg-2">上下文</h3>
            <span className="text-[10.5px] text-fg-4">点击切换到相邻日志</span>
          </div>
          {neighbors.length === 0 ? (
            <p className="rounded-xl border border-dashed border-[var(--glass-border-strong)] px-3 py-4 text-center text-[12px] text-fg-4">
              当前筛选结果里没有相邻日志
            </p>
          ) : (
            <div className="space-y-0.5 rounded-xl border border-line bg-glass p-1">
              {neighbors.map((item) => {
                const itemMeta = levelMeta(item.level);
                return (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => onSelect(item)}
                    className="group flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-glass-hover"
                  >
                    <span className="num w-[74px] flex-none font-mono text-[10px] text-fg-4">{formatTime(item.timestamp)}</span>
                    <span className={cn('w-[58px] flex-none font-mono text-[9.5px] tracking-wide', `tint-${itemMeta.tint}`, 'text-tint')}>{item.level}</span>
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg-2">{item.content}</span>
                    <ChevronRight className="h-3 w-3 flex-none text-fg-4 opacity-0 transition-opacity group-hover:opacity-100" />
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-5 py-3">
        <Button size="sm" variant="tinted" tint="orange" onClick={() => onFilterSource((line.source as LogSource | undefined) ?? 'control')}>
          只看 {source.name}
        </Button>
        <div className="flex items-center gap-2">
          <Button size="sm" icon={<Copy className="h-3.5 w-3.5" />} onClick={copy}>复制</Button>
          <Button size="sm" variant="ghost" onClick={onClose}>关闭</Button>
        </div>
      </div>
    </div>
  );
}
