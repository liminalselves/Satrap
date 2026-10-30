import { HTMLAttributes, forwardRef, useCallback } from 'react';
import { cn } from '@/utils/cn';
import { useGlassReflect } from '@/hooks/useGlassReflect';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  variant?: 'default' | 'accent' | 'purple' | 'teal' | 'pink' | 'orange' | 'green' | 'success' | 'warning' | 'error';
  interactive?: boolean;
  /** 流光边框, 用于强调运行中等活跃状态 */
  flowing?: boolean;
}

const variantClass = {
  default: 'glass-card',
  accent: 'glass-card glass-card-accent tint-accent',
  purple: 'glass-card glass-card-purple tint-purple',
  teal: 'glass-card glass-card-teal tint-teal',
  pink: 'glass-card glass-card-pink tint-pink',
  orange: 'glass-card glass-card-orange tint-orange',
  green: 'glass-card glass-card-green tint-green',
  success: 'glass-card glass-card-success tint-green',
  warning: 'glass-card glass-card-warning tint-orange',
  error: 'glass-card glass-card-error tint-red',
} as const;

const Card = forwardRef<HTMLDivElement, CardProps>(
  ({ className, variant = 'default', interactive = false, flowing = false, children, ...props }, forwardedRef) => {
    const reflectRef = useGlassReflect<HTMLDivElement>({
      reflectRange: 150,
    });

    // 合并 refs
    const setRefs = useCallback((element: HTMLDivElement | null) => {
      reflectRef(element);
      if (typeof forwardedRef === 'function') {
        forwardedRef(element);
      } else if (forwardedRef) {
        forwardedRef.current = element;
      }
    }, [forwardedRef, reflectRef]);

    return (
      <div
        ref={setRefs}
        className={cn(
          variantClass[variant],
          // 只有可交互磁片才上浮, 结构面板保持静止
          interactive ? 'glass-card-interactive cursor-pointer' : 'glass-card-static',
          flowing && 'flow-border',
          'p-6',
          className
        )}
        {...props}
      >
        {children}
        {flowing && <span className="flow-ring" aria-hidden />}
      </div>
    );
  }
);

Card.displayName = 'Card';

export { Card };
