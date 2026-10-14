import type { ReactNode } from 'react';
import { Button, type ButtonProps } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';

// 危险操作的二次确认弹窗: 替代 window.confirm, 与记录管理域其它页面的确认交互一致
export function ConfirmModal({
  open,
  title,
  message,
  confirmText = '确认',
  variant = 'danger',
  busy = false,
  error,
  children,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmText?: string;
  variant?: ButtonProps['variant'];
  busy?: boolean;
  error?: string;
  children?: ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal open={open} onClose={() => { if (!busy) onCancel(); }} title={title} size="sm">
      <div className="space-y-4">
        <p className="whitespace-pre-wrap break-words text-sm text-text-secondary">{message}</p>
        {children}
        {error && <p role="alert" className="text-error">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>取消</Button>
          <Button variant={variant} onClick={onConfirm} disabled={busy}>{confirmText}</Button>
        </div>
      </div>
    </Modal>
  );
}
