import { useState, type ReactNode } from 'react';
import { AlertCircle, AlertTriangle, ChevronLeft, ChevronRight, Download, Info, Search } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Button } from '@/components/kit/Button';
import { Modal } from '@/components/kit/Modal';
import { Field, Input, Select } from '@/components/kit';
import { Switch } from '@/components/kit/Switch';
import { EmptyState } from '@/components/kit/Feedback';
import { toast } from '@/components/kit/Toast';
import type { Tint } from '@/components/kit';

/** 复用预览的头像块: 首字 + 渐变玻璃底 */
export function Avatar({ name, tint = 'green', size = 'md', live }: {
  name: string; tint?: Tint; size?: 'sm' | 'md' | 'lg'; live?: boolean;
}) {
  const dim = size === 'lg' ? 'h-12 w-12 rounded-[15px] text-[17px]' : size === 'sm' ? 'h-8 w-8 rounded-[10px] text-[12px]' : 'h-10 w-10 rounded-xl text-[14px]';
  return (
    <span className={cn('relative grid flex-none place-items-center font-semibold', dim, `tint-${tint}`,
      'border border-[rgb(var(--tint)/0.25)] bg-[linear-gradient(140deg,rgb(var(--tint)/0.32),rgb(var(--tint)/0.08))] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]')}>
      {name.trim().slice(0, 1).toUpperCase() || '?'}
      {live && <span className="absolute -bottom-1 -right-1 rounded-full bg-[var(--bg-base)] p-[3px]"><span className="dot tint-green block !h-2 !w-2" data-live="1" /></span>}
    </span>
  );
}

export function StatTile({ label, value, hint, tint }: { label: string; value: ReactNode; hint?: ReactNode; tint?: Tint }) {
  return (
    <div className={cn('min-w-0 rounded-xl border border-line bg-glass px-3 py-2', tint && `tint-${tint}`)}>
      <div className="text-[11px] text-fg-4">{label}</div>
      <div className="mt-0.5 truncate text-[15px] font-semibold text-fg">{value}</div>
      {hint !== undefined && <div className="mt-0.5 truncate text-[10.5px] text-fg-4">{hint}</div>}
    </div>
  );
}

export function Notice({ tone = 'info', children, className }: {
  tone?: 'info' | 'warning' | 'error' | 'success'; children: ReactNode; className?: string;
}) {
  const meta = {
    info: { icon: Info, cls: 'tint-accent' },
    warning: { icon: AlertTriangle, cls: 'tint-orange' },
    error: { icon: AlertCircle, cls: 'tint-red' },
    success: { icon: Info, cls: 'tint-green' },
  }[tone];
  const Icon = meta.icon;
  return (
    <div className={cn('flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.22)] bg-[rgb(var(--tint)/0.08)] px-3 py-2 text-[12.5px] leading-5 text-fg-2', meta.cls, className)}>
      <Icon className="mt-0.5 h-3.5 w-3.5 flex-none text-tint" />
      <span className="min-w-0">{children}</span>
    </div>
  );
}

export function KeyValue({ label, children, mono }: { label: ReactNode; children: ReactNode; mono?: boolean }) {
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-3 text-[12px]">
      <span className="flex-none text-fg-4">{label}</span>
      <span className={cn('min-w-0 break-all text-right text-fg-2', mono && 'font-mono text-[11px]')}>{children}</span>
    </div>
  );
}

export function Pager({ page, pageCount, total, pageSize, onPage, label = '条', disabled, prevLabel = '上一页', nextLabel = '下一页' }: {
  page: number; pageCount: number; total: number; pageSize: number; onPage: (page: number) => void; label?: string; disabled?: boolean;
  prevLabel?: string; nextLabel?: string;
}) {
  const from = total === 0 ? 0 : page * pageSize + 1;
  const to = Math.min(total, (page + 1) * pageSize);
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
      <span className="num text-[11px] text-fg-4">{total === 0 ? `共 0 ${label}` : `第 ${from}–${to} ${label} · 共 ${total} ${label}`}</span>
      <div className="flex items-center gap-1.5">
        <Button size="sm" variant="ghost" disabled={disabled || page <= 0} onClick={() => onPage(page - 1)} icon={<ChevronLeft className="h-3.5 w-3.5" />} aria-label={prevLabel}>{prevLabel}</Button>
        <span className="num min-w-12 text-center text-[11px] text-fg-4">{page + 1} / {Math.max(1, pageCount)}</span>
        <Button size="sm" variant="ghost" disabled={disabled || page + 1 >= pageCount} onClick={() => onPage(page + 1)} aria-label={nextLabel}>{nextLabel}<ChevronRight className="h-3.5 w-3.5" /></Button>
      </div>
    </div>
  );
}

