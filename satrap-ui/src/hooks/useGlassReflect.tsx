import { useEffect, useRef, useCallback, createContext, useContext, ReactNode } from 'react';

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

export function GlassReflectProvider({ children }: { children: ReactNode }) {
  const elementsRef = useRef<Map<HTMLElement, GlassElement>>(new Map());
  const mousePosRef = useRef<{ x: number; y: number }>({ x: -1000, y: -1000 });
  const rafRef = useRef<number | null>(null);

  // 更新所有元素的反射状态
  const updateAllElements = useCallback(() => {
    const { x: mouseX, y: mouseY } = mousePosRef.current;

    elementsRef.current.forEach(({ element, reflectRange }) => {
      const rect = element.getBoundingClientRect();
      
      // 计算鼠标相对于元素的位置
      const x = mouseX - rect.left;
      const y = mouseY - rect.top;
      
      // 计算鼠标到元素边缘的最短距离
      const closestX = Math.max(rect.left, Math.min(mouseX, rect.right));
      const closestY = Math.max(rect.top, Math.min(mouseY, rect.bottom));
      const distanceToEdge = Math.sqrt(
        Math.pow(mouseX - closestX, 2) + Math.pow(mouseY - closestY, 2)
      );
      
      // 检查是否在影响范围内
      const isInRange = distanceToEdge < reflectRange;
      const isHovering = mouseX >= rect.left && mouseX <= rect.right && 
                         mouseY >= rect.top && mouseY <= rect.bottom;
      
      // 更新 CSS 变量
      element.style.setProperty('--mouse-x', `${x}px`);
      element.style.setProperty('--mouse-y', `${y}px`);
      
      // 设置状态类
      if (isHovering) {
        element.classList.add('glass-hovering');
        element.classList.remove('glass-nearby');
      } else if (isInRange) {
        element.classList.remove('glass-hovering');
        element.classList.add('glass-nearby');
      } else {
        element.classList.remove('glass-hovering', 'glass-nearby');
      }
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
    const handleMouseMove = (e: MouseEvent) => {
      mousePosRef.current = { x: e.clientX, y: e.clientY };
      scheduleUpdate();
    };

    const handleMouseLeave = () => {
      mousePosRef.current = { x: -1000, y: -1000 };
      scheduleUpdate();
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.documentElement.addEventListener('mouseleave', handleMouseLeave);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.documentElement.removeEventListener('mouseleave', handleMouseLeave);
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
      }
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
    
    // 设置反光大小变量
    if (options?.reflectSize) {
      element.style.setProperty('--reflect-size', `${options.reflectSize}px`);
    }
  }, []);

  // 注销元素
  const unregister = useCallback((element: HTMLElement) => {
    elementsRef.current.delete(element);
  }, []);

  return (
    <GlassReflectContext.Provider value={{ register, unregister }}>
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
}) {
  const ref = useRef<T>(null);
  const context = useContext(GlassReflectContext);

  useEffect(() => {
    const element = ref.current;
    if (!element || !context) return;

    context.register(element, options);
    return () => context.unregister(element);
  }, [context, options?.reflectRange, options?.reflectSize]);

  return ref;
}

/**
 * 独立使用的玻璃反射 Hook(不需要 Provider)
 * 适用于单个元素或小组件
 */
export function useStandaloneGlassReflect<T extends HTMLElement>(options?: {
  reflectRange?: number;
  reflectSize?: number;
}) {
  const ref = useRef<T>(null);
  const reflectRange = options?.reflectRange ?? DEFAULT_REFLECT_RANGE;

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    const handleMouseMove = (e: MouseEvent) => {
      const rect = element.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      
      // 计算鼠标到元素边缘的最短距离
      const closestX = Math.max(rect.left, Math.min(e.clientX, rect.right));
      const closestY = Math.max(rect.top, Math.min(e.clientY, rect.bottom));
      const distanceToEdge = Math.sqrt(
        Math.pow(e.clientX - closestX, 2) + Math.pow(e.clientY - closestY, 2)
      );
      
      const isInRange = distanceToEdge < reflectRange;
      const isHovering = e.clientX >= rect.left && e.clientX <= rect.right && 
                         e.clientY >= rect.top && e.clientY <= rect.bottom;
      
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
    };

    const handleMouseLeave = () => {
      element.classList.remove('glass-hovering', 'glass-nearby');
    };

    // 监听全局鼠标移动
    document.addEventListener('mousemove', handleMouseMove);
    element.addEventListener('mouseleave', handleMouseLeave);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      element.removeEventListener('mouseleave', handleMouseLeave);
    };
  }, [reflectRange]);

  return ref;
}
