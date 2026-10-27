import { useState, type ReactNode } from 'react';
import { AlertTriangle, Check, ChevronLeft, ChevronRight, Copy, Info } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, type Tone } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { toast } from '@/components/kit/Toast';
import type { Tint } from '@/components/kit/tints';

/* ============================== 展示文案 ============================== */

export const membershipLabels: Record<string, string> = {
  joined: '已加入', left: '已离开', unknown: '待确认', config_only: '仅配置未发现',
};

export const responseSourceLabels: Record<string, string> = {
  group: '本群', account: '账号', platform: '平台总开关',
};

export const bindingSourceLabels: Record<string, string> = {
  group: '本群覆盖', platform: '平台默认', default: '系统默认',
};

export const roleLabels: Record<string, string> = { owner: '群主', admin: '管理员', member: '普通成员' };

export const groupEventLabels: Record<string, string> = {
  group_increase: '成员入群', group_decrease: '成员退群', group_recall: '消息撤回',
  group_ban: '群禁言', group_upload: '群文件上传', group_admin: '管理员变更',
  group_card: '群昵称变更', group_request: '加群请求或邀请',
};

export const approvalLabels: Record<string, string> = {
  approval_required: '需审批', auto_execute: '自动执行', default: '继承平台默认',
};

export const capabilityLabels: Record<string, string> = {
  supported: '平台已支持', unsupported: '平台已确认不支持',
  unavailable: '平台连接不可用', unknown: '待平台确认',
};

/* ============================== 基础组件 ============================== */

const avatarSize = {
  sm: 'h-8 w-8 text-[12px] rounded-lg',
  md: 'h-10 w-10 text-[14px] rounded-xl',
  lg: 'h-12 w-12 text-[16px] rounded-2xl',
  xl: 'h-16 w-16 text-[22px] rounded-[18px]',
};

export function Avatar({ name, hue = 330, size = 'md', online, ring, className, square }: {
  name: string; hue?: number; size?: keyof typeof avatarSize; online?: boolean; ring?: boolean; className?: string; square?: boolean;
}) {
  const text = (name || '?').trim().slice(0, 2).toUpperCase();
  return (
    <span
      className={cn('relative inline-grid flex-none place-items-center font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_6px_18px_-8px_rgba(0,0,0,0.7)]', avatarSize[size], square && '!rounded-lg', ring && 'ring-2 ring-[rgb(var(--tint)/0.35)] ring-offset-2 ring-offset-[var(--bg-base)]', className)}
      style={{ background: `linear-gradient(140deg, hsl(${hue} 68% 56%), hsl(${hue + 26} 58% 38%))` }}
    >
      {text}
      {online !== undefined && (
        <span aria-label={online ? '在线' : '离线'} className={cn('absolute -bottom-0.5 -right-0.5 h-3 w-3 rounded-full border-2 border-[var(--bg-base)]', online ? 'bg-green' : 'bg-fg-4')} />
      )}
    </span>
  );
}

export function hueFor(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) hash = (hash * 31 + value.charCodeAt(index)) % 360;
  return hash;
}

export function Note({ children, tone = 'accent', icon, title, action, className }: {
  children?: ReactNode; tone?: Tone; icon?: ReactNode; title?: ReactNode; action?: ReactNode; className?: string;
}) {
  const toneClass = tone === 'neutral' ? '' : `tint-${tone}`;
  return (
    <div className={cn('flex items-start gap-2.5 rounded-xl border px-3.5 py-2.5 text-[13px] leading-5', toneClass,
      tone === 'neutral' ? 'border-line bg-glass text-fg-2' : 'border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)] text-fg-2', className)}>
      <span className={cn('mt-0.5 flex-none', tone === 'neutral' ? 'text-fg-3' : 'text-tint')}>
        {icon ?? (tone === 'orange' || tone === 'red' ? <AlertTriangle className="h-4 w-4" /> : <Info className="h-4 w-4" />)}
      </span>
      <div className="min-w-0 flex-1">
        {title && <p className={cn('font-medium', tone === 'neutral' ? 'text-fg' : 'text-tint')}>{title}</p>}
        {children && <div className="text-fg-2">{children}</div>}
      </div>
      {action && <div className="flex-none">{action}</div>}
    </div>
  );
}

