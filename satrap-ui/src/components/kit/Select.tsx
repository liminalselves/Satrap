import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown } from 'lucide-react';
import { cn } from '@/utils/cn';

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
  icon?: ReactNode;
}

/** 玻璃下拉: 替代原生 select, 支持键盘导航与说明文本 */
export function Select({ value, onChange, options, placeholder = '请选择', className, id, ...aria }: {
  value: string; onChange: (v: string) => void; options: SelectOption[]; placeholder?: string; className?: string;
  id?: string; 'aria-invalid'?: boolean; 'aria-describedby'?: string; 'aria-label'?: string;
}) {
  const listId = useId();
  const btnRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const current = options.find((o) => o.value === value);

  useLayoutEffect(() => {
    if (open && btnRef.current) setRect(btnRef.current.getBoundingClientRect());
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => {
      if (!(e.target as HTMLElement).closest(`[data-select="${listId}"]`)) setOpen(false);
    };
    const onScroll = () => setOpen(false);
    window.addEventListener('pointerdown', close);
    window.addEventListener('resize', onScroll);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('resize', onScroll); };
  }, [open, listId]);

  const openList = () => {
    setActive(Math.max(0, options.findIndex((o) => o.value === value)));
    setOpen(true);
  };
  const commit = (i: number) => { onChange(options[i].value); setOpen(false); btnRef.current?.focus(); };

  const onKeyDown = (e: KeyboardEvent) => {
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); openList(); }
      return;
    }
    if (e.key === 'Escape') { e.preventDefault(); setOpen(false); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => (a + 1) % options.length); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => (a - 1 + options.length) % options.length); }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); commit(active); }
    else if (e.key === 'Tab') setOpen(false);
  };

  const below = rect ? window.innerHeight - rect.bottom > 280 : true;

  return (
    <>
      <button
        ref={btnRef}
        id={id}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open ? `${listId}-${active}` : undefined}
        data-select={listId}
        onClick={() => (open ? setOpen(false) : openList())}
        onKeyDown={onKeyDown}
        className={cn('field flex items-center justify-between gap-2 text-left', open && 'border-[rgb(var(--rgb-accent)/0.7)] shadow-[0_0_0_4px_rgb(var(--rgb-accent)/0.16)]', className)}
        {...aria}
      >
        <span className={cn('flex min-w-0 items-center gap-2 truncate', !current && 'text-fg-4')}>
          {current?.icon}
          {current?.label ?? placeholder}
        </span>
        <ChevronDown className={cn('h-4 w-4 flex-none text-fg-3 transition-transform duration-300', open && 'rotate-180')} />
      </button>
      {open && rect && createPortal(
        <div
          id={listId}
          role="listbox"
          data-select={listId}
          className="scroll fixed z-[150] max-h-64 rounded-xl border border-[var(--glass-border-strong)] p-1 shadow-[var(--shadow-3)] surface-strong backdrop-blur-2xl animate-pop"
          style={{
            left: rect.left,
            width: Math.max(rect.width, 200),
            ...(below ? { top: rect.bottom + 6 } : { bottom: window.innerHeight - rect.top + 6 }),
            transformOrigin: below ? 'top' : 'bottom',
          }}
        >
          {options.map((o, i) => {
            const selected = o.value === value;
            return (
              <div
                key={o.value}
                id={`${listId}-${i}`}
                role="option"
                aria-selected={selected}
                onPointerEnter={() => setActive(i)}
                onClick={() => commit(i)}
                className={cn(
                  'flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors duration-100',
                  i === active ? 'bg-glass-hover text-fg' : 'text-fg-2',
                )}
              >
                {o.icon && <span className="flex-none text-fg-3">{o.icon}</span>}
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{o.label}</span>
                  {o.description && <span className="block truncate text-xs text-fg-3">{o.description}</span>}
                </span>
                {selected && <Check className="h-4 w-4 flex-none text-accent" />}
              </div>
            );
          })}
        </div>,
        document.body,
      )}
    </>
  );
}
