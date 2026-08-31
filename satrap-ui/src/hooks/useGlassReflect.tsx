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
import {
  calculateReflectState,
  copyReflectRect,
  getPointerCellKey,
  getReflectCellKeys,
  REFLECT_SPATIAL_CELL_SIZE,
  type ReflectRect,
} from './glassReflectGeometry';
import { createAnimationFrameLimiter, MAX_UI_FRAME_RATE } from '@/utils/frameLimiter';

/**
 * 全局玻璃反射管理器
 * 跟踪鼠标位置, 为所有注册的玻璃元素提供反射效果
 */

interface GlassElement {
  element: HTMLElement;
  reflectRange: number;
  reflectSize: number;
  rect: ReflectRect | null;
  visible: boolean;
  cellKeys: string[];
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
  item: GlassElement,
  mouseX: number,
  mouseY: number,
): boolean {
  if (!item.rect) return false;
  const state = calculateReflectState(item.rect, mouseX, mouseY, item.reflectRange);
  item.element.style.setProperty('--mouse-x', `${state.x}px`);
  item.element.style.setProperty('--mouse-y', `${state.y}px`);
  if (state.isHovering) {
    item.element.classList.add('glass-hovering');
    item.element.classList.remove('glass-nearby');
  } else if (state.isInRange) {
    item.element.classList.remove('glass-hovering');
    item.element.classList.add('glass-nearby');
  } else {
    item.element.classList.remove('glass-hovering', 'glass-nearby');
  }
  return state.isInRange;
}

export function GlassReflectProvider({ children }: { children: ReactNode }) {
  const elementsRef = useRef<Map<HTMLElement, GlassElement>>(new Map());
  const spatialIndexRef = useRef<Map<string, Set<GlassElement>>>(new Map());
  const activeElementsRef = useRef<Set<GlassElement>>(new Set());
  const geometryDirtyAllRef = useRef(false);
  const mousePosRef = useRef<{ x: number; y: number }>({ x: -1000, y: -1000 });
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  const intersectionObserverRef = useRef<IntersectionObserver | null>(null);
  const frameLimiter = useMemo(
    () => createAnimationFrameLimiter({ maxFps: MAX_UI_FRAME_RATE }),
    [],
  );

  const removeFromSpatialIndex = useCallback((item: GlassElement) => {
    for (const key of item.cellKeys) {
      const bucket = spatialIndexRef.current.get(key);
      bucket?.delete(item);
      if (bucket?.size === 0) spatialIndexRef.current.delete(key);
    }
    item.cellKeys = [];
  }, []);

  const updateSpatialIndex = useCallback((item: GlassElement, rect: ReflectRect) => {
    removeFromSpatialIndex(item);
    item.rect = rect;
    if (!item.visible) return;
    item.cellKeys = getReflectCellKeys(rect, item.reflectRange);
    for (const key of item.cellKeys) {
      let bucket = spatialIndexRef.current.get(key);
      if (!bucket) {
        bucket = new Set();
        spatialIndexRef.current.set(key, bucket);
      }
      bucket.add(item);
    }
  }, [removeFromSpatialIndex]);

  // 仅在布局发生变化时读取元素矩形并重建空间索引
  const refreshGeometry = useCallback(() => {
    if (!geometryDirtyAllRef.current) return;
    geometryDirtyAllRef.current = false;
    const targets = [...elementsRef.current.values()].filter((item) => item.visible);
    for (const item of targets) {
      if (!elementsRef.current.has(item.element)) continue;
      updateSpatialIndex(item, copyReflectRect(item.element.getBoundingClientRect()));
    }
  }, [updateSpatialIndex]);

  // 指针移动只查询缓存和当前空间分区, 不读取布局
  const updatePointerElements = useCallback(() => {
    const { x: mouseX, y: mouseY } = mousePosRef.current;
    const candidates = spatialIndexRef.current.get(getPointerCellKey(mouseX, mouseY)) ?? new Set();
    const nextActive = new Set<GlassElement>();
    for (const item of candidates) {
      if (item.visible && updateReflectState(item, mouseX, mouseY)) {
        nextActive.add(item);
      }
    }
    for (const item of activeElementsRef.current) {
      if (!nextActive.has(item)) {
        item.element.classList.remove('glass-hovering', 'glass-nearby');
      }
    }
    activeElementsRef.current = nextActive;
  }, []);

  const runFrame = useCallback(() => {
    refreshGeometry();
    updatePointerElements();
  }, [refreshGeometry, updatePointerElements]);

  // 几何和指针更新共用一个 90 FPS 动画帧调度器
  const scheduleUpdate = useCallback(() => {
    frameLimiter.schedule(runFrame);
  }, [frameLimiter, runFrame]);

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

    const handleGeometryChange = () => {
      geometryDirtyAllRef.current = true;
      scheduleUpdate();
    };

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
      frameLimiter.cancel();
    };
  }, [frameLimiter, scheduleUpdate]);

  useEffect(() => {
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      geometryDirtyAllRef.current = true;
      scheduleUpdate();
    });
    resizeObserverRef.current = observer;
    elementsRef.current.forEach(({ element }) => observer.observe(element));
    return () => {
      observer.disconnect();
      resizeObserverRef.current = null;
    };
  }, [scheduleUpdate]);

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const item = elementsRef.current.get(entry.target as HTMLElement);
        if (!item) continue;
        item.visible = entry.isIntersecting;
        if (item.visible) {
          updateSpatialIndex(item, copyReflectRect(entry.boundingClientRect));
        } else {
          removeFromSpatialIndex(item);
          activeElementsRef.current.delete(item);
          item.element.classList.remove('glass-hovering', 'glass-nearby');
        }
      }
      scheduleUpdate();
    }, { rootMargin: `${REFLECT_SPATIAL_CELL_SIZE}px` });
    intersectionObserverRef.current = observer;
    elementsRef.current.forEach(({ element }) => observer.observe(element));
    return () => {
      observer.disconnect();
      intersectionObserverRef.current = null;
    };
  }, [removeFromSpatialIndex, scheduleUpdate, updateSpatialIndex]);

  // 注册元素
  const register = useCallback((
    element: HTMLElement, 
    options?: { reflectRange?: number; reflectSize?: number }
  ) => {
    elementsRef.current.set(element, {
      element,
      reflectRange: options?.reflectRange ?? DEFAULT_REFLECT_RANGE,
      reflectSize: options?.reflectSize ?? DEFAULT_REFLECT_SIZE,
      rect: null,
      visible: true,
      cellKeys: [],
    });
    element.style.setProperty('--reflect-size', `${options?.reflectSize ?? DEFAULT_REFLECT_SIZE}px`);
    resizeObserverRef.current?.observe(element);
    intersectionObserverRef.current?.observe(element);
    geometryDirtyAllRef.current = true;
    scheduleUpdate();
  }, [scheduleUpdate]);

  // 注销元素
  const unregister = useCallback((element: HTMLElement) => {
    const item = elementsRef.current.get(element);
    resizeObserverRef.current?.unobserve(element);
    intersectionObserverRef.current?.unobserve(element);
    if (item) {
      activeElementsRef.current.delete(item);
      removeFromSpatialIndex(item);
    }
    elementsRef.current.delete(element);
    element.classList.remove('glass-hovering', 'glass-nearby');
    geometryDirtyAllRef.current = true;
    scheduleUpdate();
  }, [removeFromSpatialIndex, scheduleUpdate]);

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
