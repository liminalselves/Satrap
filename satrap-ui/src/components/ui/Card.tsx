import { HTMLAttributes, forwardRef } from 'react';
import { cn } from '@/utils/cn';
import { useStandaloneGlassReflect } from '@/hooks/useGlassReflect';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  variant?: 'default' | 'accent' | 'purple' | 'teal' | 'pink' | 'orange' | 'green' | 'success' | 'warning' | 'error';
  interactive?: boolean;
}

const Card = forwardRef<HTMLDivElement, CardProps>(
  ({ className, variant = 'default', interactive = false, children, ...props }, forwardedRef) => {
    const reflectRef = useStandaloneGlassReflect<HTMLDivElement>({
      reflectRange: 150,
    });
    
    // 合并 refs
    const setRefs = (element: HTMLDivElement | null) => {
      // @ts-expect-error - 合并 refs
      reflectRef.current = element;
      if (typeof forwardedRef === 'function') {
        forwardedRef(element);
      } else if (forwardedRef) {
        forwardedRef.current = element;
      }
    };

    const variantClass = {
      default: 'glass-card',
      accent: 'glass-card glass-card-accent',
      purple: 'glass-card glass-card-purple',
      teal: 'glass-card glass-card-teal',
      pink: 'glass-card glass-card-pink',
      orange: 'glass-card glass-card-orange',
      green: 'glass-card glass-card-green',
      success: 'glass-card glass-card-success',
      warning: 'glass-card glass-card-warning',
      error: 'glass-card glass-card-error',
    }[variant];

    return (
      <div
        ref={setRefs}
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
