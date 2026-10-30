import { cn } from '@/utils/cn';

export function Switch({ checked, onChange, label, disabled, size = 'md' }: {
  checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean; size?: 'sm' | 'md';
}) {
  const w = size === 'sm' ? 'w-8 h-[18px]' : 'w-10 h-[22px]';
  const k = size === 'sm' ? 'h-3 w-3' : 'h-4 w-4';
  const shift = size === 'sm' ? 'translate-x-[14px]' : 'translate-x-[18px]';
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'group relative inline-flex flex-none items-center rounded-full border px-[2px] transition-all duration-300 disabled:opacity-40',
        w,
        checked
          ? 'border-[rgb(var(--rgb-accent))] bg-[rgb(var(--rgb-accent))] shadow-[0_0_16px_-2px_rgb(var(--rgb-accent)/0.7)]'
          : 'border-[var(--glass-border-strong)] bg-glass-hover',
      )}
    >
      <span
        className={cn(
          'rounded-full shadow-sm transition-all duration-300 [transition-timing-function:var(--ease-spring)] group-active:scale-x-125',
          k,
          checked ? cn(shift, 'bg-white') : 'translate-x-0 bg-fg-3',
        )}
      />
    </button>
  );
}
