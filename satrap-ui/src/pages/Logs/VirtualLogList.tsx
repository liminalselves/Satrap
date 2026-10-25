import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';

export interface LogEntry { id: number; content: string; level: string; source?: string; timestamp?: number; sequence?: number }
const ESTIMATED_HEIGHT = 28;
const OVERSCAN = 8;

function indexAt(offsets: number[], position: number) {
  let low = 0, high = offsets.length - 1;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (offsets[mid] <= position) low = mid; else high = mid - 1;
  }
  return low;
}

const MeasuredRow = memo(function MeasuredRow({ log, render, onHeight }: {
  log: LogEntry; render: (log: LogEntry) => ReactNode; onHeight: (id: number, height: number) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const row = element.current;
    if (!row) return;
    const measure = () => onHeight(log.id, row.getBoundingClientRect().height);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(row);
    return () => observer.disconnect();
  }, [log.id, onHeight]);
  return <div ref={element} data-log-id={log.id}>{render(log)}</div>;
});

export const VirtualLogList = memo(function VirtualLogList({ logs, render, empty, autoScroll, onBottomChange }: {
  logs: LogEntry[]; render: (log: LogEntry) => ReactNode; empty: ReactNode;
  autoScroll: boolean; onBottomChange: (value: boolean) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const anchor = useRef<{ id: number; offset: number }>();
  const scrollFrame = useRef<number>();
  const dragging = useRef(false);
  const [viewport, setViewport] = useState({ top: 0, height: 600 });
  const heights = useRef(new Map<number, number>());
  const [heightRevision, setHeightRevision] = useState(0);
  const onHeight = useCallback((id: number, height: number) => {
    if (!height) return;
    const cache = heights.current;
    if (cache.get(id) === height) return;
    cache.set(id, height);
    if (cache.size > 6000) {
      for (const key of [...cache.keys()].sort((a, b) => a - b).slice(0, cache.size - 5000)) cache.delete(key);
    }
    setHeightRevision((current) => current + 1);
  }, []);
  const offsets = useMemo(() => {
    const values = [0];
    for (const log of logs) values.push(values[values.length - 1] + (heights.current.get(log.id) || ESTIMATED_HEIGHT));
    return values;
  }, [logs, heightRevision]);
  const start = Math.max(0, indexAt(offsets, viewport.top) - OVERSCAN);
  const end = Math.min(logs.length, indexAt(offsets, viewport.top + viewport.height) + OVERSCAN + 1);
  const captureViewport = useCallback(() => {
    const element = container.current;
    if (!element) return;
    const top = element.scrollTop, height = element.clientHeight;
    setViewport((current) => current.top === top && current.height === height ? current : { top, height });
  }, []);
  useLayoutEffect(() => {
    const element = container.current;
    if (!element) return;
    if (autoScroll) {
      scrollFrame.current = requestAnimationFrame(() => {
        scrollFrame.current = undefined;
        element.scrollTop = element.scrollHeight;
        captureViewport();
      });
    }
    else if (anchor.current) {
      const index = logs.findIndex((log) => log.id === anchor.current?.id);
      if (index >= 0) element.scrollTop = offsets[index] + anchor.current.offset;
    }
    captureViewport();
    return () => {
      if (scrollFrame.current !== undefined) cancelAnimationFrame(scrollFrame.current);
      scrollFrame.current = undefined;
    };
  }, [logs, offsets, autoScroll, captureViewport]);
  useLayoutEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(captureViewport);
    observer.observe(element);
    captureViewport();
    return () => observer.disconnect();
  }, [captureViewport]);
  const scroll = () => {
    const element = container.current;
    if (!element) return;
    if ((!autoScroll || dragging.current) && scrollFrame.current !== undefined) { cancelAnimationFrame(scrollFrame.current); scrollFrame.current = undefined; }
    const index = Math.min(logs.length - 1, indexAt(offsets, element.scrollTop));
    anchor.current = index >= 0 ? { id: logs[index].id, offset: element.scrollTop - offsets[index] } : undefined;
    if (!autoScroll || dragging.current) onBottomChange(element.scrollHeight - element.scrollTop - element.clientHeight < 50);
    // 自动追加与行高测量产生的滚动不代表用户向上阅读, 避免连续批次意外关闭跟随
    captureViewport();
  };
  return <div ref={container} onScroll={scroll} aria-label="日志内容" style={{ overflowAnchor: 'none' }} tabIndex={0}
    onWheel={(event) => { if (event.deltaY < 0) onBottomChange(false); }}
    onTouchMove={() => onBottomChange(false)}
    onKeyDown={(event) => { if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) onBottomChange(false); }}
    onPointerDown={() => { dragging.current = true; }} onPointerUp={() => { dragging.current = false; }} onPointerCancel={() => { dragging.current = false; }}
    className="flex-1 min-h-0 overflow-auto custom-scrollbar p-4 font-mono text-sm">
    {logs.length === 0 ? empty : <>
      <div aria-hidden="true" style={{ height: offsets[start] }} />
      {logs.slice(start, end).map((log) => <MeasuredRow key={log.id} log={log} render={render} onHeight={onHeight} />)}
      <div aria-hidden="true" style={{ height: offsets[logs.length] - offsets[end] }} />
    </>}
  </div>;
});
