import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { X, CheckCircle, AlertCircle, AlertTriangle, Info } from 'lucide-react';
import { cn } from '@/utils/cn';

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface Toast {
  id: string;
  type: ToastType;
  message: string;
  duration?: number;
  count?: number;
}

interface ToastItemProps {
  toast: Toast;
  onClose: (id: string) => void;
}

const toastTint = { success: 'tint-green', error: 'tint-red', warning: 'tint-orange', info: 'tint-accent' } as const;

function ToastItem({ toast, onClose }: ToastItemProps) {
  const [paused, setPaused] = useState(false);
  const duration = toast.duration || 5000;
  const repeat = toast.count ?? 1;

  // 悬停暂停倒计时; 同类消息再次出现时重新计时
  useEffect(() => {
    if (paused) return;
    const timer = setTimeout(() => {
      onClose(toast.id);
    }, duration);

    return () => clearTimeout(timer);
  }, [toast.id, duration, repeat, paused, onClose]);

  const icons = {
    success: <CheckCircle className="h-[18px] w-[18px] text-success" />,
    error: <AlertCircle className="h-[18px] w-[18px] text-error" />,
    warning: <AlertTriangle className="h-[18px] w-[18px] text-warning" />,
    info: <Info className="h-[18px] w-[18px] text-accent" />,
  };

  const bgClasses = {
    success: 'glass-toast glass-toast-success',
    error: 'glass-toast glass-toast-error',
    warning: 'glass-toast glass-toast-warning',
    info: 'glass-toast glass-toast-info',
  };

  return (
    <div
      onPointerEnter={() => setPaused(true)}
      onPointerLeave={() => setPaused(false)}
      className={cn(
        bgClasses[toast.type],
        toastTint[toast.type],
        'pointer-events-auto rounded-xl p-3.5 flex items-start gap-3 min-w-[300px] max-w-md'
      )}
    >
      <span className="mt-0.5 shrink-0">{icons[toast.type]}</span>
      <p className="flex-1 text-sm leading-5 text-text-primary">{toast.message}</p>
      {repeat > 1 && <span className="num shrink-0 rounded-full bg-[rgb(var(--tint)/0.18)] px-1.5 text-[11px] leading-5 text-[var(--tint-fg)]" aria-label={`重复 ${repeat} 次`}>×{repeat}</span>}
      <button
        onClick={() => onClose(toast.id)}
        aria-label="关闭通知"
        className="-m-0.5 p-1 rounded-md text-text-tertiary hover:text-text-primary hover:bg-glass-hover transition-colors"
      >
        <X className="h-4 w-4" />
      </button>
      {/* 剩余时间 */}
      <span
        key={repeat}
        aria-hidden
        className="absolute bottom-0 left-0 h-[2px] bg-[rgb(var(--tint)/0.7)]"
        style={{ animation: `toast-progress ${duration}ms linear forwards`, animationPlayState: paused ? 'paused' : 'running' }}
      />
    </div>
  );
}

/** 位于顶栏下方右侧, 不遮挡顶栏操作 */
export function ToastContainer({ toasts, onClose }: { toasts: Toast[]; onClose: (id: string) => void }) {
  return createPortal(
    <div className="pointer-events-none fixed top-[76px] right-4 z-[210] flex flex-col gap-2" aria-live="polite">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onClose={onClose} />
      ))}
    </div>,
    document.body
  );
}

// 简单的 Toast 管理 hook
let toastId = 0;
const listeners: ((toasts: Toast[]) => void)[] = [];
let toasts: Toast[] = [];

export function toast(type: ToastType, message: string, duration?: number) {
  // 同类同文案合并计数, 不重复堆叠
  const existing = toasts.find((item) => item.type === type && item.message === message);
  if (existing) {
    toasts = toasts.map((item) => item === existing ? { ...item, count: (item.count ?? 1) + 1, duration } : item);
    listeners.forEach((l) => l(toasts));
    return existing.id;
  }
  const id = `toast-${++toastId}`;
  toasts = [...toasts, { id, type, message, duration }];
  listeners.forEach((l) => l(toasts));
  return id;
}

export function useToasts() {
  const [toastList, setToastList] = useState<Toast[]>(toasts);

  useEffect(() => {
    listeners.push(setToastList);
    return () => {
      const index = listeners.indexOf(setToastList);
      if (index > -1) listeners.splice(index, 1);
    };
  }, []);

  const closeToast = (id: string) => {
    toasts = toasts.filter((t) => t.id !== id);
    listeners.forEach((l) => l(toasts));
  };

  return { toasts: toastList, closeToast };
}
