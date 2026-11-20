import { Fragment } from 'react';
import { Route, X } from 'lucide-react';
import { Badge } from '@/components/kit';
import { cn } from '@/utils/cn';
import type { FlowNode, TypeIndex } from './flowModel';
import type { ActivationTrace, TraceLink, TracePath } from './trace';

const LINK_TEXT: Record<TraceLink, string> = { event: '触发', done: '完成后触发', handoff: '调用交接', reply: '回复' };

/** 正在追踪的目标和选中的链路; path 为 null 时高亮全部链路 */
export interface TraceFocus {
  /** 追踪目标节点 id */
  target: string;
  /** 选中的链路下标 */
  path: number | null;
}

function rootTag(path: TracePath, types: TypeIndex) {
  if (path.root === null) return <Badge tone="red">断开, 不会触发</Badge>;
  if (path.root === 'wake') return <Badge tone="pink">唤醒 · 前台</Badge>;
  if (path.root === 'threshold') return <Badge tone="teal">阈值 · 后台</Badge>;
  return <Badge tone="neutral">{types.get(path.root)?.label ?? path.root}</Badge>;
}

/** 一条链路: 从目标往前排, 每一步可点击去编辑 */
function PathRow({ path, index, active, nodes, types, onPick, onSelect }: {
  path: TracePath; index: number; active: boolean; nodes: FlowNode[]; types: TypeIndex;
  onPick: () => void; onSelect: (id: string) => void;
}) {
  const title = (id: string) => nodes.find((node) => node.id === id)?.data.title ?? id;
  const back = [...path.steps].reverse();
  const conditions = path.steps.flatMap((step) => step.conditions);
  return (
    <div data-trace-path={index} className={cn('rounded-lg border px-2.5 py-2 text-[12px]', active ? 'border-[rgb(var(--rgb-accent)/0.6)] bg-[rgb(var(--rgb-accent)/0.08)]' : 'border-line')}>
      <button type="button" onClick={onPick} className="flex w-full flex-wrap items-center gap-1.5 text-left" aria-pressed={active} aria-label={`高亮链路 ${index + 1}`}>
        <span className="font-mono text-[11px] text-fg-3">#{index + 1}</span>
        {rootTag(path, types)}
        {path.modelDecides && <Badge tone="orange">模型决定</Badge>}
        {path.steps.some((step) => step.joinAll) && <Badge tone="purple">AND 汇合</Badge>}
        {conditions.length > 0 && <span className="text-[11px] text-fg-3">条件 {conditions.length}</span>}
      </button>
      <div className="mt-1.5 flex flex-wrap items-center gap-1">
        {back.map((step, at) => (
          <Fragment key={`${step.id}-${at}`}>
            {at > 0 && <span className="text-[10px] text-fg-4">← {LINK_TEXT[back[at].link ?? 'event']}</span>}
            <button type="button" onClick={() => onSelect(step.id)} title="选中并编辑这个节点"
              className={cn('rounded border px-1.5 py-px hover:bg-glass-hover',
                step.kind === 'dead' ? 'border-[rgb(var(--rgb-red)/0.5)] text-red' : step.kind === 'handoff' ? 'border-[rgb(var(--rgb-orange)/0.45)] text-orange' : 'border-line text-fg-2')}>
              {title(step.id)}
            </button>
          </Fragment>
        ))}
      </div>
      {conditions.length > 0 && (
        <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-fg-3">
          条件
          {conditions.map((id, at) => (
            <button key={`${id}-${at}`} type="button" onClick={() => onSelect(id)} className="rounded border border-dashed border-[rgb(var(--rgb-green)/0.5)] px-1.5 py-px text-green hover:bg-glass-hover">{title(id)}</button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 检查器里的激活链路列表 */
export function TraceList({ trace, focus, nodes, types, onTrace, onSelect }: {
  trace: ActivationTrace; focus: TraceFocus | null; nodes: FlowNode[]; types: TypeIndex;
  onTrace: (focus: TraceFocus | null) => void; onSelect: (id: string) => void;
}) {
  const on = focus?.target === trace.target;
  if (!trace.paths.length) return <p className="text-[12px] text-fg-3">没有激活链路</p>;
  return (
    <div className="space-y-1.5" aria-label="激活链路">
      {trace.paths.map((path, index) => (
        <PathRow key={index} path={path} index={index} active={on && (focus.path === null || focus.path === index)} nodes={nodes} types={types}
          onPick={() => onTrace(on && focus.path === index ? { target: trace.target, path: null } : { target: trace.target, path: index })} onSelect={onSelect} />
      ))}
      {trace.truncated && <p className="text-[11px] text-orange">链路过多, 只列出前 {trace.paths.length} 条</p>}
    </div>
  );
}

/** 画布左上角的追踪横幅: 切换链路, 退出追踪 */
export function TraceBanner({ trace, focus, title, onTrace, onSelect }: {
  trace: ActivationTrace; focus: TraceFocus; title: string;
  onTrace: (focus: TraceFocus | null) => void; onSelect: (id: string) => void;
}) {
  const count = trace.paths.length;
  const pill = (label: string, path: number | null) => (
    <button key={label} type="button" onClick={() => onTrace({ target: trace.target, path })} aria-pressed={focus.path === path}
      className={cn('rounded-full px-2 py-px text-[11px]', focus.path === path ? 'bg-[rgb(var(--rgb-accent)/0.2)] text-accent' : 'text-fg-3 hover:bg-glass-hover')}>{label}</button>
  );
  return (
    <div role="status" aria-label="激活链路追踪" className="absolute left-3 top-3 z-10 flex max-w-[60%] flex-wrap items-center gap-1.5 rounded-xl border border-line bg-[var(--glass-bg-strong)] px-2.5 py-1.5 text-[12px] shadow-[var(--shadow-1)]">
      <Route className="h-3.5 w-3.5 text-accent" />
      <span className="text-fg-3">激活链路</span>
      <button type="button" onClick={() => onSelect(trace.target)} className="font-medium text-fg hover:underline">{title}</button>
      <span className="text-fg-4">·</span>
      {count === 0 ? <span className="text-orange">没有激活链路</span> : <>
        {pill(`全部 ${count}`, null)}
        {count > 1 && count <= 12 && trace.paths.map((_, index) => pill(String(index + 1), index))}
      </>}
      <button type="button" onClick={() => onTrace(null)} aria-label="退出追踪" title="退出追踪 (Esc)" className="ml-1 rounded p-0.5 text-fg-3 hover:bg-glass-hover hover:text-fg"><X className="h-3.5 w-3.5" /></button>
    </div>
  );
}
