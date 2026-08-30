import {
  useEffect,
  useRef,
  useCallback,
  useMemo,
  createContext,
  useContext,
  ReactNode,
  type RefCallback,
} from 'react';

/**
 * 全局玻璃反射管理器
 * 跟踪鼠标位置, 为所有注册的玻璃元素提供反射效果
 */

interface GlassElement {
  element: HTMLElement;
  reflectRange: number;
  reflectSize: number;
}

interface GlassReflectContextType {
  register: (element: HTMLElement, options?: { reflectRange?: number; reflectSize?: number }) => void;
  unregister: (element: HTMLElement) => void;
}

const GlassReflectContext = createContext<GlassReflectContextType | null>(null);

// 默认配置
const DEFAULT_REFLECT_RANGE = 150;   // 反光影响范围
const DEFAULT_REFLECT_SIZE = 150;   // 反光光圈大小

function updateReflectState(
  element: HTMLElement,
  mouseX: number,
  mouseY: number,
  reflectRange: number,
) {
  const rect = element.getBoundingClientRect();
  const x = mouseX - rect.left;
  const y = mouseY - rect.top;
  const closestX = Math.max(rect.left, Math.min(mouseX, rect.right));
  const closestY = Math.max(rect.top, Math.min(mouseY, rect.bottom));
  const distanceToEdge = Math.hypot(mouseX - closestX, mouseY - closestY);
  const isInRange = distanceToEdge < reflectRange;
  const isHovering = mouseX >= rect.left && mouseX <= rect.right
    && mouseY >= rect.top && mouseY <= rect.bottom;

  element.style.setProperty('--mouse-x', `${x}px`);
  element.style.setProperty('--mouse-y', `${y}px`);
  if (isHovering) {
    element.classList.add('glass-hovering');
    element.classList.remove('glass-nearby');
  } else if (isInRange) {
    element.classList.remove('glass-hovering');
    element.classList.add('glass-nearby');
  } else {
    element.classList.remove('glass-hovering', 'glass-nearby');
  }
}

export function GlassReflectProvider({ children }: { children: ReactNode }) {
  const elementsRef = useRef<Map<HTMLElement, GlassElement>>(new Map());
  const mousePosRef = useRef<{ x: number; y: number }>({ x: -1000, y: -1000 });
  const rafRef = useRef<number | null>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);

  // 更新所有元素的反射状态
  const updateAllElements = useCallback(() => {
    const { x: mouseX, y: mouseY } = mousePosRef.current;

    elementsRef.current.forEach(({ element, reflectRange }) => {
      updateReflectState(element, mouseX, mouseY, reflectRange);
    });
  }, []);

  // 节流更新
  const scheduleUpdate = useCallback(() => {
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      updateAllElements();
      rafRef.current = null;
    });
  }, [updateAllElements]);

  // 全局鼠标移动监听
  useEffect(() => {
    const handlePointerMove = (e: PointerEvent) => {
      mousePosRef.current = { x: e.clientX, y: e.clientY };
      scheduleUpdate();
    };

    const handlePointerLeave = () => {
      mousePosRef.current = { x: -1000, y: -1000 };
      scheduleUpdate();
    };

    const handleGeometryChange = () => scheduleUpdate();

    document.addEventListener('pointermove', handlePointerMove);
    document.documentElement.addEventListener('pointerleave', handlePointerLeave);
    document.addEventListener('scroll', handleGeometryChange, true);
    window.addEventListener('resize', handleGeometryChange);
    window.visualViewport?.addEventListener('resize', handleGeometryChange);
    window.visualViewport?.addEventListener('scroll', handleGeometryChange);

    return () => {
      document.removeEventListener('pointermove', handlePointerMove);
      document.documentElement.removeEventListener('pointerleave', handlePointerLeave);
      document.removeEventListener('scroll', handleGeometryChange, true);
      window.removeEventListener('resize', handleGeometryChange);
      window.visualViewport?.removeEventListener('resize', handleGeometryChange);
      window.visualViewport?.removeEventListener('scroll', handleGeometryChange);
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
      }
    };
  }, [scheduleUpdate]);

  useEffect(() => {
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => scheduleUpdate());
    resizeObserverRef.current = observer;
    elementsRef.current.forEach(({ element }) => observer.observe(element));
    return () => {
      observer.disconnect();
      resizeObserverRef.current = null;
    };
  }, [scheduleUpdate]);

  // 注册元素
  const register = useCallback((
    element: HTMLElement, 
    options?: { reflectRange?: number; reflectSize?: number }
  ) => {
    elementsRef.current.set(element, {
      element,
      reflectRange: options?.reflectRange ?? DEFAULT_REFLECT_RANGE,
      reflectSize: options?.reflectSize ?? DEFAULT_REFLECT_SIZE,
    });
    element.style.setProperty('--reflect-size', `${options?.reflectSize ?? DEFAULT_REFLECT_SIZE}px`);
    resizeObserverRef.current?.observe(element);
    scheduleUpdate();
  }, [scheduleUpdate]);

  // 注销元素
  const unregister = useCallback((element: HTMLElement) => {
    resizeObserverRef.current?.unobserve(element);
    elementsRef.current.delete(element);
    element.classList.remove('glass-hovering', 'glass-nearby');
  }, []);

  const contextValue = useMemo(
    () => ({ register, unregister }),
    [register, unregister],
  );

  return (
    <GlassReflectContext.Provider value={contextValue}>
      {children}
    </GlassReflectContext.Provider>
  );
}

/**
 * 使用全局玻璃反射的 Hook
 */
export function useGlassReflect<T extends HTMLElement>(options?: { 
  reflectRange?: number; 
  reflectSize?: number;
  enabled?: boolean;
}): RefCallback<T> {
  const context = useContext(GlassReflectContext);
  const elementRef = useRef<T | null>(null);
  const reflectRange = options?.reflectRange;
  const reflectSize = options?.reflectSize;
  const enabled = options?.enabled ?? true;

  return useCallback((element: T | null) => {
    const previous = elementRef.current;
    if (previous === element) return;
    if (previous && context) {
      context.unregister(previous);
    }
    elementRef.current = element;
    if (element && context && enabled) {
      context.register(element, { reflectRange, reflectSize });
    }
  }, [context, enabled, reflectRange, reflectSize]);
}
