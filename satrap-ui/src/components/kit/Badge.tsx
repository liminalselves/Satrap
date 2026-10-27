import type { ReactNode } from 'react';
import { cn } from '@/utils/cn';
import type { Tint } from './tints';

export type Tone = Tint | 'red' | 'neutral';

export function Badge({ tone = 'neutral', dot, live, children, className }: {
  tone?: Tone; dot?: boolean; live?: boolean; children: ReactNode; className?: string;
}) {
  return (
    <span className={cn('chip', tone !== 'neutral' && `tint-${tone}`, className)}>
      {dot && <span className="dot !h-1.5 !w-1.5" data-live={live ? '1' : undefined} />}
      {children}
    </span>
  );
}

export function StatusDot({ tone, live, className }: { tone: Tone; live?: boolean; className?: string }) {
  return <span className={cn('dot', tone !== 'neutral' && `tint-${tone}`, className)} data-live={live ? '1' : undefined} />;
}
