import { forwardRef, type HTMLAttributes, type ReactNode } from 'react';
import { cn } from '@/utils/cn';
import type { Tint } from './tints';

type CardTint = Tint | 'red';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  tint?: CardTint;
  /** 可交互磁片: 上浮 + 完整反光; 默认是静态结构面板 */
  interactive?: boolean;
  padding?: 'none' | 'sm' | 'md' | 'lg';
  /** 流光边框, 用于强调“运行中”一类状态 */
  flowing?: boolean;
}

const pad = { none: '', sm: 'p-4', md: 'p-5', lg: 'p-6' };

export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  { tint, interactive, padding = 'md', flowing, className, children, ...props }, ref,
) {
  return (
    <div
      ref={ref}
      tabIndex={interactive ? 0 : undefined}
      role={interactive && props.onClick ? 'button' : undefined}
      onKeyDown={interactive && props.onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); (e.currentTarget as HTMLElement).click(); } } : undefined}
      className={cn(interactive ? 'tile' : 'surface', tint && `tint-${tint}`, pad[padding], flowing && 'flow-border', className)}
      {...props}
    >
      {flowing && <span className="flow-ring" aria-hidden />}
      {children}
    </div>
  );
});

export function CardHeader({ title, description, icon, actions, className }: {
  title: ReactNode; description?: ReactNode; icon?: ReactNode; actions?: ReactNode; className?: string;
}) {
  return (
    <div className={cn('mb-4 flex items-start justify-between gap-4', className)}>
      <div className="flex min-w-0 items-start gap-3">
        {icon && <div className="mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint">{icon}</div>}
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold leading-6 text-fg">{title}</h3>
          {description && <p className="mt-0.5 text-[13px] text-fg-3">{description}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-none items-center gap-2">{actions}</div>}
    </div>
  );
}
