import { useState, type CSSProperties, type InputHTMLAttributes, type ReactNode } from 'react';
import { AlertCircle, AlertTriangle, CheckCircle2, Eye, EyeOff, Info, Trash2 } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Button, Card, Input, Modal, type Tint } from '@/components/kit';

/** 设置分节外壳: 与预览一致的分节卡片、图标底色与入场上浮 */
export function SectionCard({ id, tint, icon, title, description, actions, children, className, flowing, i }: {
  id: string;
  tint: Tint;
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  flowing?: boolean;
  i: number;
}) {
  return (
    <Card id={`sec-${id}`} padding="lg" tint={tint} flowing={flowing}
      className={cn('scroll-mt-4', className)} style={{ '--i': i } as CSSProperties}>
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          {icon && <div className="mt-0.5 grid h-9 w-9 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.14)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">{icon}</div>}
          <div className="min-w-0">
            <h2 className="text-[16px] font-semibold leading-6 text-fg">{title}</h2>
            {description && <p className="mt-0.5 text-[13px] leading-5 text-fg-3">{description}</p>}
          </div>
        </div>
        {actions && <div className="flex w-full flex-wrap items-center gap-2 sm:ml-auto sm:w-auto sm:max-w-full">{actions}</div>}
      </div>
      {children}
    </Card>
  );
}

const bannerMeta = {
  info: { tint: 'accent', icon: Info },
  warning: { tint: 'orange', icon: AlertTriangle },
  error: { tint: 'red', icon: AlertCircle },
  success: { tint: 'green', icon: CheckCircle2 },
} as const;

/** 状态横幅: error 使用 alert 语义, 其余为 status */
export function Banner({ tone = 'info', title, children, actions, className, compact }: {
  tone?: keyof typeof bannerMeta;
  title?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  className?: string;
  compact?: boolean;
}) {
  const meta = bannerMeta[tone];
  const Icon = meta.icon;
  return (
    <div role={tone === 'error' ? 'alert' : 'status'}
      className={cn(`tint-${meta.tint}`, 'flex flex-wrap items-start gap-3 rounded-xl border border-[rgb(var(--tint)/0.28)] bg-[rgb(var(--tint)/0.08)]',
        compact ? 'px-3 py-2' : 'p-3.5', className)}>
      <Icon className={cn('mt-0.5 h-4 w-4 flex-none text-tint', !compact && 'h-[18px] w-[18px]')} />
      <div className="min-w-0 flex-1 text-[13px] leading-5 text-fg-2">
        {title && <p className="font-medium text-fg">{title}</p>}
        {children && <div className={cn(title && 'mt-1', 'text-[13px] leading-5 text-fg-2')}>{children}</div>}
      </div>
      {actions && <div className="flex max-w-full flex-wrap items-center gap-2 sm:ml-auto">{actions}</div>}
    </div>
  );
}

export function ConfirmDialog({ open, onClose, onConfirm, title, description, confirmLabel = '确认', danger, loading, children }: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  loading?: boolean;
  children?: ReactNode;
}) {
  return (
    <Modal open={open} onClose={onClose} size="sm" title={title} description={description}
      icon={danger ? <Trash2 className="h-[18px] w-[18px]" /> : <Info className="h-[18px] w-[18px]" />}
      className={danger ? 'tint-red' : 'tint-orange'}
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={loading}>取消</Button>
        <Button variant={danger ? 'danger' : 'primary'} loading={loading} onClick={onConfirm}>{confirmLabel}</Button>
      </>}>
      {children ?? <p className="text-sm leading-6 text-fg-2">请确认此操作。</p>}
    </Modal>
  );
}

/** 带显示/隐藏切换的密码框, aria 标签沿用页面字段名 */
export function PasswordInput(props: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="relative">
      <Input {...props} type={visible ? 'text' : 'password'} className={cn('pr-10', props.className)} />
      <button type="button" onClick={() => setVisible((v) => !v)}
        aria-label={visible ? '隐藏密码' : '显示密码'} aria-pressed={visible}
        className="absolute right-1.5 top-1/2 grid h-8 w-8 -translate-y-1/2 place-items-center rounded-lg text-fg-3 transition hover:bg-glass-hover hover:text-fg">
        {visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
      </button>
    </div>
  );
}

export function KeyValue({ label, children, mono, className }: { label: ReactNode; children: ReactNode; mono?: boolean; className?: string }) {
  return (
    <div className={cn('flex items-baseline justify-between gap-4 py-2 text-[13px]', className)}>
      <span className="flex-none text-fg-3">{label}</span>
      <span className={cn('min-w-0 truncate text-right text-fg-2', mono && 'font-mono text-[12px]')}>{children}</span>
    </div>
  );
}

export function FieldGroup({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('grid gap-4 sm:grid-cols-2', className)}>{children}</div>;
}

export function InlineSwitchRow({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-6 py-3">
      <div className="min-w-0">
        <p className="text-sm font-medium text-fg">{label}</p>
        {hint && <p className="mt-0.5 text-[13px] leading-5 text-fg-3">{hint}</p>}
      </div>
      <div className="flex-none">{children}</div>
    </div>
  );
}
