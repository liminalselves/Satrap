import { createContext, useCallback, useContext, useLayoutEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import type { ButtonProps } from '@/components/ui/Button';

export interface ConfirmationOptions {
  title?: string;
  confirmText?: string;
  variant?: ButtonProps['variant'];
}

export type RequestConfirmation = (message: string, options: ConfirmationOptions, signal: AbortSignal) => Promise<boolean>;
export const ConfirmationContext = createContext<RequestConfirmation | null>(null);

// 导航, 账号切换或调用方卸载时取消待确认操作, 不执行旧页面的写请求
export function useConfirm(scope = '') {
  const request = useContext(ConfirmationContext);
  const location = useLocation();
  const controller = useRef<AbortController | null>(null);
  useLayoutEffect(() => {
    const current = new AbortController();
    controller.current = current;
    return () => current.abort();
  }, [location.key, scope]);
  const confirm = useCallback((message: string, options: ConfirmationOptions = {}) => {
    const signal = controller.current?.signal;
    if (!request || !signal || signal.aborted) return Promise.resolve(false);
    return request(message, options, signal).then((accepted) => accepted && !signal.aborted);
  }, [request]);
  if (!request) throw new Error('确认弹窗服务未安装');
  return confirm;
}
