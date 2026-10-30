import { useEffect, useRef, useState } from 'react';
import {
  AlertCircle, ArrowUpRight, Database, FileText, Layers, Loader2, MoreHorizontal,
  RotateCw, Settings2, Star, Trash2, Upload,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { formatRelativeTime } from '@/utils/format';
import { Badge } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { Card } from '@/components/kit/Card';
import { CountUp } from '@/components/kit/Feedback';
import { scopeLabel, jobPhaseLabel, type Kb } from './types';

export type KbAction = 'open' | 'import' | 'edit' | 'rebuild' | 'delete';

function ProgressRing({ value, size = 40, stroke = 4 }: { value: number; size?: number; stroke?: number }) {
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden>
      <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="rgb(var(--tint) / 0.16)" strokeWidth={stroke} />
      <circle
        cx={size / 2} cy={size / 2} r={radius} fill="none"
        stroke="rgb(var(--tint))" strokeWidth={stroke} strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - Math.min(100, Math.max(0, value)) / 100)}
        style={{ transition: 'stroke-dashoffset 700ms var(--ease-out)' }}
      />
    </svg>
  );
}

function CardMenu({ kb, onAction }: { kb: Kb; onAction: (action: KbAction) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', onKey);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('keydown', onKey); };
  }, [open]);

  const item = (label: string, icon: React.ReactNode, action: KbAction, danger = false, disabled = false) => (
    <button
      type="button"
      role="menuitem"
      disabled={disabled}
      onClick={(event) => { event.stopPropagation(); setOpen(false); if (!disabled) onAction(action); }}
      className={cn(
        'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors disabled:opacity-40',
        danger ? 'text-red hover:bg-[rgb(var(--rgb-red)/0.12)]' : 'text-fg-2 hover:bg-glass-hover hover:text-fg',
      )}
    >
      {icon}{label}
    </button>
  );

  return (
    <div
      ref={ref}
      className="relative flex-none"
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <IconButton label={`${kb.name} 的更多操作`} size="sm" active={open} aria-haspopup="menu" aria-expanded={open} onClick={(event) => { event.stopPropagation(); setOpen((value) => !value); }}>
        <MoreHorizontal className="h-4 w-4" />
      </IconButton>
      {open && (
        <div role="menu" aria-label="知识库操作" className="surface surface-strong animate-pop absolute right-0 top-9 z-30 w-44 !rounded-xl p-1 !shadow-[var(--shadow-3)]">
          {item('打开详情', <ArrowUpRight className="h-3.5 w-3.5" />, 'open')}
          {item('导入文档', <Upload className="h-3.5 w-3.5" />, 'import')}
          {item('编辑配置', <Settings2 className="h-3.5 w-3.5" />, 'edit')}
          <div className="divider my-1" />
          {item('重建索引', <RotateCw className="h-3.5 w-3.5" />, 'rebuild', false, kb.documentCount === 0 || kb.status === 'indexing')}
          {item('删除知识库', <Trash2 className="h-3.5 w-3.5" />, 'delete', true)}
        </div>
      )}
    </div>
  );
}

