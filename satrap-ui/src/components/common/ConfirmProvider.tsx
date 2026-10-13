import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { ConfirmationContext, type ConfirmationOptions, type RequestConfirmation } from '@/hooks/useConfirm';
import { ConfirmModal } from './ConfirmModal';

interface PendingConfirmation {
  message: string;
  options: ConfirmationOptions;
  finish: (accepted: boolean) => void;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const current = useRef<PendingConfirmation | null>(null);
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const request = useCallback<RequestConfirmation>((message, options, signal) => {
    if (signal.aborted || current.current) return Promise.resolve(false);
    return new Promise((resolve) => {
      const entry: PendingConfirmation = { message, options, finish: (accepted) => {
        if (current.current !== entry) return;
        current.current = null;
        signal.removeEventListener('abort', abort);
        setPending(null);
        resolve(accepted && !signal.aborted);
      } };
      const abort = () => entry.finish(false);
      current.current = entry;
      signal.addEventListener('abort', abort, { once: true });
      setPending(entry);
    });
  }, []);
  useEffect(() => () => current.current?.finish(false), []);
  return <ConfirmationContext.Provider value={request}>
    {children}
    <ConfirmModal open={!!pending} title={pending?.options.title || '确认操作'} message={pending?.message || ''}
      confirmText={pending?.options.confirmText} variant={pending?.options.variant}
      onConfirm={() => pending?.finish(true)} onCancel={() => pending?.finish(false)} />
  </ConfirmationContext.Provider>;
}
