import { ButtonHTMLAttributes, forwardRef, useCallback } from 'react';
import { cn } from '@/utils/cn';
import { useGlassReflect } from '@/hooks/useGlassReflect';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'default' | 'primary' | 'subtle' | 'ghost' | 'danger';
  size?: 'sm' | 'md' | 'lg';
  // 是否启用鼠标跟随玻璃反光, 默认启用
  reflect?: boolean;
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'default', size = 'md', reflect = true, children, ...props }, forwardedRef) => {
    const reflectRef = useGlassReflect<HTMLButtonElement>({
      reflectRange: 120,
      enabled: reflect,
    });

    // 合并反光 ref 与外部 ref
    const setRefs = useCallback((element: HTMLButtonElement | null) => {
      reflectRef(element);
      if (typeof forwardedRef === 'function') {
        forwardedRef(element);
      } else if (forwardedRef) {
        forwardedRef.current = element;
      }
    }, [forwardedRef, reflectRef]);

    const sizeClasses = { sm: 'ctl-sm', md: '', lg: 'ctl-lg' };

    // 沿用新组件库 .ctl 外观, 保留旧调用方的 variant 名称
    const variantClasses = {
      default: 'ctl',
      primary: 'ctl ctl-primary',
      subtle: 'ctl ctl-ghost',
      ghost: 'ctl ctl-ghost',
      danger: 'ctl ctl-danger',
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
