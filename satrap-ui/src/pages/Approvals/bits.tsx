import type { LucideIcon } from 'lucide-react';
import { Brain, Clock, Contact, UserPlus, Users } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge } from '@/components/kit';
import {
  avatarInfo, decisionLabels, decisionTone, isExpired, remainingLabel, riskLabel, riskTone, sourceLabels,
  sourceTint, stateLabels, stateTone, type ApprovalSource, type DecisionOutput, type UnifiedAction,
} from './data';

export const sourceIcons: Record<ApprovalSource, LucideIcon> = {
  group: Users,
  invitation: UserPlus,
  friend: Contact,
  memory: Brain,
};

export function SourceChip({ source, className }: { source: ApprovalSource; className?: string }) {
  const Icon = sourceIcons[source];
  return (
    <Badge tone={sourceTint[source]} className={cn('gap-1.5', className)}>
      <Icon className="h-3 w-3" />
      {sourceLabels[source]}
    </Badge>
  );
}

export function StateChip({ item, now, className }: { item: UnifiedAction; now: number; className?: string }) {
  if (isExpired(item, now)) return <Badge tone="red" dot className={className}>已到期</Badge>;
  const tone = stateTone[item.state] ?? 'neutral';
  const neutral = tone === 'neutral';
  return (
    <Badge tone={tone} dot={!neutral} live={item.state === 'executing'} className={className}>
      {neutral && <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-[var(--text-3)]" />}
      {stateLabels[item.state] ?? item.state}
    </Badge>
  );
}

export function RiskChip({ risk, className }: { risk: string; className?: string }) {
  const key = (risk || 'unknown').toLowerCase();
  if (key === 'low') return null;
  return <Badge tone={riskTone[key] ?? 'neutral'} className={className}>{riskLabel[key] ?? '风险未知'}</Badge>;
}

export function DecisionChip({ output, className }: { output: DecisionOutput; className?: string }) {
  return <Badge tone={decisionTone[output.result] ?? 'neutral'} className={className}>{decisionLabels[output.result] ?? output.result}</Badge>;
}

export function Avatar({ item, size = 'md', className }: {
  item: UnifiedAction; size?: 'sm' | 'md' | 'lg'; className?: string;
}) {
  const info = avatarInfo(item);
  const dims = size === 'lg' ? 'h-12 w-12 rounded-[14px] text-[17px]'
    : size === 'sm' ? 'h-8 w-8 rounded-[10px] text-[12px]' : 'h-10 w-10 rounded-xl text-[14px]';
  return (
    <span
      aria-hidden
      title={info.name}
      className={cn(
        'grid flex-none place-items-center border font-semibold',
        dims,
        `tint-${sourceTint[item.source]}`,
        'border-[rgb(var(--tint)/0.24)] bg-[linear-gradient(135deg,rgb(var(--tint)/0.34),rgb(var(--tint)/0.08))] text-tint shadow-[inset_0_1px_0_var(--glass-highlight),0_6px_18px_-10px_rgb(var(--tint)/0.8)]',
        className,
      )}
    >
      {info.label}
    </span>
  );
}

export function ExpiryText({ item, now, className }: { item: UnifiedAction; now: number; className?: string }) {
  if (item.expires_at === null) return null;
  const expired = item.state === 'expired' || isExpired(item, now);
  return (
    <span className={cn('inline-flex items-center gap-1 text-[11.5px]', expired ? 'text-red' : 'text-fg-3', className)}>
      <Clock className="h-3 w-3" />
      {expired ? '已到期' : remainingLabel(item.expires_at, now)}
    </span>
  );
}

export function KbdHint({ keys, children }: { keys: string; children?: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] text-fg-4">
      <kbd>{keys}</kbd>
      {children}
    </span>
  );
}

export function JsonBlock({ value, title = '完整参数' }: { value: unknown; title?: string }) {
  return (
    <details className="group/json rounded-xl border border-line bg-[rgb(0_0_0/0.16)] [[data-theme=light]_&]:bg-[rgb(15_23_42/0.03)]">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs font-medium text-fg-3 transition hover:text-fg">
        <span className="grid h-4 w-4 place-items-center rounded bg-glass-hover text-[9px] text-fg-3 transition-transform group-open/json:rotate-90">▶</span>
        {title}
      </summary>
      <pre className="scroll max-h-56 overflow-auto border-t border-line px-3 py-2.5 font-mono text-[11.5px] leading-[1.65] text-fg-2">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}
