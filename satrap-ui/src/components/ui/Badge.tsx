import { HTMLAttributes, forwardRef } from 'react';
import { cn } from '@/utils/cn';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: 'default' | 'success' | 'warning' | 'error' | 'info' | 'accent' | 'purple' | 'teal' | 'pink';
}

const Badge = forwardRef<HTMLSpanElement, BadgeProps>(
  ({ className, variant = 'default', children, ...props }, ref) => {
    const variantClasses = {
      default: 'glass-badge bg-glass border border-glass-border text-text-secondary',
      success: 'glass-badge glass-badge-success',
      warning: 'glass-badge glass-badge-warning',
      error: 'glass-badge glass-badge-error',
      info: 'glass-badge glass-badge-info',
      accent: 'glass-badge bg-glass-accent border border-glass-accent-border text-accent-light',
      purple: 'glass-badge bg-glass-purple border border-glass-purple-border text-purple-light',
      teal: 'glass-badge bg-glass-teal border border-glass-teal-border text-teal-light',
      pink: 'glass-badge bg-glass-pink border border-glass-pink-border text-pink-light',
    };

    return (
      <span
        ref={ref}
        className={cn(variantClasses[variant], className)}
        {...props}
      >
        {children}
      </span>
    );
  }
);

Badge.displayName = 'Badge';

export { Badge };
