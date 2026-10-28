import {
  Children, Fragment, isValidElement, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
  type KeyboardEvent, type ReactElement, type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown } from 'lucide-react';
import { cn } from '@/utils/cn';

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
  icon?: ReactNode;
  disabled?: boolean;
}

type OptionProps = { value?: string | number; disabled?: boolean; children?: ReactNode };

function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return '';
}

/** 把 <option> 子元素 (可嵌在数组、Fragment、条件表达式里) 转成选项 */
function optionsFromChildren(children: ReactNode): SelectOption[] {
  const result: SelectOption[] = [];
  Children.forEach(children, (child) => {
    if (!isValidElement(child)) return;
    if (child.type === Fragment) {
      result.push(...optionsFromChildren((child as ReactElement<{ children?: ReactNode }>).props.children));
      return;
    }
    if (child.type !== 'option') return;
    const props = (child as ReactElement<OptionProps>).props;
    const label = textOf(props.children);
    result.push({ value: props.value === undefined ? label : String(props.value), label, disabled: props.disabled });
  });
  return result;
}

const LIST_GAP = 6;
const LIST_MAX = 288;
const EDGE = 8;

/**
 * 玻璃下拉: 替代原生 select, 原生弹层由系统绘制无法统一外观.
 * 选项可用 options 传入, 也可沿用 <option> 子元素; 列表渲染到 body, 避免被玻璃卡片裁剪.
 */
