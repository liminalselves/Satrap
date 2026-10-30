import { useEffect, useRef, useState, type ReactNode } from 'react';
import { cn } from '@/utils/cn';
import type { Tint } from './tints';

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('skeleton h-4', className)} aria-hidden />;
}

/** 空状态: 带插画光晕和下一步操作 */
export function EmptyState({ icon, title, description, actions, tint = 'accent', className }: {
  icon: ReactNode; title: string; description?: ReactNode; actions?: ReactNode; tint?: Tint; className?: string;
}) {
  return (
    <div className={cn('flex flex-col items-center px-6 py-12 text-center', `tint-${tint}`, className)}>
      <div className="relative mb-5">
        <div className="absolute inset-0 -m-6 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.28),transparent_70%)] blur-md" />
        <div className="relative grid h-14 w-14 place-items-center rounded-2xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.12)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">
          {icon}
        </div>
      </div>
      <h3 className="text-base font-semibold text-fg">{title}</h3>
      {description && <p className="mt-1.5 max-w-sm text-[13px] leading-5 text-fg-3">{description}</p>}
      {actions && <div className="mt-5 flex flex-wrap items-center justify-center gap-2">{actions}</div>}
    </div>
  );
}

/** 数字缓动计数 */
export function CountUp({ value, duration = 900, format = (n: number) => n.toLocaleString() }: {
  value: number; duration?: number; format?: (n: number) => string;
}) {
  const [shown, setShown] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const step = (t: number) => {
      const p = Math.min(1, (t - start) / duration);
      const eased = 1 - Math.pow(1 - p, 4);
      const v = a + (value - a) * eased;
      setShown(v);
      if (p < 1) raf = requestAnimationFrame(step);
      else from.current = value;
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);
  return <span className="num">{format(Math.round(shown))}</span>;
}

/** 迷你折线 */
export function Sparkline({ data, className, fill = true }: { data: number[]; className?: string; fill?: boolean }) {
  const w = 120;
  const h = 36;
  const max = Math.max(...data);
  const min = Math.min(...data);
  const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - 3 - ((v - min) / (max - min || 1)) * (h - 6)]);
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const id = useRef(`sg${Math.random().toString(36).slice(2)}`).current;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className={cn('h-9 w-full overflow-visible', className)} aria-hidden>
      <defs>
        <linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="rgb(var(--tint))" stopOpacity="0.35" />
          <stop offset="1" stopColor="rgb(var(--tint))" stopOpacity="0" />
        </linearGradient>
      </defs>
      {fill && <path d={`${d} L${w},${h} L0,${h} Z`} fill={`url(#${id})`} />}
      <path d={d} fill="none" stroke="var(--tint-fg)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" className="sparkline-path" />
    </svg>
  );
}

export function PageHeader({ title, description, actions, eyebrow }: {
  title: string; description?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 animate-rise">
      <div className="min-w-0">
        {eyebrow && <div className="mb-1.5">{eyebrow}</div>}
        <h1 className="display text-[26px] font-semibold leading-tight text-fg">{title}</h1>
        {description && <p className="mt-1 text-sm text-fg-3">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
