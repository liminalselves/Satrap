import { createContext, useContext, useState, ReactNode, useCallback, useLayoutEffect, useRef, type KeyboardEvent } from 'react';
import { cn } from '@/utils/cn';

interface TabsContextValue {
  activeTab: string;
  setActiveTab: (value: string) => void;
}

const TabsContext = createContext<TabsContextValue | null>(null);

export interface TabsProps {
  defaultValue?: string;
  // 受控模式: 传入 value 后高亮完全由调用方决定 (拒绝切换等场景无需重挂载)
  value?: string;
  children: ReactNode;
  className?: string;
  onValueChange?: (value: string) => void;
}

export function Tabs({
  defaultValue,
  value,
  children,
  className,
  onValueChange,
}: TabsProps) {
  const [innerTab, setInnerTab] = useState(defaultValue ?? value ?? '');
  const activeTab = value ?? innerTab;

  const setActiveTab = useCallback((next: string) => {
    setInnerTab(next);
    onValueChange?.(next);
  }, [onValueChange]);

  return (
    <TabsContext.Provider value={{ activeTab, setActiveTab }}>
      <div className={className}>{children}</div>
    </TabsContext.Provider>
  );
}

/** 玻璃分段控件: 选中胶囊在选项间滑动, 方向键切换 */
export function TabsList({ children, className }: { children: ReactNode; className?: string }) {
  const context = useContext(TabsContext);
  const listRef = useRef<HTMLDivElement>(null);
  const [indicator, setIndicator] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const activeTab = context?.activeTab;

  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const update = () => {
      const el = list.querySelector<HTMLElement>('[data-tab-active="true"]');
      setIndicator(el ? { left: el.offsetLeft, top: el.offsetTop, width: el.offsetWidth, height: el.offsetHeight } : null);
    };
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(list);
    return () => observer.disconnect();
  }, [activeTab]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const tabs = Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>('[data-tab-value]:not(:disabled)') ?? []);
    const index = tabs.findIndex((tab) => tab === document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    tabs[next].click();
  };

  return (
    <div
      ref={listRef}
      onKeyDown={onKeyDown}
      className={cn('relative inline-flex max-w-full flex-wrap items-center gap-0.5 rounded-[12px] border border-glass-border bg-glass p-1 backdrop-blur-md', className)}
    >
      {indicator && (
        <span
          aria-hidden
          className="pointer-events-none absolute rounded-[9px] border border-[rgb(var(--rgb-accent)/0.28)] bg-[rgb(var(--rgb-accent)/0.14)] shadow-[0_4px_16px_-4px_rgb(var(--rgb-accent)/0.45),inset_0_1px_0_var(--glass-highlight)]"
          style={{ ...indicator, transition: 'left 380ms var(--ease-out), top 380ms var(--ease-out), width 380ms var(--ease-out)' }}
        />
      )}
      {children}
    </div>
  );
}

export function TabsTrigger({
  value,
  children,
  className,
}: {
  value: string;
  children: ReactNode;
  className?: string;
}) {
  const context = useContext(TabsContext);
  if (!context) throw new Error('TabsTrigger must be used within Tabs');

  const { activeTab, setActiveTab } = context;
  const isActive = activeTab === value;

  return (
    <button
      type="button"
      data-tab-value={value}
      data-tab-active={isActive}
      aria-current={isActive ? 'true' : undefined}
      onClick={() => setActiveTab(value)}
      className={cn(
        'relative z-[1] inline-flex h-8 items-center gap-2 rounded-[9px] px-3.5 text-sm font-medium transition-colors duration-200',
        isActive ? 'text-accent' : 'text-text-tertiary hover:text-text-primary',
        className
      )}
    >
      {children}
    </button>
  );
}

export function TabsContent({
  value,
  children,
  className,
  forceMount = false,
}: {
  value: string;
  children: ReactNode;
  className?: string;
  // 保持挂载: 非活动页仅隐藏不卸载, 用于持有未保存草稿的面板
  forceMount?: boolean;
}) {
  const context = useContext(TabsContext);
  if (!context) throw new Error('TabsContent must be used within Tabs');

  const { activeTab } = context;
  if (activeTab !== value && !forceMount) return null;

  return <div hidden={activeTab !== value} className={cn('pt-4 animate-rise', className)}>{children}</div>;
}