export function KnowledgeBaseCard({ kb, index, onAction }: { kb: Kb; index: number; onAction: (action: KbAction) => void }) {
  const err = kb.status === 'error';
  const indexing = kb.status === 'indexing';
  const progress = kb.progress;
  const empty = kb.documentCount === 0;

  return (
    <Card
      interactive
      tint="purple"
      padding="md"
      flowing={indexing}
      onClick={() => onAction('open')}
      aria-label={`${kb.name}，${scopeLabel(kb)}，${kb.documentCount} 份文档`}
      className="group flex flex-col hover:z-10 focus-within:z-20"
      style={{ ['--i' as string]: index }}
    >
      <div className="flex items-start gap-3">
        <div className="relative grid h-10 w-10 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">
          <Database className="h-[18px] w-[18px]" />
          {indexing && <span aria-hidden className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-[var(--bg-base)] bg-tint animate-pulse" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <h3 className="min-w-0 truncate text-[15px] font-semibold text-fg">
              <button
                type="button"
                className="max-w-full truncate text-left transition-colors hover:text-purple"
                onClick={(event) => { event.stopPropagation(); onAction('open'); }}
              >
                {kb.name}
              </button>
            </h3>
            {kb.isDefault && <Star className="h-3.5 w-3.5 flex-none fill-[var(--fg-purple)] text-purple" aria-label="会话默认知识库" />}
          </div>
          <p className="mt-0.5 truncate text-[11.5px] text-fg-4" title={scopeLabel(kb)}>{scopeLabel(kb)}</p>
        </div>
        <CardMenu kb={kb} onAction={onAction} />
      </div>

      {indexing && (
        <div className="mt-3.5 flex items-center gap-3 rounded-xl border border-[rgb(var(--rgb-purple)/0.24)] bg-[rgb(var(--rgb-purple)/0.08)] p-2.5 tint-purple">
          <div className="relative grid flex-none place-items-center">
            {progress === null
              ? <span className="grid h-10 w-10 place-items-center rounded-full border border-[rgb(var(--tint)/0.24)]"><Loader2 className="h-4 w-4 animate-spin-slow text-tint" aria-hidden /></span>
              : <ProgressRing value={progress} />}
            {progress !== null && <span className="num absolute text-[9.5px] font-semibold text-tint">{Math.round(progress)}%</span>}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[12.5px] font-medium text-fg">正在构建索引</div>
            <div className="num mt-0.5 text-[11px] text-fg-3">
              {progress === null
                ? `阶段：${jobPhaseLabel(kb.phase)} · 总量未知 · 旧索引仍可检索`
                : `已处理 ${Math.round((kb.documentCount * progress) / 100)}/${kb.documentCount} 份 · 旧索引仍可检索`}
            </div>
          </div>
          <Loader2 className="h-4 w-4 flex-none animate-spin-slow text-tint" aria-hidden />
        </div>
      )}

      {err && (
        <div className="mt-3.5 flex items-start gap-2 rounded-xl border border-[rgb(var(--rgb-red)/0.26)] bg-[rgb(var(--rgb-red)/0.08)] px-3 py-2 text-red">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-none" aria-hidden />
          <p className="line-clamp-2 text-[12px] leading-5">{kb.lastError || '上次操作失败'}</p>
        </div>
      )}

      <div className="mt-4 grid grid-cols-2 gap-2">
        <div className={cn('rounded-xl border border-line bg-glass px-3 py-2', empty && 'opacity-60')}>
          <div className="flex items-center gap-1.5 text-[11px] text-fg-4"><FileText className="h-3 w-3" aria-hidden />文档</div>
          <div className="mt-0.5 text-lg font-semibold leading-tight text-fg"><CountUp value={kb.documentCount} /></div>
        </div>
        <div className={cn('rounded-xl border border-line bg-glass px-3 py-2', empty && 'opacity-60')}>
          <div className="flex items-center gap-1.5 text-[11px] text-fg-4"><Layers className="h-3 w-3" aria-hidden />片段</div>
          <div className="mt-0.5 text-lg font-semibold leading-tight text-fg"><CountUp value={kb.chunkCount} /></div>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {err
          ? <Badge tone="red" dot>上次失败</Badge>
          : indexing
            ? <Badge tone="purple" dot live>索引中{progress === null ? '' : ` ${Math.round(progress)}%`}</Badge>
            : empty
              ? <Badge tone="neutral" dot>空知识库</Badge>
              : <Badge tone="green" dot>已就绪</Badge>}
        {kb.isDefault && <Badge tone="purple">会话默认</Badge>}
        <span className="chip max-w-full" title={kb.config.embed || undefined}>
          <Layers className="h-3 w-3 flex-none" aria-hidden />
          <span className="truncate font-mono text-[11px]">{kb.config.embed || '未配置 Embedding'}</span>
        </span>
      </div>

      <div className="mt-auto pt-4">
        <div className="flex items-center justify-between gap-2 border-t border-line pt-3">
          <span className="min-w-0 truncate text-[11px] text-fg-4">
            {empty ? '尚未导入文档' : indexing ? '处理中 · 旧索引可检索' : kb.lastIndexedAt ? `最近索引 ${formatRelativeTime(kb.lastIndexedAt)}` : '最近索引 —'}
          </span>
          <div className="flex flex-none items-center gap-1" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
            <Button size="sm" variant="tinted" tint="purple" icon={<Upload className="h-3.5 w-3.5" />} aria-label="上传文档" onClick={() => onAction('import')}>导入</Button>
            <Button size="sm" variant="ghost" onClick={() => onAction('open')}>详情<ArrowUpRight className="h-3.5 w-3.5" /></Button>
          </div>
        </div>
      </div>
    </Card>
  );
}
