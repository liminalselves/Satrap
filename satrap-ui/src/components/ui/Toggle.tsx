import { cn } from '@/utils/cn';

// 开关滑块: 替代原生 checkbox 的统一启用/停用控件, 旋钮弹性回位
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
        'group relative inline-flex h-[22px] w-10 shrink-0 items-center rounded-full border px-[2px] transition-all duration-300 disabled:cursor-not-allowed disabled:opacity-40',
        checked
          ? 'border-accent bg-accent shadow-[0_0_16px_-2px_rgb(var(--rgb-accent)/0.7)]'
          : 'border-[var(--glass-border-strong)] bg-glass-active'
      )}
      title={title}
    >
      <span
        className={cn(
          'h-4 w-4 rounded-full shadow-sm transition-all duration-300 [transition-timing-function:var(--ease-spring)] group-active:scale-x-125',
          checked ? 'translate-x-[18px] bg-white' : 'translate-x-0 bg-text-tertiary'
        )}
      />
    </button>
  );
}
