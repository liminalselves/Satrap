import { useEffect, useState } from 'react';
import {
  AlertTriangle, Check, CornerDownLeft, Keyboard, ShieldCheck, Undo2, X, XCircle,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, IconButton, Modal, Textarea } from '@/components/kit';
import {
  actionDescription, isActionable, relativeTime, sourceLabels, type UnifiedAction,
} from './data';

export interface DecisionModalProps {
  open: boolean;
  items: UnifiedAction[];
  approve: boolean;
  initialNote?: string;
  now: number;
  busy?: boolean;
  onClose: () => void;
  onConfirm: (note: string) => void;
}

export function DecisionModal({ open, items, approve, initialNote = '', now, busy, onClose, onConfirm }: DecisionModalProps) {
  const [note, setNote] = useState(initialNote);

  useEffect(() => {
    if (open) setNote(initialNote);
  }, [open, initialNote]);

  const actionable = items.filter((item) => isActionable(item, now));
  const blocked = items.filter((item) => !isActionable(item, now));
  const single = items.length === 1 ? items[0] : null;
  const detail = single
    ? single.action_type === 'delete_friend'
      ? '只解除好友关系，历史对话与上下文保留。'
      : ['handle_request', 'handle_group_request'].includes(single.action_type)
        ? '按原申请尝试处理一次，平台结果以实际返回为准。'
        : ''
    : '';

  return (
    <Modal
      open={open}
      onClose={onClose}
      size={items.length > 1 ? 'lg' : 'md'}
      title={approve ? '批准执行' : '驳回操作'}
      description={items.length > 1 ? `将处理 ${items.length} 项审批，其中 ${actionable.length} 项可提交（每批最多 20 项）。` : '确认后将记录本次审批决定，提交前有 5 秒撤销窗口。'}
      icon={approve ? <ShieldCheck className="h-[18px] w-[18px]" /> : <XCircle className="h-[18px] w-[18px]" />}
      className={approve ? 'tint-green' : 'tint-red'}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button
            data-autofocus
            variant={approve ? 'primary' : 'danger'}
            icon={approve ? <Check className="h-4 w-4" /> : <X className="h-4 w-4" />}
            disabled={actionable.length === 0 || busy}
            loading={busy}
            onClick={() => onConfirm(note.trim())}
          >
            {approve ? '确认通过' : '确认驳回'}{actionable.length > 1 ? ` ${actionable.length} 项` : ''}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className={cn('flex gap-2.5 rounded-xl border px-3.5 py-2.5', approve ? 'tint-green' : 'tint-red', 'border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)]')}>
          <AlertTriangle className="mt-0.5 h-4 w-4 flex-none text-tint" />
          <p className="text-[12px] leading-5 text-fg-2">
            {approve
              ? '通过后会写入决策并提交到平台执行，平台结果以 action.state 为准。执行中的动作不能撤回。'
              : '驳回后动作不会提交到平台，原始申请仍在对应业务页，可重新发起。'}
          </p>
        </div>

        <div className="scroll max-h-[260px] space-y-1.5 rounded-xl border border-line bg-glass p-2">
          {items.map((item) => {
            const ok = isActionable(item, now);
            return (
              <div key={`${item.platform_id}-${item.self_id}-${item.source}-${item.action_id}`} className={cn('rounded-lg px-2.5 py-2', ok ? 'bg-glass' : 'bg-[rgb(var(--rgb-orange)/0.06)]')}>
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-fg">{actionDescription(item)}</span>
                  {(item.risk === 'high' || item.risk === 'medium') && <Badge tone={item.risk === 'high' ? 'red' : 'orange'}>{item.risk === 'high' ? '高风险' : '需复核'}</Badge>}
                  {!ok && <Badge tone="orange">不可提交</Badge>}
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10.5px] text-fg-4">
                  <span>{sourceLabels[item.source]}</span>
                  <span>{item.platform_id}</span>
                  <span>机器人 {item.self_id}</span>
                  <span>修订 {item.revision}</span>
                  {item.group_id && <span>群 {item.group_id}</span>}
                  <span>{relativeTime(item.created_at, now)}</span>
                </div>
                {!ok && <p className="mt-1 text-[10.5px] text-orange">{item.unavailable_reason || '审批已到期或状态已变化'}</p>}
              </div>
            );
          })}
        </div>

        {blocked.length > 0 && (
          <p className="flex items-start gap-1.5 text-[11.5px] text-orange">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />
            {blocked.length} 项已过期、不可处理或结果未确认，将跳过并保留在原列表。
          </p>
        )}

        {detail && <p className="rounded-lg bg-glass px-3 py-2 text-[11.5px] text-fg-3">{detail}</p>}

        <div>
          <label htmlFor="decision-note" className="text-[12.5px] font-medium text-fg">
            附言 <span className="text-[10.5px] font-normal text-fg-4">（可选，最多 1000 字）</span>
          </label>
          <Textarea
            id="decision-note"
            rows={2}
            maxLength={1000}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder={approve ? '例如：已核对群规，确认执行' : '例如：先确认对方身份，暂不处理'}
            className="mt-1.5 !text-[13px]"
          />
          <div className="mt-1 flex items-center justify-between text-[10.5px] text-fg-4">
            <span>附言会随审批结果记录</span>
            <span className="num">{note.length}/1000</span>
          </div>
        </div>
      </div>
    </Modal>
  );
}