export function SectionHead({ title, description, icon, actions, tint = 'pink', className }: {
  title: ReactNode; description?: ReactNode; icon?: ReactNode; actions?: ReactNode; tint?: Tint; className?: string;
}) {
  return (
    <div className={cn('mb-4 flex flex-wrap items-start justify-between gap-3', className)}>
      <div className="flex min-w-0 items-start gap-3">
        {icon && (
          <div className={cn('mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint', `tint-${tint}`)}>
            {icon}
          </div>
        )}
        <div className="min-w-0">
          <h2 className="text-[15px] font-semibold leading-6 text-fg">{title}</h2>
          {description && <p className="mt-0.5 text-[13px] leading-5 text-fg-3">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Pager({ page, totalPages, onChange, disabled, label }: {
  page: number; totalPages: number; onChange: (page: number) => void; disabled?: boolean; label?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 text-[12px] text-fg-3">
      {label ?? <span />}
      <div className="flex items-center gap-1.5">
        <IconButton label="上一页" size="sm" disabled={disabled || page <= 1} onClick={() => onChange(page - 1)}>
          <ChevronLeft className="h-4 w-4" />
        </IconButton>
        <span className="num px-1 font-mono text-fg-2">{page} / {Math.max(1, totalPages)}</span>
        <IconButton label="下一页" size="sm" disabled={disabled || page >= totalPages} onClick={() => onChange(page + 1)}>
          <ChevronRight className="h-4 w-4" />
        </IconButton>
      </div>
    </div>
  );
}

export function CopyValue({ value, label = '复制', className }: { value: string; label?: string; className?: string }) {
  const [done, setDone] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(value).then(() => {
      setDone(true);
      toast.success('已复制到剪贴板', value);
      window.setTimeout(() => setDone(false), 1600);
    }).catch(() => toast.error('复制失败', '浏览器未授予剪贴板权限'));
  };
  return (
    <Button size="sm" variant="ghost" className={cn('!h-6 !px-1.5 text-[11px]', className)} onClick={copy} icon={done ? <Check className="h-3 w-3 text-green" /> : <Copy className="h-3 w-3" />}>
      {done ? '已复制' : label}
    </Button>
  );
}

export function EmptyCard({ children }: { children: ReactNode }) {
  return <div className="rounded-xl border border-dashed border-[var(--glass-border-strong)] px-5 py-10 text-center text-[13px] text-fg-3">{children}</div>;
}

export function RoleBadge({ role }: { role: string | null }) {
  if (role === 'owner') return <Badge tone="orange">群主</Badge>;
  if (role === 'admin') return <Badge tone="purple">管理员</Badge>;
  if (role === 'member') return <Badge>成员</Badge>;
  return <Badge>{role ? roleLabels[role] || role : '角色未知'}</Badge>;
}

export function ProgressBar({ value, tone = 'pink', className }: { value: number; tone?: Tint; className?: string }) {
  return (
    <div className={cn('h-1.5 overflow-hidden rounded-full bg-glass-hover', `tint-${tone}`, className)}>
      <div className="h-full rounded-full bg-[linear-gradient(90deg,rgb(var(--tint)/0.45),rgb(var(--tint)))] transition-[width] duration-500" style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}

export function FieldBlock({ label, hint, children, className }: {
  label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <label className={cn('block text-[13px] font-medium text-fg-2', className)}>
      <span className="mb-1.5 block">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs font-normal text-fg-3">{hint}</span>}
    </label>
  );
}

export function StatTile({ label, value, hint, children, tone = 'pink', className }: {
  label: ReactNode; value?: ReactNode; hint?: ReactNode; children?: ReactNode; tone?: Tint; className?: string;
}) {
  return (
    <div className={cn('rounded-xl border border-line bg-glass px-3 py-2', `tint-${tone}`, className)}>
      <div className="text-[10.5px] text-fg-4">{label}</div>
      {value !== undefined && <div className="num truncate text-[15px] font-semibold text-fg">{value}</div>}
      {children}
      {hint && <div className="truncate text-[10.5px] text-fg-4">{hint}</div>}
    </div>
  );
}