export function InlineEmpty({ icon, title, description, action }: {
  icon: ReactNode; title: string; description?: ReactNode; action?: ReactNode;
}) {
  return <EmptyState icon={icon} tint="green" title={title} description={description} actions={action} className="py-8" />;
}

export function SearchField({ value, onChange, placeholder, ariaLabel, onEnter }: {
  value: string; onChange: (value: string) => void; placeholder: string; ariaLabel: string; onEnter?: () => void;
}) {
  return (
    <Input leading={<Search className="h-4 w-4" />} value={value} onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => { if (e.key === 'Enter' && onEnter) { e.preventDefault(); onEnter(); } }}
      placeholder={placeholder} aria-label={ariaLabel} className="!h-9 text-[13px]" />
  );
}

export function LayerNote({ children }: { children: ReactNode }) {
  return <p className="mt-1.5 text-[11.5px] leading-5 text-fg-4">{children}</p>;
}

/** 导出当前已加载的数据层; 纯前端下载, 不修改任何后端数据 */
export function ExportDialog({ open, onClose, title, layer, payload }: {
  open: boolean; onClose: () => void; title: string; layer: 'context' | 'history'; payload: unknown[];
}) {
  const [format, setFormat] = useState<'json' | 'jsonl'>('json');
  const [includeReasoning, setIncludeReasoning] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = () => {
    setBusy(true);
    window.setTimeout(() => {
      const items = payload.map((item) => {
        if (includeReasoning || !item || typeof item !== 'object') return item;
        const clone = { ...(item as Record<string, unknown>) };
        delete clone.reasoning;
        delete clone.reasoning_content;
        delete clone.thinking;
        return clone;
      });
      const name = `satrap-${title.replace(/[^\w.-]+/g, '-')}-${layer === 'context' ? 'model-context' : 'chat-history'}.${format}`;
      const text = format === 'json'
        ? JSON.stringify({ title, layer, exported_at: new Date().toISOString(), items }, null, 2)
        : items.map((item) => JSON.stringify(item)).join('\n');
      const url = URL.createObjectURL(new Blob([text], { type: 'application/json;charset=utf-8' }));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = name;
      anchor.click();
      URL.revokeObjectURL(url);
      setBusy(false);
      onClose();
      toast.success('已导出对话数据', `${name} · ${items.length} 项`);
    }, 360);
  };

  return (
    <Modal open={open} onClose={onClose} title="导出对话" description={`${title} · ${layer === 'context' ? '模型上下文' : '展示历史'}`}
      icon={<Download className="h-[18px] w-[18px]" />} className="tint-green"
      footer={<><Button variant="ghost" onClick={onClose} disabled={busy}>取消</Button><Button variant="tinted" tint="green" loading={busy} onClick={run} icon={<Download className="h-4 w-4" />}>导出文件</Button></>}>
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="文件格式" hint="JSON 便于人工查看；JSONL 适合逐行消费">
            <Select value={format} onChange={(v) => setFormat(v as 'json' | 'jsonl')} options={[
              { value: 'json', label: 'JSON', description: '带元数据的结构化文件' },
              { value: 'jsonl', label: 'JSONL', description: '每行一条记录' },
            ]} />
          </Field>
          <Field label="导出范围">
            <Input value={`${payload.length} 项 · 当前已加载`} readOnly aria-label="导出范围" />
          </Field>
        </div>
        <div className="rounded-xl border border-line bg-glass px-4">
          <Field inline label="包含思考内容" hint="默认不导出 reasoning / thinking 字段">
            <Switch label="包含思考内容" checked={includeReasoning} onChange={setIncludeReasoning} />
          </Field>
        </div>
        <Notice>导出只读取当前数据层，不会修改上下文或展示历史；备份与修订号不随文件导出。</Notice>
      </div>
    </Modal>
  );
}

/** 重试 / 关闭等小图标按钮的场景共用 */