const shortcuts: { keys: string[]; label: string }[] = [
  { keys: ['J', 'K'], label: '移动到下一条 / 上一条审批' },
  { keys: ['A'], label: '通过当前审批（打开确认）' },
  { keys: ['R'], label: '拒绝当前审批（打开确认）' },
  { keys: ['X'], label: '勾选 / 取消勾选当前审批' },
  { keys: ['/'], label: '聚焦搜索框' },
  { keys: ['?'], label: '打开快捷键说明' },
  { keys: ['Esc'], label: '关闭详情、弹窗或筛选抽屉' },
];

export function ShortcutsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} size="md" title="键盘快捷键" description="输入框聚焦时快捷键自动暂停"
      icon={<Keyboard className="h-[18px] w-[18px]" />} className="tint-pink"
      footer={<Button onClick={onClose}>知道了</Button>}>
      <div className="space-y-1">
        {shortcuts.map((item) => (
          <div key={item.label} className="flex items-center gap-3 rounded-lg px-1 py-2 transition hover:bg-glass-hover">
            <span className="flex w-20 flex-none items-center gap-1">
              {item.keys.map((key) => <kbd key={key}>{key}</kbd>)}
            </span>
            <span className="text-[12.5px] text-fg-2">{item.label}</span>
          </div>
        ))}
      </div>
      <div className="mt-4 flex items-start gap-2 rounded-xl border border-line bg-glass px-3 py-2.5 text-[11px] text-fg-3">
        <CornerDownLeft className="mt-0.5 h-3.5 w-3.5 flex-none" />
        快捷键只在审批页面内生效；确认弹窗打开时，按 Enter 可直接确认。
      </div>
    </Modal>
  );
}

export function BatchBar({ count, actionable, onApprove, onReject, onClear, disabled }: {
  count: number; actionable: number; onApprove: () => void; onReject: () => void; onClear: () => void; disabled?: boolean;
}) {
  if (count === 0) return null;
  return (
    <div className="pointer-events-none fixed bottom-5 left-1/2 z-50 -translate-x-1/2 px-2 transition-all duration-500 [transition-timing-function:var(--ease-spring)] md:left-[calc(50%+124px)]">
      <div role="region" aria-label="批量审批操作"
        className="surface surface-strong tint-pink pointer-events-auto flex flex-wrap items-center gap-3 !rounded-2xl py-2 pl-4 pr-2 !shadow-[var(--shadow-3)]">
        <span className="num flex items-center gap-2 text-[12.5px] text-fg-2">
          <span className="grid h-6 min-w-6 place-items-center rounded-full bg-[rgb(var(--tint)/0.2)] px-1.5 text-[11px] font-semibold text-tint">{count}</span>
          项已选
        </span>
        {actionable < count && (
          <span className="flex items-center gap-1 text-[11px] text-orange"><AlertTriangle className="h-3 w-3" />{count - actionable} 项不可处理</span>
        )}
        <span className="hidden h-5 w-px bg-[var(--glass-border-strong)] sm:block" />
        <Button size="sm" variant="tinted" tint="red" icon={<X className="h-3.5 w-3.5" />} disabled={disabled || actionable === 0} onClick={onReject}>拒绝所选</Button>
        <Button size="sm" variant="primary" icon={<Check className="h-3.5 w-3.5" />} disabled={disabled || actionable === 0} onClick={onApprove}>通过所选</Button>
        <IconButton label="取消选择" size="sm" onClick={onClear}><X className="h-4 w-4" /></IconButton>
      </div>
    </div>
  );
}

export function UndoBar({ open, title, description, duration, raised, onUndo, onDismiss }: {
  open: boolean; title: string; description: string; duration: number; raised?: boolean; onUndo: () => void; onDismiss: () => void;
}) {
  if (!open) return null;
  return (
    <div className={cn('pointer-events-none fixed left-1/2 z-50 w-[min(420px,calc(100vw-24px))] -translate-x-1/2 transition-all duration-500 [transition-timing-function:var(--ease-spring)] md:left-[calc(50%+124px)]', raised ? 'bottom-28' : 'bottom-5')}>
      <div role="status" aria-live="polite"
        className="surface surface-strong tint-green pointer-events-auto relative flex items-center gap-3 overflow-hidden !rounded-xl py-2.5 pl-4 pr-2 !shadow-[var(--shadow-3)]">
        <span className="grid h-7 w-7 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.16)] text-tint"><Undo2 className="h-4 w-4" /></span>
        <div className="min-w-0 flex-1">
          <p className="text-[12.5px] font-medium text-fg">{title}</p>
          <p className="mt-0.5 truncate text-[11px] text-fg-3">{description}</p>
        </div>
        <Button size="sm" variant="tinted" tint="green" onClick={onUndo}>撤销</Button>
        <IconButton label="关闭撤销提示" size="sm" onClick={onDismiss}><X className="h-3.5 w-3.5" /></IconButton>
        <span key={`${title}-${duration}`} aria-hidden
          className="absolute bottom-0 left-0 h-[2px] bg-[rgb(var(--tint)/0.75)]"
          style={{ animation: `toast-progress ${duration}ms linear forwards` }} />
      </div>
    </div>
  );
}
