import { forwardRef, useEffect, useRef, useState, useCallback, useLayoutEffect, type MutableRefObject } from 'react';
import { createPortal } from 'react-dom';
import { ChevronDown, Check } from 'lucide-react';
import { cn } from '@/utils/cn';
import { useStandaloneGlassReflect } from '@/hooks/useGlassReflect';

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectProps {
  error?: boolean;
  options: SelectOption[];
  value?: string;
  onChange?: (e: { target: { value: string } }) => void;
  className?: string;
  disabled?: boolean;
  placeholder?: string;
  // 选项文字对齐, 默认右对齐(聊天页风格)
  align?: 'left' | 'right';
}

interface ListPos {
  top?: number;
  bottom?: number;
  left: number;
  width: number;
}

const LIST_MAX_HEIGHT = 240;   // 限制列表最大高度为 max-h-60
const GAP = 6;   // 与触发按钮间距

// 自定义下拉: 原生 select 弹出层由 OS 渲染无法自定义; 用 button + Portal 弹出列表实现
// 弹出层渲染到 body 并用 fixed 定位, 脱离玻璃卡片 backdrop-filter 产生的 containing block, 避免被遮挡
const Select = forwardRef<HTMLDivElement, SelectProps>(
  ({ className, error, options, value, onChange, disabled, placeholder = '请选择', align = 'right' }, forwardedRef) => {
    const [open, setOpen] = useState(false);
    const [pos, setPos] = useState<ListPos | null>(null);
    const rootRef = useRef<HTMLDivElement | null>(null);
    const triggerRef = useRef<HTMLButtonElement | null>(null);
    const listRef = useStandaloneGlassReflect<HTMLDivElement>({
      reflectRange: 100,
      reflectSize: 100,
    });

    // 合并根 ref
    const setRootRef = useCallback((el: HTMLDivElement | null) => {
      rootRef.current = el;
      if (typeof forwardedRef === 'function') {
        forwardedRef(el);
      } else if (forwardedRef) {
        (forwardedRef as MutableRefObject<HTMLDivElement | null>).current = el;
      }
    }, [forwardedRef]);

    const selected = options.find((o) => o.value === value);

    const close = useCallback(() => setOpen(false), []);

    // 计算弹出位置: 默认向下, 下方空间不足则向上; 右对齐触发按钮
    const updatePos = useCallback(() => {
      const trigger = triggerRef.current;
      if (!trigger) return;
      const rect = trigger.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      const openUp = spaceBelow < LIST_MAX_HEIGHT + GAP && spaceAbove > spaceBelow;
      setPos({
        left: rect.left,
        width: rect.width,
        ...(openUp
          ? { bottom: window.innerHeight - rect.top + GAP }
          : { top: rect.bottom + GAP }),
      });
    }, []);

    useLayoutEffect(() => {
      if (open) updatePos();
    }, [open, updatePos]);

    // 滚动 / 缩放时重定位
    useEffect(() => {
      if (!open) return;
      window.addEventListener('scroll', updatePos, true);
      window.addEventListener('resize', updatePos);
      return () => {
        window.removeEventListener('scroll', updatePos, true);
        window.removeEventListener('resize', updatePos);
      };
    }, [open, updatePos]);

    // 点击外部 / Escape 关闭
    useEffect(() => {
      if (!open) return;
      const onDocClick = (e: MouseEvent) => {
        const target = e.target as Node;
        if (rootRef.current?.contains(target)) return;
        if (listRef.current?.contains(target)) return;
        close();
      };
      const onEsc = (e: KeyboardEvent) => {
        if (e.key === 'Escape') close();
      };
      document.addEventListener('mousedown', onDocClick);
      document.addEventListener('keydown', onEsc);
      return () => {
        document.removeEventListener('mousedown', onDocClick);
        document.removeEventListener('keydown', onEsc);
      };
    }, [open, close, listRef]);

    const handleSelect = useCallback(
      (val: string) => {
        onChange?.({ target: { value: val } });
        close();
      },
      [onChange, close]
    );

    const dropdown = open && pos ? (
      <div
        ref={listRef}
        style={{
          position: 'fixed',
          left: pos.left,
          width: pos.width,
          ...(pos.top !== undefined ? { top: pos.top } : { bottom: pos.bottom }),
        }}
        className="glass-card glass-card-accent z-[100] rounded-lg p-1.5 shadow-glass animate-fade-in"
      >
        <div className="max-h-60 overflow-y-auto custom-scrollbar space-y-0.5">
          {options.length === 0 ? (
            <div className="px-3 py-2 text-sm text-text-tertiary text-center">暂无选项</div>
          ) : (
            options.map((opt) => {
              const active = opt.value === value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => handleSelect(opt.value)}
                  className={cn(
                    'w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm transition-colors text-left',
                    active
                      ? 'bg-accent-subtle text-accent'
                      : 'text-text-secondary hover:bg-glass-hover'
                  )}
                >
                  <span className="flex-1 truncate">{opt.label}</span>
                  {active && <Check className="h-3.5 w-3.5 shrink-0" />}
                </button>
              );
            })
          )}
        </div>
      </div>
    ) : null;

    return (
      <div ref={setRootRef} className={cn('relative', className)}>
        {/* 触发按钮 */}
        <button
          ref={triggerRef}
          type="button"
          disabled={disabled}
          onClick={() => setOpen((v) => !v)}
          className={cn(
            'glass-input w-full appearance-none pr-9 cursor-pointer flex items-center justify-between gap-2',
            align === 'right' ? 'text-right' : 'text-left',
            error && 'border-error focus:border-error focus:ring-error',
            disabled && 'opacity-50 cursor-not-allowed'
          )}
        >
          <span className={cn('truncate', !selected && 'text-text-tertiary')}>
            {selected ? selected.label : placeholder}
          </span>
          <ChevronDown
            className={cn(
              'absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary pointer-events-none transition-transform',
              open && 'rotate-180'
            )}
          />
        </button>

        {/* 弹出列表经 Portal 渲染到 body, 避免被玻璃卡片 containing block 遮挡 */}
        {dropdown && createPortal(dropdown, document.body)}
      </div>
    );
  }
);

Select.displayName = 'Select';

export { Select };
