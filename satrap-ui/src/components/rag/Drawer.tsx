import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '@/utils/cn';

/**
 * 知识库详情抽屉。
 *
 * 与 @/components/kit/Drawer 的视觉和交互一致, 但层级低于全局 Legacy Modal 栈
 * (legacy Modal 从 z=51 起递增), 这样在页面内另开的配置/导入/插件弹窗始终位于
 * 抽屉之上, 不会被抽屉的 select 等控件拦截点击。
 */
export function Drawer({ open, onClose, label, children, width = 720, className }: {
  open: boolean; onClose: () => void; label: string; children: ReactNode; width?: number; className?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement;
    requestAnimationFrame(() => panelRef.current?.focus());
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      // 更高层弹窗打开时由它自己处理 Esc, 避免把底层抽屉一起关掉
      const panels = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]'));
      if (panels[panels.length - 1] !== panelRef.current) return;
      e.stopPropagation();
      onClose();
    };
    document.addEventListener('keydown', onKey, true);
    return () => { document.removeEventListener('keydown', onKey, true); restoreRef.current?.focus?.(); };
  }, [open, onClose]);

  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-[45]">
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
