import { useEffect, useRef } from 'react';

/** 滚动经过分节会反复激活; 在该间隔内重新激活不再重复读取 */
export const ACTIVATION_REFRESH_MS = 30_000;

/**
 * 分节激活时读取数据, 刚读取过则跳过
 *
 * load 返回 false 表示读取失败, 下次激活会立即重试; 依赖变化 (如服务地址) 时重置并重新读取
 */
export function useActivationLoad(active: boolean, load: () => Promise<boolean | void>, deps: readonly unknown[] = []) {
  const loadedAt = useRef(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    loadedAt.current = 0;
    // deps 由调用方提供, 用于在数据来源变化时强制重读
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    if (!active || Date.now() - loadedAt.current <= ACTIVATION_REFRESH_MS) return;
    let cancelled = false;
    void loadRef.current().then((ok) => {
      if (!cancelled && ok !== false) loadedAt.current = Date.now();
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, ...deps]);
}
