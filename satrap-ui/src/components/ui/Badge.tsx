import { HTMLAttributes, forwardRef } from 'react';
import { cn } from '@/utils/cn';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: 'default' | 'success' | 'warning' | 'error' | 'info' | 'accent' | 'purple' | 'teal' | 'pink';
}

const Badge = forwardRef<HTMLSpanElement, BadgeProps>(
  ({ className, variant = 'default', children, ...props }, ref) => {
    // 统一为染色胶囊: 背景/边框/文字都由 tint 派生
    const variantClasses = {
      default: 'chip',
      success: 'chip tint-green',
      warning: 'chip tint-orange',
      error: 'chip tint-red',
      info: 'chip tint-accent',
      accent: 'chip tint-accent',
      purple: 'chip tint-purple',
      teal: 'chip tint-teal',
      pink: 'chip tint-pink',
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
