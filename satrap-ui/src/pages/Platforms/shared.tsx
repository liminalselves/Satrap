import { useState, type ReactNode } from 'react';
import {
  AlertTriangle, Boxes, Check, ChevronLeft, ChevronRight, Copy, Gamepad2, Globe, Info, MessageCircle,
  Network, Radio, Send,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, IconButton, type Tone, type Tint } from '@/components/kit';
import { toast } from '@/components/ui/Toast';

/** 平台类型图标: 与预览保持一致, 未知类型回退到通用方块 */
export const platformGlyph = (type: string) => {
  switch (type) {
    case 'onebot':
    case 'aiocqhttp': return MessageCircle;
    case 'discord': return Gamepad2;
    case 'telegram': return Send;
    case 'websocket': return Globe;
    case 'kook': return Radio;
    case 'matrix': return Network;
    default: return Boxes;
  }
};

export function PlatformIcon({ type, size = 'md', className }: { type: string; size?: 'sm' | 'md' | 'lg'; className?: string }) {
  const Icon = platformGlyph(type);
  const box = { sm: 'h-8 w-8 rounded-lg', md: 'h-10 w-10 rounded-xl', lg: 'h-12 w-12 rounded-2xl' }[size];
  const icon = { sm: 'h-4 w-4', md: 'h-[18px] w-[18px]', lg: 'h-6 w-6' }[size];
  return (
    <span className={cn('relative grid flex-none place-items-center border border-[rgb(var(--tint)/0.3)] bg-[linear-gradient(135deg,rgb(var(--tint)/0.28),rgb(var(--tint)/0.07))] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)] tint-pink', box, className)}>
      <Icon className={icon} />
    </span>
  );
}

const avatarSize = {
  sm: 'h-8 w-8 text-[12px] rounded-lg',
  md: 'h-10 w-10 text-[14px] rounded-xl',
  lg: 'h-12 w-12 text-[16px] rounded-2xl',
  xl: 'h-16 w-16 text-[22px] rounded-[18px]',
};

const hueOf = (text: string) => {
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) hash = (hash * 31 + text.charCodeAt(index)) % 360;
  return hash;
};

/** 账号/好友头像: 有平台头像地址时展示图片, 否则用名称首字生成稳定渐变 */
export function Avatar({ name, image, size = 'md', online, ring, className, square }: {
  name: string; image?: string | null; size?: keyof typeof avatarSize; online?: boolean; ring?: boolean; className?: string; square?: boolean;
}) {
  const text = (name || '?').trim().slice(0, 2).toUpperCase();
  const hue = hueOf(name || '?');
  const [failed, setFailed] = useState(false);
  return (
    <span className={cn('relative inline-grid flex-none place-items-center overflow-hidden font-semibold text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.3),0_6px_18px_-8px_rgba(0,0,0,0.7)]', avatarSize[size], square && '!rounded-lg', ring && 'ring-2 ring-[rgb(var(--tint)/0.35)] ring-offset-2 ring-offset-[var(--bg-base)]', className)}
      style={{ background: `linear-gradient(140deg, hsl(${hue} 68% 56%), hsl(${(hue + 26) % 360} 58% 38%))` }}>
      {image && !failed ? <img src={image} alt="" className="h-full w-full object-cover" onError={() => setFailed(true)} /> : text}
      {online !== undefined && (
        <span aria-label={online ? '在线' : '离线'} className={cn('absolute bottom-0 right-0 h-3 w-3 rounded-full border-2 border-[var(--bg-base)]', online ? 'bg-green' : 'bg-fg-4')} />
      )}
    </span>
  );
}

/** 带色调的说明条: 用于覆盖不完整、保存不等于生效一类的诚实提示 */
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

export function KeyValue({ label, children, mono, className }: { label: ReactNode; children: ReactNode; mono?: boolean; className?: string }) {
  return (
    <div className={cn('flex items-start justify-between gap-4 py-2', className)}>
      <span className="flex-none text-[12.5px] text-fg-3">{label}</span>
      <span className={cn('min-w-0 break-all text-right text-[13px] text-fg-2', mono && 'font-mono text-[12px]')}>{children}</span>
    </div>
  );
}

export function SectionHead({ title, description, icon, actions, tint, className }: {
  title: ReactNode; description?: ReactNode; icon?: ReactNode; actions?: ReactNode; tint?: Tint; className?: string;
}) {
  return (
    <div className={cn('mb-4 flex flex-wrap items-start justify-between gap-3', className)}>
      <div className="flex min-w-0 items-start gap-3">
        {icon && (
          <div className={cn('mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint', tint ? `tint-${tint}` : 'tint-pink')}>
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

export function Metric({ label, value, hint, icon, tint = 'pink', className }: {
  label: ReactNode; value: ReactNode; hint?: ReactNode; icon?: ReactNode; tint?: Tint; className?: string;
}) {
  return (
    <div className={cn('flex items-center gap-3 rounded-xl border border-line bg-glass px-3.5 py-2.5', `tint-${tint}`, className)}>
      {icon && <span className="grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint">{icon}</span>}
      <div className="min-w-0">
        <div className="text-[11px] text-fg-4">{label}</div>
        <div className="truncate text-[14px] font-semibold text-fg">{value}</div>
        {hint && <div className="truncate text-[11px] text-fg-3">{hint}</div>}
      </div>
    </div>
  );
}

/** 上一页/下一页: 与预览的分页器同构, 用作游标分页与页码分页的统一控件 */
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
      toast('success', `已复制到剪贴板: ${value}`);
      window.setTimeout(() => setDone(false), 1600);
    }).catch(() => toast('error', '复制失败: 浏览器未授予剪贴板权限'));
  };
  return (
    <Button size="sm" variant="ghost" className={cn('!h-6 !px-1.5 text-[11px]', className)} onClick={copy} icon={done ? <Check className="h-3 w-3 text-green" /> : <Copy className="h-3 w-3" />}>
      {done ? '已复制' : label}
    </Button>
  );
}

export type UiTone = Tone;

export function StateBadge({ tone, children, dot, live }: { tone: UiTone; children: ReactNode; dot?: boolean; live?: boolean }) {
  return <Badge tone={tone} dot={dot} live={live}>{children}</Badge>;
}

export function EmptyCard({ children }: { children: ReactNode }) {
  return <div className="rounded-xl border border-dashed border-[var(--glass-border-strong)] px-5 py-10 text-center text-[13px] text-fg-3">{children}</div>;
}
