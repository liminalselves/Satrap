import {
  forwardRef,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MutableRefObject,
} from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown } from 'lucide-react';
import { cn } from '@/utils/cn';

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps {
  options: SelectOption[];
  value?: string;
  onChange?: (event: { target: { value: string } }) => void;
  className?: string;
  disabled?: boolean;
  placeholder?: string;
  error?: boolean;
  align?: 'left' | 'right';
}

interface ListPosition {
  top?: number;
  bottom?: number;
  left: number;
  width: number;
}

const LIST_MAX_HEIGHT = 240;
const LIST_GAP = 6;

/**
 * 聊天页下拉选择: 与全局 Select 行为一致, 但弹层使用更高的 z-index.
 * 弹层层级需高于旧组件库 Modal (Z_BASE 125); 选项保持真实 button,
 * 供键盘操作与 e2e 以角色/名称定位.
 */
export const Select = forwardRef<HTMLDivElement, SelectProps>(function Select(
  { className, error, options, value, onChange, disabled, placeholder = '请选择', align = 'right' },
  forwardedRef,
) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<ListPosition | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const selected = options.find((option) => option.value === value);

  const setRootRef = useCallback((element: HTMLDivElement | null) => {
    rootRef.current = element;
    if (typeof forwardedRef === 'function') forwardedRef(element);
    else if (forwardedRef) (forwardedRef as MutableRefObject<HTMLDivElement | null>).current = element;
  }, [forwardedRef]);

  const close = useCallback(() => setOpen(false), []);

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top;
    const openUp = spaceBelow < LIST_MAX_HEIGHT + LIST_GAP && spaceAbove > spaceBelow;
    setPosition({
      left: rect.left,
      width: rect.width,
      ...(openUp
        ? { bottom: window.innerHeight - rect.top + LIST_GAP }
        : { top: rect.bottom + LIST_GAP }),
    });
  }, []);

  useLayoutEffect(() => {
    if (open) updatePosition();
  }, [open, updatePosition]);

  useEffect(() => {
    if (!open) return;
    const onScroll = () => updatePosition();
    const onResize = () => updatePosition();
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onResize);
    };
  }, [open, updatePosition]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (rootRef.current?.contains(target)) return;
      if (listRef.current?.contains(target)) return;
      close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, close]);

  const handleSelect = useCallback((next: string) => {
    onChange?.({ target: { value: next } });
    close();
    triggerRef.current?.focus();
  }, [onChange, close]);

  const dropdown = open && position ? (
    <div
      ref={listRef}
      style={{
        position: 'fixed',
        left: position.left,
        width: position.width,
        ...(position.top !== undefined ? { top: position.top } : { bottom: position.bottom }),
      }}
      className="surface surface-strong z-[150] !rounded-xl p-1 !shadow-[var(--shadow-3)] animate-pop"
    >
      <div className="scroll max-h-60 space-y-0.5">
        {options.length === 0 ? (
          <div className="px-3 py-2 text-center text-sm text-fg-4">暂无选项</div>
        ) : (
          options.map((option) => {
            const active = option.value === value;
            return (
              <button
                key={option.value}
                type="button"
                onClick={() => handleSelect(option.value)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm transition-colors duration-100',
                  active
                    ? 'bg-[rgb(var(--rgb-accent)/0.14)] text-accent'
                    : 'text-fg-2 hover:bg-glass-hover hover:text-fg',
                )}
              >
                <span className="flex-1 truncate">{option.label}</span>
                {active && <Check className="h-3.5 w-3.5 flex-none" />}
              </button>
            );
          })
        )}
      </div>
    </div>
  ) : null;

  return (
    <div ref={setRootRef} className={cn('relative', className)}>
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        className={cn(
          'field flex w-full items-center justify-between gap-2 pr-9',
          align === 'right' ? 'text-right' : 'text-left',
          open && 'border-[rgb(var(--rgb-accent)/0.7)] shadow-[0_0_0_4px_rgb(var(--rgb-accent)/0.16)]',
          error && 'border-[rgb(var(--rgb-red)/0.7)]',
          disabled && 'cursor-not-allowed opacity-50',
        )}
      >
        <span className={cn('truncate', !selected && 'text-fg-4')}>
          {selected ? selected.label : placeholder}
        </span>
        <ChevronDown
          className={cn(
            'pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-3 transition-transform duration-300',
            open && 'rotate-180',
          )}
        />
      </button>

      {dropdown && createPortal(dropdown, document.body)}
    </div>
  );
});
