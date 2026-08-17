import { useEffect, useCallback, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Button } from './Button';

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: React.ReactNode;
  size?: 'sm' | 'md' | 'lg' | 'xl';
  showClose?: boolean;
}

// 全局 Modal 栈: 嵌套弹窗时只有栈顶(最上层)响应 Escape / 遮罩点击
const modalStack: symbol[] = [];
// 递增层级计数器: 每打开一个 Modal 分配更高 z-index, 避免渲染期读栈不可靠
let zCounter = 50;

export function Modal({
  open,
  onClose,
  title,
  children,
  size = 'md',
  showClose = true,
}: ModalProps) {
  const idRef = useRef<symbol>(Symbol('modal'));
  const id = idRef.current;
  // 本 Modal 的层级, 打开时分配(后打开者更高)
  const [zIndex, setZIndex] = useState(50);

  const isTop = useCallback(() => modalStack[modalStack.length - 1] === id, [id]);

  // 进出栈 + 分配层级
  useEffect(() => {
    if (open) {
      modalStack.push(id);
      setZIndex(++zCounter);
      document.body.style.overflow = 'hidden';
    }
    return () => {
      const idx = modalStack.indexOf(id);
      if (idx !== -1) modalStack.splice(idx, 1);
      if (modalStack.length === 0) {
        document.body.style.overflow = '';
        zCounter = 50; // 全部关闭后重置, 避免无限增长
      }
    };
  }, [open, id]);

  // Escape 仅栈顶响应
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isTop()) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose, isTop]);

  if (!open) return null;

  const sizeClasses = {
    sm: 'max-w-sm',
    md: 'max-w-md',
    lg: 'max-w-lg',
    xl: 'max-w-xl',
  };

  return createPortal(
    <div className="fixed inset-0 flex items-center justify-center p-4" style={{ zIndex }}>
      <div className="glass-overlay" onClick={() => { if (isTop()) onClose(); }} />
      <div
        className={cn(
          'glass-modal relative w-full flex flex-col max-h-[85vh]',
          sizeClasses[size]
        )}
        role="dialog"
        aria-modal="true"
      >
        {(title || showClose) && (
          <div className="flex items-center justify-between mb-4 shrink-0">
            {title && <h3 className="text-lg font-semibold text-text-primary">{title}</h3>}
            {showClose && (
              <Button variant="ghost" size="sm" onClick={onClose} className="ml-auto">
                <X className="h-4 w-4" />
              </Button>
            )}
          </div>
        )}
        <div className="overflow-y-auto custom-scrollbar flex-1 min-h-0 -mx-6 px-6">
          {children}
        </div>
      </div>
    </div>,
    document.body
  );
}
