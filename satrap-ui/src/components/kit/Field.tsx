import { cloneElement, useId, type InputHTMLAttributes, type ReactElement, type ReactNode, type TextareaHTMLAttributes, forwardRef } from 'react';
import { AlertCircle } from 'lucide-react';
import { cn } from '@/utils/cn';

/** 统一的表单字段: 标签 / 说明 / 错误 / 必填, 自动关联 aria */
export function Field({ label, hint, error, required, children, className, inline }: {
  label: ReactNode; hint?: ReactNode; error?: string; required?: boolean;
  children: ReactElement; className?: string; inline?: boolean;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  const control = cloneElement(children, {
    id,
    'aria-invalid': error ? true : undefined,
    'aria-describedby': [hint && hintId, error && errId].filter(Boolean).join(' ') || undefined,
    'aria-required': required || undefined,
  });

  if (inline) {
    return (
      <div className={cn('flex items-center justify-between gap-6 py-3', className)}>
        <div className="min-w-0">
          <label htmlFor={id} className="text-sm font-medium text-fg">{label}</label>
          {hint && <p id={hintId} className="mt-0.5 text-[13px] text-fg-3">{hint}</p>}
        </div>
        <div className="flex-none">{control}</div>
      </div>
    );
  }

  return (
    <div className={cn('space-y-1.5', className)}>
      {/* 星号放在 label 之外, 可访问名称与标签文字完全一致 */}
      <div className="flex items-center gap-1 text-[13px] font-medium text-fg-2">
        <label htmlFor={id}>{label}</label>
        {required && <span className="text-red" aria-hidden>*</span>}
      </div>
      {control}
      {error ? (
        <p id={errId} className="flex items-center gap-1.5 text-xs text-red animate-pop"><AlertCircle className="h-3.5 w-3.5" />{error}</p>
      ) : hint ? (
        <p id={hintId} className="text-xs text-fg-3">{hint}</p>
      ) : null}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { leading?: ReactNode; trailing?: ReactNode }>(
  function Input({ className, leading, trailing, ...props }, ref) {
    if (!leading && !trailing) return <input ref={ref} className={cn('field', className)} {...props} />;
    return (
      <div className="relative">
        {leading && <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-fg-3">{leading}</span>}
        <input ref={ref} className={cn('field', leading && 'pl-9', trailing && 'pr-10', className)} {...props} />
        {trailing && <span className="absolute right-2 top-1/2 -translate-y-1/2">{trailing}</span>}
      </div>
    );
  },
);

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...props }, ref) {
    return <textarea ref={ref} className={cn('field', className)} {...props} />;
  },
);
