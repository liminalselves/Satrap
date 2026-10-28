import { useCallback, useRef, useState, type SetStateAction } from 'react';

// 仅存于内存: 注销会整页刷新, 不会把上一个账号的数据带进新会话
const cache = new Map<string, unknown>();

/** 读取缓存值, 没有记录时返回 fallback */
export function readCached<T>(key: string, fallback: T): T {
  return cache.has(key) ? cache.get(key) as T : fallback;
}

/** 按 SetStateAction 语义写入缓存并返回新值 */
export function writeCached<T>(key: string, value: SetStateAction<T>, fallback: T): T {
  const next = typeof value === 'function' ? (value as (prev: T) => T)(readCached(key, fallback)) : value;
  cache.set(key, next);
  return next;
}

/** 测试用: 清空全部缓存 */
export function clearCachedState(): void {
  cache.clear();
}

/**
 * 与 useState 相同, 但值按 key 保留到组件卸载之后
 *
 * 路由切换会卸载页面, 重新进入时先显示上次的结果, 由页面自己的轮询在后台更新;
 * key 变化时切到该 key 的缓存, 没有缓存时回到 initial
 */
export function useCachedState<T>(key: string, initial: T): [T, (value: SetStateAction<T>) => void] {
  const initialRef = useRef(initial);
  const [state, setState] = useState(() => ({ key, value: readCached(key, initial) }));
  const keyRef = useRef(key);
  keyRef.current = key;

  // 渲染期切换 key, 不会先画出一帧旧 key 的数据
  let current = state;
  if (state.key !== key) {
    current = { key, value: readCached(key, initialRef.current) };
    setState(current);
  }

  const set = useCallback((value: SetStateAction<T>) => {
    const target = keyRef.current;
    const next = writeCached(target, value, initialRef.current);
    setState({ key: target, value: next });
  }, []);

  return [current.value, set];
}
