import { useEffect, useRef, useCallback } from 'react';

/**
 * 用于跟踪鼠标在元素上的位置，并更新 CSS 变量
 * 供玻璃反光效果使用
 * 
 * 使用像素值而不是百分比，确保反光大小统一
 */
export function useMousePosition<T extends HTMLElement>() {
  const ref = useRef<T>(null);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    const element = ref.current;
    if (!element) return;

    const rect = element.getBoundingClientRect();
    // 使用像素值，确保反光中心精确对准鼠标
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    element.style.setProperty('--mouse-x', `${x}px`);
    element.style.setProperty('--mouse-y', `${y}px`);
  }, []);

  const handleMouseLeave = useCallback(() => {
    const element = ref.current;
    if (!element) return;
    // 鼠标离开时重置位置，避免反光停留在边缘
    element.style.setProperty('--mouse-x', '-1000px');
    element.style.setProperty('--mouse-y', '-1000px');
  }, []);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    element.addEventListener('mousemove', handleMouseMove);
    element.addEventListener('mouseleave', handleMouseLeave);
    return () => {
      element.removeEventListener('mousemove', handleMouseMove);
      element.removeEventListener('mouseleave', handleMouseLeave);
    };
  }, [handleMouseMove, handleMouseLeave]);

  return ref;
}
