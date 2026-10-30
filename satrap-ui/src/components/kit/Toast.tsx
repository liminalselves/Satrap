import { toast as push } from '@/components/ui/Toast';

/** 页面通知: 复用全局通知队列, 标题与说明合成一行 */
const send = (type: 'success' | 'error' | 'warning' | 'info') => (title: string, description?: string) =>
  push(type, description ? `${title}: ${description}` : title);

export const toast = {
  success: send('success'),
  error: send('error'),
  warning: send('warning'),
  info: send('info'),
};
