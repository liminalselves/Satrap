import { useEffect } from 'react';
import { useBlocker } from 'react-router-dom';

export const DISCARD_MESSAGE = '当前表单有未保存的修改, 确定放弃吗?';

// Modal 关闭 (取消按钮/Escape/遮罩/关闭按钮) 的统一确认入口
export function confirmDiscard(): boolean {
  return window.confirm(DISCARD_MESSAGE);
}

/**
 * 脏状态保护:
 * - 浏览器关闭/刷新 (beforeunload): 交给浏览器原生确认;
 * - 站内导航 (侧栏链接/代码跳转) 与浏览器前进/后退: 数据路由的 useBlocker 统一拦截,
 *   确认后按原意图继续跳转, 拒绝则留在当前页面且草稿保持。
 * 确认只决定是否离开, 不改写草稿; 草稿不写入 localStorage/sessionStorage。
 */
export function useDirtyGuard(active: boolean): void {
  const blocker = useBlocker(active);

  useEffect(() => {
    if (blocker.state !== 'blocked') return;
    if (confirmDiscard()) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  useEffect(() => {
    if (!active) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = DISCARD_MESSAGE;
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [active]);
}
