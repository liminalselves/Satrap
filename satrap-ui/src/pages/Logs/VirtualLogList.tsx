import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { cn } from '@/utils/cn';
import { formatTime, levelMeta, sourceMeta, splitSourcePrefix } from './data';

export interface LogEntry {
  id: number;
  content: string;
  level: string;
  source?: string;
  timestamp?: number;
  sequence?: number;
  pid?: number;
  stream_id?: string;
  truncated?: boolean;
}

const ROW_HEIGHT = 24;
const WRAP_ESTIMATE = 38;
const OVERSCAN = 10;
const EMPTY_HEIGHT = 600;

function indexAt(offsets: number[], position: number): number {
  let low = 0;
  let high = offsets.length - 1;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (offsets[mid] <= position) low = mid;
    else high = mid - 1;
  }
  return low;
}

/** 搜索命中高亮 (终端固定配色, 两种主题一致) */
export function Highlight({ text, query }: { text: string; query: string }) {
  return useMemo(() => {
    const needle = query.trim();
    if (!needle) return <>{text}</>;
    const lowerText = text.toLowerCase();
    const lowerNeedle = needle.toLowerCase();
    if (!lowerText.includes(lowerNeedle)) return <>{text}</>;
    const parts: ReactNode[] = [];
    let cursor = 0;
    let key = 0;
    for (;;) {
      const at = lowerText.indexOf(lowerNeedle, cursor);
      if (at < 0) {
        parts.push(text.slice(cursor));
        break;
      }
      if (at > cursor) parts.push(text.slice(cursor, at));
      parts.push(
        <mark key={`m${key++}`} className="rounded-[2px] bg-[rgb(255_180_69/0.28)] px-[1px] text-[#ffd28a]">
          {text.slice(at, at + needle.length)}
        </mark>,
      );
      cursor = at + needle.length;
    }
    return <>{parts}</>;
  }, [text, query]);
}

interface RowProps {
  log: LogEntry;
  wrap: boolean;
  showTime: boolean;
  query: string;
  selected: boolean;
  onSelect: (log: LogEntry) => void;
}

const LogRow = memo(function LogRow({ log, wrap, showTime, query, selected, onSelect }: RowProps) {
  const level = levelMeta(log.level);
  const source = sourceMeta(log.source);
  const { prefix, body } = splitSourcePrefix(log.content, log.source);
  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={`${log.level} ${source.name} ${body.slice(0, 90)}`}
      onClick={() => onSelect(log)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onSelect(log);
        }
      }}
      className={cn(
        'group relative flex w-full cursor-pointer select-text items-start border-b border-[rgb(255_255_255/0.035)] pl-2.5 pr-2 text-left outline-none transition-colors',
        wrap ? 'min-h-6 py-1' : 'h-6 items-center',
        selected ? 'bg-[rgb(255_140_0/0.12)]' : 'hover:bg-[rgb(255_255_255/0.045)]',
        'focus-visible:bg-[rgb(255_255_255/0.06)]',
      )}
    >
      <span aria-hidden className={cn('absolute inset-y-0 left-0 w-[2px]', level.bar, !selected && 'opacity-70')} />
      {showTime && (
        <span className="num mr-2 w-[86px] flex-none font-mono text-[10.5px] leading-4 text-[#6e7681]">
          {formatTime(log.timestamp)}
        </span>
      )}
      <span className={cn('mr-2 w-[62px] flex-none font-mono text-[10px] leading-4 tracking-wide', level.terminal)}>
        {log.level}
      </span>
      <span className="mr-2 hidden w-[76px] flex-none truncate font-mono text-[10px] leading-4 text-[#7d8590] sm:block">
        [{source.name}]
      </span>
      <span
        className={cn(
          'min-w-0 flex-1 font-mono text-[11.5px] leading-[18px] text-[#c9d1d9]',
          wrap ? 'whitespace-pre-wrap break-words' : 'truncate',
        )}
      >
        {/* 服务名已放在独立列; 前缀仅保留在正文的可访问文本中, 不重复占用视觉空间 */}
        {prefix && <span className="sr-only">{prefix}</span>}
        <Highlight text={body} query={query} />
      </span>
      <ChevronRight
        className={cn(
          'ml-1 h-3.5 w-3.5 flex-none text-[#6e7681] transition-opacity',
          wrap && 'mt-0.5',
          selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
        )}
        aria-hidden
      />
    </div>
  );
});

