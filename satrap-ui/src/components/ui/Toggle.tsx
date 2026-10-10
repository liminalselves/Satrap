import { cn } from '@/utils/cn';

// 开关滑块: 替代原生 checkbox 的统一启用/停用控件
export function Toggle({
  checked,
  onChange,
  title,
  disabled = false,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  title?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'w-9 h-5 rounded-full relative transition-colors shrink-0 border disabled:opacity-40 disabled:cursor-not-allowed',
        checked ? 'bg-accent border-accent' : 'bg-glass-active border-glass-border'
      )}
      title={title}
    >
      <span
        className={cn(
          'absolute top-1/2 -translate-y-1/2 left-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-transform',
          checked && 'translate-x-4'
        )}
      />
    </button>
  );
}
