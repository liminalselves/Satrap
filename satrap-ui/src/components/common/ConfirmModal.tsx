import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';

// 危险操作的二次确认弹窗: 替代 window.confirm, 与记录管理域其它页面的确认交互一致
export function ConfirmModal({
  open,
  title,
  message,
  confirmText = '确认',
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmText?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal open={open} onClose={onCancel} title={title} size="sm">
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">{message}</p>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>取消</Button>
          <Button variant="danger" onClick={onConfirm} disabled={busy}>{confirmText}</Button>
        </div>
      </div>
    </Modal>
  );
}
