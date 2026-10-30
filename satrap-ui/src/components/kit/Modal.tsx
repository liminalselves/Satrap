import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { cn } from '@/utils/cn';
import { IconButton } from './Button';

export function Modal({ open, onClose, title, description, icon, children, footer, size = 'md', className }: {
  open: boolean; onClose: () => void; title?: ReactNode; description?: ReactNode; icon?: ReactNode;
  children: ReactNode; footer?: ReactNode; size?: 'sm' | 'md' | 'lg' | 'xl'; className?: string;
}) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement;
    const panel = panelRef.current;
    const focusable = () => Array.from(panel?.querySelectorAll<HTMLElement>('button, [href], input, textarea, [tabindex]:not([tabindex="-1"])') ?? []).filter((el) => !el.hasAttribute('disabled'));
    requestAnimationFrame(() => (panel?.querySelector<HTMLElement>('[data-autofocus]') ?? focusable()[1] ?? panel)?.focus());
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); onClose(); }
      if (e.key !== 'Tab') return;
      const els = focusable();
      if (!els.length) return;
      const first = els[0];
      const last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', onKey, true);
    return () => { document.removeEventListener('keydown', onKey, true); restoreRef.current?.focus?.(); };
  }, [open, onClose]);

  if (!open) return null;
  const width = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-2xl', xl: 'max-w-4xl' }[size];

  return createPortal(
    <div className="fixed inset-0 z-[120] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-[6px] animate-fade [[data-theme=light]_&]:bg-white/40" onClick={onClose} aria-hidden />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        tabIndex={-1}
        className={cn('surface surface-strong relative flex max-h-[min(86vh,820px)] w-full flex-col !rounded-[22px] !shadow-[var(--shadow-3)] outline-none', width, className)}
        style={{ animation: 'dialog-in 420ms var(--ease-out) both' }}
      >
        {title && (
          <div className="flex items-start gap-3 px-6 pt-5 pb-4">
            {icon && <div className="grid h-9 w-9 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.14)] text-tint">{icon}</div>}
            <div className="min-w-0 flex-1">
              <h2 id={titleId} className="text-lg font-semibold text-fg">{title}</h2>
              {description && <p className="mt-0.5 text-[13px] text-fg-3">{description}</p>}
            </div>
            <IconButton label="关闭弹窗" shortcut="Esc" size="sm" onClick={onClose} className="-mr-2 -mt-1"><X className="h-4 w-4" /></IconButton>
          </div>
        )}
        <div className="scroll min-h-0 flex-1 px-6 pb-5">{children}</div>
        {footer && <div className="flex items-center justify-end gap-2 border-t border-line px-6 py-4">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