export function Select({
  value, onChange, options: optionList, children, placeholder = '请选择', className, id, disabled, title, ...aria
}: {
  value: string; onChange: (value: string) => void; options?: SelectOption[]; children?: ReactNode;
  placeholder?: string; className?: string; id?: string; disabled?: boolean; title?: string;
  'aria-invalid'?: boolean; 'aria-describedby'?: string; 'aria-label'?: string; 'aria-labelledby'?: string;
}) {
  const listId = useId();
  const triggerId = useId();
  const btnRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ text: '', at: 0 });
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const options = useMemo(() => optionList ?? optionsFromChildren(children), [optionList, children]);
  const current = options.find((o) => o.value === value);

  useLayoutEffect(() => {
    if (open && btnRef.current) setRect(btnRef.current.getBoundingClientRect());
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    // 只滚动列表自身; scrollIntoView 会连带滚动页面, 触发下面的关闭逻辑
    const list = listRef.current;
    const item = list?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    if (!list || !item) return;
    if (item.offsetTop < list.scrollTop) list.scrollTop = item.offsetTop;
    else if (item.offsetTop + item.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = item.offsetTop + item.offsetHeight - list.clientHeight;
  }, [open, active]);

  useEffect(() => {
    if (!open) return;
    const inside = (target: EventTarget | null) => target instanceof Node
      && (btnRef.current?.contains(target) || listRef.current?.contains(target));
    const onPointer = (event: PointerEvent) => { if (!inside(event.target)) setOpen(false); };
    // 外层滚动时列表跟随触发器; 触发器滚出可视区才关闭
    const onScroll = (event: Event) => {
      if (inside(event.target) || !btnRef.current) return;
      const next = btnRef.current.getBoundingClientRect();
      if (next.bottom < 0 || next.top > window.innerHeight) setOpen(false);
      else setRect(next);
    };
    // 先于弹窗/抽屉在 document 捕获阶段的 Escape 处理, 只关闭列表
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      btnRef.current?.focus();
    };
    const close = () => setOpen(false);
    window.addEventListener('pointerdown', onPointer, true);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', close);
    window.addEventListener('keydown', onKey, true);
    return () => {
      window.removeEventListener('pointerdown', onPointer, true);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', close);
      window.removeEventListener('keydown', onKey, true);
    };
  }, [open]);

  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);

  const enabledIndex = (from: number, step: 1 | -1) => {
    for (let n = 1; n <= options.length; n += 1) {
      const index = (from + step * n + options.length * n) % options.length;
      if (!options[index].disabled) return index;
    }
    return from;
  };
  const openList = () => {
    if (disabled || !options.length) return;
    const selected = options.findIndex((o) => o.value === value);
    setActive(selected >= 0 ? selected : enabledIndex(-1, 1));
    setOpen(true);
  };
  const commit = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    setOpen(false);
    btnRef.current?.focus();
    if (option.value !== value) onChange(option.value);
  };
  const jumpTo = (key: string) => {
    const now = performance.now();
    typeahead.current = { text: (now - typeahead.current.at < 700 ? typeahead.current.text : '') + key.toLowerCase(), at: now };
    const match = options.findIndex((o) => !o.disabled && o.label.toLowerCase().startsWith(typeahead.current.text));
    if (match < 0) return;
    if (open) setActive(match);
    else commit(match);
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      jumpTo(event.key);
      return;
    }
    if (!open) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) { event.preventDefault(); openList(); }
      return;
    }
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((index) => enabledIndex(index, 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive((index) => enabledIndex(index, -1)); }
    else if (event.key === 'Home') { event.preventDefault(); setActive(enabledIndex(-1, 1)); }
    else if (event.key === 'End') { event.preventDefault(); setActive(enabledIndex(options.length, -1)); }
    else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); commit(active); }
    else if (event.key === 'Tab') setOpen(false);
  };

  const below = rect ? window.innerHeight - rect.bottom >= Math.min(LIST_MAX, rect.top) : true;
  const width = rect ? Math.min(Math.max(rect.width, 180), window.innerWidth - EDGE * 2) : 0;
  const left = rect ? Math.min(Math.max(EDGE, rect.left), window.innerWidth - width - EDGE) : 0;
  const room = rect ? (below ? window.innerHeight - rect.bottom : rect.top) - LIST_GAP - EDGE : LIST_MAX;

  return (
    <>
      <button
        ref={btnRef}
        id={id ?? triggerId}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-activedescendant={open ? `${listId}-${active}` : undefined}
        data-value={value}
        data-open={open ? '' : undefined}
        disabled={disabled}
        title={title ?? current?.label}
        onClick={(event) => { event.stopPropagation(); if (open) setOpen(false); else openList(); }}
        onKeyDown={onKeyDown}
        className={cn('select-trigger field', className)}
        {...aria}
      >
        {current?.icon && <span aria-hidden className="flex-none">{current.icon}</span>}
        <span className={cn('select-value', !current && 'text-fg-4')}>{current?.label ?? placeholder}</span>
        <span aria-hidden className="select-chevron"><ChevronDown /></span>
      </button>
      {open && rect && createPortal(
        <div
          ref={listRef}
          id={listId}
          role="listbox"
          aria-labelledby={id ?? triggerId}
          className="select-list scroll"
          data-placement={below ? 'bottom' : 'top'}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => event.stopPropagation()}
          style={{
            left,
            width,
            maxHeight: Math.max(120, Math.min(LIST_MAX, room)),
            ...(below ? { top: rect.bottom + LIST_GAP } : { bottom: window.innerHeight - rect.top + LIST_GAP }),
          }}
        >
          {options.map((option, index) => {
            const selected = option.value === value;
            return (
              <div
                key={`${option.value}:${index}`}
                id={`${listId}-${index}`}
                role="option"
                aria-selected={selected}
                aria-disabled={option.disabled || undefined}
                data-value={option.value}
                data-index={index}
                data-active={index === active ? '' : undefined}
                onPointerEnter={() => { if (!option.disabled) setActive(index); }}
                onPointerDown={(event) => event.preventDefault()}
                onClick={() => commit(index)}
                className="select-option"
              >
                {option.icon && <span className="flex-none text-fg-3">{option.icon}</span>}
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{option.label}</span>
                  {option.description && <span className="block truncate text-xs text-fg-3">{option.description}</span>}
                </span>
                <Check aria-hidden className={cn('h-4 w-4 flex-none text-accent transition-opacity', selected ? 'opacity-100' : 'opacity-0')} />
              </div>
            );
          })}
        </div>,
        document.body,
      )}
    </>
  );
}
