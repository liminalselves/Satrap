import { cloneElement, useId, useRef, useState, type ReactElement } from 'react';
import { createPortal } from 'react-dom';

interface TooltipProps {
  content: string;
  shortcut?: string;
  side?: 'top' | 'bottom' | 'right';
  children: ReactElement;
}

export function Tooltip({ content, shortcut, side = 'bottom', children }: TooltipProps) {
  const id = useId();
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const timer = useRef<number>();

  const show = (el: HTMLElement) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      const r = el.getBoundingClientRect();
      if (side === 'right') setPos({ x: r.right + 10, y: r.top + r.height / 2 });
      else if (side === 'top') setPos({ x: r.left + r.width / 2, y: r.top - 8 });
      else setPos({ x: r.left + r.width / 2, y: r.bottom + 8 });
    }, 380);
  };
  const hide = () => { window.clearTimeout(timer.current); setPos(null); };

  const transform = side === 'right' ? 'translate(0,-50%)' : side === 'top' ? 'translate(-50%,-100%)' : 'translate(-50%,0)';

  return (
    <>
      {cloneElement(children, {
        'aria-describedby': pos ? id : undefined,
        onPointerEnter: (e: React.PointerEvent<HTMLElement>) => { children.props.onPointerEnter?.(e); show(e.currentTarget); },
        onPointerLeave: (e: React.PointerEvent<HTMLElement>) => { children.props.onPointerLeave?.(e); hide(); },
        onFocus: (e: React.FocusEvent<HTMLElement>) => { children.props.onFocus?.(e); if (e.currentTarget.matches(':focus-visible')) show(e.currentTarget); },
        onBlur: (e: React.FocusEvent<HTMLElement>) => { children.props.onBlur?.(e); hide(); },
        onClick: (e: React.MouseEvent<HTMLElement>) => { children.props.onClick?.(e); hide(); },
      })}
      {pos && createPortal(
        <div
          id={id}
          role="tooltip"
          className="pointer-events-none fixed z-[200] animate-pop"
          style={{ left: pos.x, top: pos.y, transform }}
        >
          <div className="flex items-center gap-2 rounded-md border border-line px-2.5 py-1.5 text-xs text-fg shadow-lg surface-strong backdrop-blur-xl">
            {content}
            {shortcut && <span className="kbd">{shortcut}</span>}
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
