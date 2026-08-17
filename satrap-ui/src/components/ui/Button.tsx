import { ButtonHTMLAttributes, forwardRef } from 'react';
import { cn } from '@/utils/cn';
import { useStandaloneGlassReflect } from '@/hooks/useGlassReflect';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'default' | 'primary' | 'subtle' | 'ghost' | 'danger';
  size?: 'sm' | 'md' | 'lg';
  // 是否启用鼠标跟随玻璃反光, 默认启用
  reflect?: boolean;
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'default', size = 'md', reflect = true, children, ...props }, forwardedRef) => {
    const reflectRef = useStandaloneGlassReflect<HTMLButtonElement>({
      reflectRange: 120,
      reflectSize: 120,
    });

    // 合并反光 ref 与外部 ref
    const setRefs = (element: HTMLButtonElement | null) => {
      if (reflect) {
        // @ts-expect-error - 合并 refs
        reflectRef.current = element;
      }
      if (typeof forwardedRef === 'function') {
        forwardedRef(element);
      } else if (forwardedRef) {
        forwardedRef.current = element;
      }
    };

    const sizeClasses = {
      sm: 'px-3 py-1.5 text-sm',
      md: 'px-4 py-2 text-sm',
      lg: 'px-5 py-2.5 text-base',
    };

    const variantClasses = {
      default: 'glass-button',
      primary: 'glass-button glass-button-primary',
      subtle: 'glass-button bg-transparent border-transparent hover:bg-glass',
      ghost: 'glass-button bg-transparent border-transparent hover:bg-glass-hover',
      danger: 'glass-button bg-glass-error border-glass-error-border text-error hover:bg-error hover:text-white',
    };

    return (
      <button
        ref={setRefs}
        className={cn(
          variantClasses[variant],
          sizeClasses[size],
          className
        )}
        {...props}
      >
        {children}
      </button>
    );
  }
);

Button.displayName = 'Button';

export { Button };