const MeasuredRow = memo(function MeasuredRow({ log, wrap, showTime, query, selected, onSelect, onHeight }: RowProps & {
  onHeight: (id: number, height: number) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!wrap) return;
    const row = element.current;
    if (!row) return;
    const measure = () => onHeight(log.id, row.getBoundingClientRect().height);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(row);
    return () => observer.disconnect();
  }, [log.id, wrap, onHeight]);
  return (
    <div ref={element} data-log-id={log.id}>
      <LogRow log={log} wrap={wrap} showTime={showTime} query={query} selected={selected} onSelect={onSelect} />
    </div>
  );
});

export interface VirtualLogListProps {
  logs: LogEntry[];
  empty: ReactNode;
  autoScroll: boolean;
  onAutoScrollChange: (value: boolean) => void;
  wrap: boolean;
  showTime: boolean;
  query: string;
  selectedId?: number | null;
  onSelect?: (log: LogEntry) => void;
  loading?: boolean;
}

/**
 * 虚拟化日志列表: 固定行高或测量换行行高, 只渲染视口内与 overscan 行.
 * 缓冲到 5000 行仍保持流畅, 并在阅读历史时用锚点抵消新日志造成的位移.
 */
export function VirtualLogList({
  logs, empty, autoScroll, onAutoScrollChange, wrap, showTime, query, selectedId, onSelect, loading,
}: VirtualLogListProps) {
  const container = useRef<HTMLDivElement>(null);
  const heights = useRef(new Map<number, number>());
  const anchor = useRef<{ id: number; offset: number }>();
  /** 上一次的滚动位置: 只有用户向上滚动才关闭自动滚动, 避免新日志撑高内容时误判 */
  const lastTop = useRef(0);
  /** 内容变化后浏览器补发的 scroll 事件可能读到旧位置, 这段时间内不据此关闭自动滚动 */
  const ignoreScrollUntil = useRef(0);
  const [revision, setRevision] = useState(0);
  const [viewport, setViewport] = useState({ top: 0, height: EMPTY_HEIGHT });

  const onHeight = useCallback((id: number, height: number) => {
    if (!height) return;
    if (heights.current.get(id) === height) return;
    heights.current.set(id, height);
    if (heights.current.size > 6000) {
      for (const key of [...heights.current.keys()].sort((a, b) => a - b).slice(0, 1000)) heights.current.delete(key);
    }
    setRevision((value) => value + 1);
  }, []);

  const captureViewport = useCallback(() => {
    const element = container.current;
    if (!element) return;
    setViewport((current) => (
      current.top === element.scrollTop && current.height === element.clientHeight
        ? current
        : { top: element.scrollTop, height: element.clientHeight }
    ));
  }, []);

  // 换行 / 时间戳切换会改变行高, 丢弃旧测量值
  useLayoutEffect(() => {
    heights.current.clear();
    setRevision((value) => value + 1);
  }, [wrap, showTime]);

  const offsets = useMemo(() => {
    const values: number[] = [0];
    for (const log of logs) {
      const height = wrap ? heights.current.get(log.id) ?? WRAP_ESTIMATE : ROW_HEIGHT;
      values.push(values[values.length - 1] + height);
    }
    return values;
  }, [logs, wrap, revision]);

  const start = Math.max(0, indexAt(offsets, viewport.top) - OVERSCAN);
  const end = Math.min(logs.length, indexAt(offsets, viewport.top + viewport.height) + OVERSCAN + 1);

  useLayoutEffect(() => {
    const element = container.current;
    if (!element) return;
    // 在提交后同步定位, 浏览器迟到或合并的 scroll 事件就不会把自动滚动误关掉
    ignoreScrollUntil.current = performance.now() + 100;
    if (autoScroll) {
      element.scrollTop = element.scrollHeight;
      lastTop.current = element.scrollTop;
    } else if (anchor.current) {
      const index = logs.findIndex((log) => log.id === anchor.current?.id);
      if (index >= 0) element.scrollTop = offsets[index] + anchor.current.offset;
      lastTop.current = element.scrollTop;
    }
    captureViewport();
    return undefined;
  }, [logs, offsets, autoScroll, wrap, showTime, captureViewport]);

  useLayoutEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(captureViewport);
    observer.observe(element);
    captureViewport();
    return () => observer.disconnect();
  }, [captureViewport]);

  const handleScroll = () => {
    const element = container.current;
    if (!element) return;
    const index = Math.min(logs.length - 1, indexAt(offsets, element.scrollTop));
    anchor.current = index >= 0 ? { id: logs[index].id, offset: element.scrollTop - offsets[index] } : undefined;
    const top = element.scrollTop;
    const atBottom = element.scrollHeight - top - element.clientHeight < 48;
    // 新日志追加会先撑高 scrollHeight, 迟到的 scroll 事件不能把自动滚动关掉;
    // 只有真正向上滚动(或从已暂停状态继续)才改变开关
    if (atBottom) onAutoScrollChange(true);
    else if (performance.now() >= ignoreScrollUntil.current && (!autoScroll || top < lastTop.current - 1)) onAutoScrollChange(false);
    lastTop.current = top;
    captureViewport();
  };

  return (
    <div
      ref={container}
      role="log"
      aria-label="日志内容"
      tabIndex={0}
      className="scroll relative min-h-0 flex-1 overflow-auto bg-[linear-gradient(180deg,rgb(10_13_20/0.98),rgb(7_9_14/1))] font-mono outline-none"
      style={{ overflowAnchor: 'none' }}
      onScroll={handleScroll}
      onWheel={(event) => { if (event.deltaY < 0 && autoScroll) onAutoScrollChange(false); }}
      onTouchMove={() => { if (autoScroll) onAutoScrollChange(false); }}
      onKeyDown={(event) => {
        if (autoScroll && ['ArrowUp', 'PageUp', 'Home'].includes(event.key)) onAutoScrollChange(false);
      }}
    >
      {loading ? (
        <div className="space-y-3 p-4" aria-busy="true" aria-label="正在载入日志">
          {Array.from({ length: 11 }).map((_, index) => (
            <div key={index} className="flex animate-pulse items-center gap-3">
              <span className="h-2.5 w-[86px] flex-none rounded bg-[rgb(255_255_255/0.05)]" />
              <span className="h-2.5 w-[52px] flex-none rounded bg-[rgb(255_255_255/0.05)]" />
              <span
                className="h-2.5 rounded bg-[rgb(255_255_255/0.05)]"
                style={{ width: `${28 + ((index * 17) % 58)}%` }}
              />
            </div>
          ))}
        </div>
      ) : logs.length === 0 ? (
        <div className="grid h-full place-items-center px-4">{empty}</div>
      ) : (
        <div className="py-1">
          <div aria-hidden style={{ height: offsets[start] }} />
          {logs.slice(start, end).map((log) => (
            <MeasuredRow
              key={log.id}
              log={log}
              wrap={wrap}
              showTime={showTime}
              query={query}
              selected={selectedId === log.id}
              onSelect={onSelect ?? (() => undefined)}
              onHeight={onHeight}
            />
          ))}
          <div aria-hidden style={{ height: offsets[logs.length] - offsets[end] }} />
        </div>
      )}
    </div>
  );
}
