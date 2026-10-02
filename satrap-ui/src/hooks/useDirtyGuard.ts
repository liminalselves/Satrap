import { useCallback, useEffect, useRef } from 'react';
import { useBlocker } from 'react-router-dom';

export const DISCARD_MESSAGE = '当前表单有未保存的修改, 确定放弃吗?';

// Modal 关闭 (取消按钮/Escape/遮罩/关闭按钮) 的统一确认入口
export function confirmDiscard(): boolean {
  return window.confirm(DISCARD_MESSAGE);
}

/**
 * 脏状态保护, 接受布尔值或实时读取多个编辑区域的状态函数
 * 浏览器关闭和刷新交给原生确认; 站内导航及前进后退由 useBlocker 拦截
 * 确认后继续原导航, 拒绝则保留页面和草稿; 草稿不写入浏览器存储
 */
export function useDirtyGuard(active: boolean | (() => boolean)): void {
  const activeRef = useRef(active);
  activeRef.current = active;
  const isActive = useCallback(() => typeof activeRef.current === 'function' ? activeRef.current() : activeRef.current, []);
  const blocker = useBlocker(isActive);   // 保存后立即按最新状态判断, 避免路由回调仍使用上一帧的脏状态

  useEffect(() => {
    if (blocker.state !== 'blocked') return;
    if (confirmDiscard()) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  useEffect(() => {
    if (!active) return;
    const handler = (event: BeforeUnloadEvent) => {
      if (!isActive()) return;
      event.preventDefault();
      event.returnValue = DISCARD_MESSAGE;
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [active, isActive]);
}
