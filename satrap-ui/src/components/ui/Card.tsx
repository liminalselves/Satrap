import { HTMLAttributes, forwardRef } from 'react';
import { cn } from '@/utils/cn';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  variant?: 'default' | 'accent' | 'purple' | 'teal' | 'pink' | 'success' | 'warning' | 'error';
  interactive?: boolean;
}

const Card = forwardRef<HTMLDivElement, CardProps>(
  ({ className, variant = 'default', interactive = false, children, ...props }, ref) => {
    const variantClass = {
      default: 'glass-card',
      accent: 'glass-card glass-card-accent',
      purple: 'glass-card glass-card-purple',
      teal: 'glass-card glass-card-teal',
      pink: 'glass-card glass-card-pink',
      success: 'glass-card glass-card-success',
      warning: 'glass-card glass-card-warning',
      error: 'glass-card glass-card-error',
    }[variant];

    return (
      <div
        ref={ref}
        className={cn(
          variantClass,
          'p-6',
          interactive && 'cursor-pointer',
          className
        )}
        {...props}
      >
        {children}
      </div>
    );
  }
);

Card.displayName = 'Card';

export { Card };
