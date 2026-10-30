import { useCallback, useEffect, useSyncExternalStore } from 'react';

type Theme = 'dark' | 'light';

const THEME_KEY = 'satrap-theme';

/** 页面内多个组件共享同一主题, 切换后立即同步 */
const listeners = new Set<() => void>();

function readStoredTheme(): Theme | null {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return saved === 'dark' || saved === 'light' ? saved : null;
  } catch {
    return null;
  }
}

function systemTheme(): Theme {
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

let current: Theme = typeof window !== 'undefined' ? readStoredTheme() ?? systemTheme() : 'dark';

function apply(next: Theme, persist: boolean) {
  current = next;
  document.documentElement.setAttribute('data-theme', next);
  if (persist) {
    try { localStorage.setItem(THEME_KEY, next); } catch { /* 存储不可用时只影响当前页 */ }
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/**
 * 切换主题; 浏览器支持 View Transitions 时从点击位置圆形扩散
 *
 * 参数:
 * - origin: 扩散起点 (视口坐标), 缺省为右上角
 */
export function switchTheme(origin?: { x: number; y: number }) {
  const next: Theme = current === 'dark' ? 'light' : 'dark';
  const doc = document as Document & { startViewTransition?: (callback: () => void) => { ready: Promise<void> } };
  const reduced = document.documentElement.getAttribute('data-motion') === 'reduced'
    || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!doc.startViewTransition || reduced) {
    apply(next, true);
    return;
  }
  const x = origin?.x ?? window.innerWidth - 40;
  const y = origin?.y ?? 32;
  const radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
  const transition = doc.startViewTransition(() => apply(next, true));
  void transition.ready.then(() => {
    document.documentElement.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
      { duration: 560, easing: 'cubic-bezier(0.16, 1, 0.3, 1)', pseudoElement: '::view-transition-new(root)' },
    );
  }).catch(() => undefined);
}

export function useTheme() {
  const theme = useSyncExternalStore(subscribe, () => current, () => current);

  // 首次挂载写入 data-theme, 并跟随未手动设置时的系统主题
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', current);
    const mediaQuery = window.matchMedia('(prefers-color-scheme: light)');
    const handleChange = (e: MediaQueryListEvent) => {
      if (!readStoredTheme()) apply(e.matches ? 'light' : 'dark', false);
    };
    mediaQuery.addEventListener('change', handleChange);
    return () => mediaQuery.removeEventListener('change', handleChange);
  }, []);

  const setTheme = useCallback((newTheme: Theme) => apply(newTheme, true), []);
  const toggleTheme = useCallback((origin?: { x: number; y: number }) => switchTheme(origin), []);

  return {
    theme,
    setTheme,
    toggleTheme,
  };
}
