import { useEffect } from 'react';

export const DISCARD_MESSAGE = '当前表单有未保存的修改, 确定放弃吗?';

// Modal 关闭 (取消按钮/Escape/遮罩/关闭按钮) 的统一确认入口
export function confirmDiscard(): boolean {
  return window.confirm(DISCARD_MESSAGE);
}

/**
 * 脏状态保护: 浏览器关闭/刷新 (beforeunload)。
 * 应用内路由切换由弹窗遮罩独占保证: 弹窗打开时侧栏链接不可点, 任何点击落在遮罩上
 * 都会经过调用方的 guardedClose (confirmDiscard); 弹窗关闭后表单即非脏, 可正常导航。
 * 非数据路由 (BrowserRouter) 下 history.block 已被 @remix-run/router 移除, useBlocker 不可用;
 * 浏览器前进/后退跳过确认属于已知边界, 需迁移数据路由才能补齐。
 */
export function useDirtyGuard(active: boolean): void {
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
