import { useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { cn } from '@/utils/cn';

export interface TabItem<T extends string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
  count?: number;
}

/** 玻璃分段控件: 选中胶囊在选项间滑动 */
export function Tabs<T extends string>({ items, value, onChange, size = 'md', className, label }: {
  items: TabItem<T>[]; value: T; onChange: (value: T) => void; size?: 'sm' | 'md'; className?: string; label: string;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const [indicator, setIndicator] = useState<{ left: number; width: number } | null>(null);

  useLayoutEffect(() => {
    const list = listRef.current;
    const el = list?.querySelector<HTMLElement>(`[data-value="${CSS.escape(value)}"]`);
    if (!list || !el) return;
    const update = () => setIndicator({ left: el.offsetLeft, width: el.offsetWidth });
    update();
    const ro = new ResizeObserver(update);
    ro.observe(list);
    return () => ro.disconnect();
  }, [value, items.length]);

  const onKeyDown = (e: KeyboardEvent) => {
    const i = items.findIndex((t) => t.value === value);
    let next = -1;
    if (e.key === 'ArrowRight') next = (i + 1) % items.length;
    if (e.key === 'ArrowLeft') next = (i - 1 + items.length) % items.length;
    if (e.key === 'Home') next = 0;
    if (e.key === 'End') next = items.length - 1;
    if (next < 0) return;
    e.preventDefault();
    onChange(items[next].value);
    listRef.current?.querySelector<HTMLElement>(`[data-value="${CSS.escape(items[next].value)}"]`)?.focus();
  };

  return (
    <div
      ref={listRef}
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn('relative inline-flex max-w-full items-center gap-0.5 overflow-x-auto rounded-[12px] border border-line bg-glass p-1 backdrop-blur-md [scrollbar-width:none] [&::-webkit-scrollbar]:hidden', className)}
    >
      {indicator && (
        <span
          aria-hidden
          className="absolute top-1 bottom-1 rounded-[9px] border border-[rgb(var(--rgb-accent)/0.28)] bg-[rgb(var(--rgb-accent)/0.14)] shadow-[0_4px_16px_-4px_rgb(var(--rgb-accent)/0.45),inset_0_1px_0_var(--glass-highlight)]"
          style={{ left: indicator.left, width: indicator.width, transition: 'left 380ms var(--ease-out), width 380ms var(--ease-out)' }}
        />
      )}
      {items.map((item) => {
        const selected = item.value === value;
        return (
          <button
            key={item.value}
            type="button"
            role="tab"
            data-value={item.value}
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(item.value)}
            className={cn(
              'relative z-[1] inline-flex shrink-0 items-center gap-2 whitespace-nowrap rounded-[9px] font-medium transition-colors duration-200',
              size === 'sm' ? 'h-7 px-2.5 text-[12.5px]' : 'h-8 px-3.5 text-sm',
              selected ? 'text-accent' : 'text-fg-3 hover:text-fg',
            )}
          >
            {item.icon}
            {item.label}
            {item.count !== undefined && (
              <span className={cn('num rounded-full px-1.5 text-[11px] leading-[18px]', selected ? 'bg-[rgb(var(--rgb-accent)/0.2)]' : 'bg-glass-hover text-fg-3')}>{item.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
