import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { Loader2 } from 'lucide-react';
import { cn } from '@/utils/cn';
import type { Tint } from './tints';
import { Tooltip } from './Tooltip';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'default' | 'primary' | 'tinted' | 'ghost' | 'danger';
  size?: 'sm' | 'md' | 'lg';
  tint?: Tint | 'red';
  icon?: ReactNode;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'default', size = 'md', tint, icon, loading, className, children, disabled, ...props }, ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(
        'ctl',
        variant !== 'default' && `ctl-${variant}`,
        size !== 'md' && `ctl-${size}`,
        tint && `tint-${tint}`,
        className,
      )}
      {...props}
    >
      {loading ? <Loader2 className="h-4 w-4 animate-spin-slow" /> : icon}
      {children}
    </button>
  );
});

export interface IconButtonProps extends Omit<ButtonProps, 'children' | 'icon'> {
  label: string;
  shortcut?: string;
  children: ReactNode;
  active?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, shortcut, children, className, variant = 'ghost', active, ...props }, ref,
) {
  return (
    <Tooltip content={label} shortcut={shortcut}>
      <Button
        ref={ref}
        aria-label={label}
        aria-pressed={active}
        variant={variant}
        className={cn('ctl-icon', active && 'text-accent bg-glass-hover', className)}
        {...props}
      >
        {children}
      </Button>
    </Tooltip>
  );
});
