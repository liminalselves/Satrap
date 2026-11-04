import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '@/utils/cn';

/** 右侧检查器抽屉: 不遮挡左侧列表, Esc 关闭并归还焦点 */
export function Drawer({ open, onClose, label, children, width = 720, className }: {
  open: boolean; onClose: () => void; label: string; children: ReactNode; width?: number; className?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement;
    requestAnimationFrame(() => panelRef.current?.focus());
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); onCloseRef.current(); } };
    document.addEventListener('keydown', onKey, true);
    return () => { document.removeEventListener('keydown', onKey, true); restoreRef.current?.focus?.(); };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-[110]">
      <div className="absolute inset-0 bg-black/25 animate-fade [[data-theme=light]_&]:bg-slate-500/10" onClick={onClose} aria-hidden />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        className={cn('surface surface-strong absolute bottom-2 right-2 top-2 flex max-w-[calc(100vw-16px)] flex-col !rounded-[22px] !shadow-[var(--shadow-3)] outline-none', className)}
        style={{ width, animation: 'drawer-in 420ms var(--ease-out) both' }}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
