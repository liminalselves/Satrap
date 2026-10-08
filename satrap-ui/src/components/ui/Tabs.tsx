import { createContext, useContext, useState, ReactNode, useCallback } from 'react';
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

export function TabsList({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('flex gap-1 border-b border-border-glass', className)}>
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
      onClick={() => setActiveTab(value)}
      className={cn(
        'px-4 py-2 text-sm font-medium transition-all duration-200 rounded-t-md',
        'border-b-2 -mb-px',
        isActive
          ? 'text-accent border-accent bg-glass'
          : 'text-text-secondary border-transparent hover:text-text-primary hover:bg-glass-hover',
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

  return <div hidden={activeTab !== value} className={cn('pt-4 animate-fade-in', className)}>{children}</div>;
}
