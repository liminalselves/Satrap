import { NavLink, useLocation } from 'react-router-dom';
import { MessageSquare, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { cn } from '@/utils/cn';
import { useGlassReflect } from '@/hooks/useGlassReflect';
import { usePrefsStore } from '@/stores/usePrefsStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { navGroups, findNav, type NavItem as NavEntry } from './nav';

// 导航项: 悬停时显出所在分组的染色反光 (由全局反光管理器驱动 --mouse-x/y)
function NavItem({ item, collapsed, active, onNavigate }: { item: NavEntry; collapsed: boolean; active: boolean; onNavigate?: () => void }) {
  const reflectRef = useGlassReflect<HTMLAnchorElement>({ reflectRange: 100, reflectSize: 90 });

  return (
    <NavLink
      ref={reflectRef}
      to={item.path}
      end
      onClick={onNavigate}
      title={collapsed ? item.label : undefined}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'nav-item group flex h-9 items-center gap-3 rounded-[10px] text-[13.5px] font-medium',
        collapsed ? 'mx-auto w-10 justify-center' : 'mx-2.5 px-3',
        active ? 'text-[var(--tint-fg)]' : 'text-text-secondary',
      )}
    >
      {/* 选中背景: 染色玻璃 */}
      <span
        aria-hidden
        className={cn(
          'absolute inset-0 -z-[1] rounded-[inherit] border transition-all duration-300',
          active
            ? 'border-[rgb(var(--tint)/0.22)] bg-[linear-gradient(135deg,rgb(var(--tint)/0.2),rgb(var(--tint)/0.06))] shadow-[0_6px_24px_-8px_rgb(var(--tint)/0.55),inset_0_1px_0_var(--glass-highlight)]'
            : 'border-transparent',
        )}
      />
      {!collapsed && (
        <span
          aria-hidden
          className={cn(
            'absolute left-0 top-1/2 w-[3px] -translate-y-1/2 rounded-r-full bg-[var(--tint-fg)] transition-all duration-300 [transition-timing-function:var(--ease-spring)]',
            active ? 'h-4 opacity-100' : 'h-0 opacity-0',
          )}
        />
      )}
      <item.icon className={cn('h-[17px] w-[17px] shrink-0 transition-transform duration-300 group-hover:scale-110', active ? 'text-[var(--tint-fg)]' : 'text-text-tertiary group-hover:text-[var(--tint-fg)]')} />
      {collapsed ? <span className="sr-only">{item.label}</span> : <span className="truncate">{item.label}</span>}
    </NavLink>
  );
}

export function Sidebar({ className, onNavigate, forceExpanded }: { className?: string; onNavigate?: () => void; forceExpanded?: boolean }) {
  const { pathname } = useLocation();
  const prefCollapsed = usePrefsStore((state) => state.sidebarCollapsed);
  const toggle = usePrefsStore((state) => state.toggleSidebar);
  const runState = useBackendStore((state) => state.runState);
  const collapsed = !forceExpanded && prefCollapsed;
  const current = findNav(pathname).item?.path;
  const sidebarRef = useGlassReflect<HTMLElement>({ reflectRange: 150 });
  const statusTone = runState === 'running' ? 'tint-green' : runState === 'stopped' ? '' : 'tint-orange';

  return (
    <aside
      ref={sidebarRef}
      aria-label="主导航"
      className={cn('glass-sidebar sticky top-2 flex shrink-0 flex-col transition-[width] duration-500 [transition-timing-function:var(--ease-out)]', className)}
      style={{ width: collapsed ? 'var(--sidebar-w-collapsed)' : 'var(--sidebar-w)' }}
    >
      <div className={cn('flex h-16 shrink-0 items-center', collapsed ? 'justify-center' : 'gap-3 px-4')}>
        <div className="relative grid h-9 w-9 shrink-0 place-items-center rounded-[11px] bg-[linear-gradient(135deg,#2b88d8,#0078d4_45%,#8b62b3)] shadow-[0_8px_24px_-6px_rgb(var(--rgb-accent)/0.8),inset_0_1px_0_rgba(255,255,255,0.35)]">
          <span className="text-[15px] font-bold text-white">S</span>
          <span className="absolute -bottom-0.5 -right-0.5 rounded-full bg-bg-base p-[2px]">
            <span className={cn('dot !h-2 !w-2', statusTone)} />
          </span>
        </div>
        {!collapsed && (
          <div className="min-w-0">
            <div className="text-[15px] font-semibold leading-5 text-text-primary">Satrap</div>
            <div className="text-[11px] text-text-tertiary">管理面板</div>
          </div>
        )}
      </div>

      <div className="divider mx-3" />

      <nav className="custom-scrollbar fade-edges flex-1 overflow-y-auto py-3">
        {navGroups.map((group, index) => (
          <div key={group.id} className={cn(`tint-${group.tint}`, index > 0 && 'mt-3')}>
            {collapsed ? (
              index > 0 && <div className="mx-auto mb-3 h-px w-6 bg-[var(--glass-border-strong)]" />
            ) : (
              <div className="mb-1 flex items-center gap-2 px-5 text-[11px] font-semibold tracking-[0.08em] text-text-disabled">
                <span className="h-1.5 w-1.5 rounded-full bg-[var(--tint-fg)] opacity-70" />
                {group.label}
              </div>
            )}
            <div className="space-y-0.5">
              {group.items.map((item) => (
                <NavItem key={item.path} item={item} collapsed={collapsed} active={current === item.path} onNavigate={onNavigate} />
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="divider mx-3" />

      <div className={cn('flex shrink-0 flex-col gap-1 p-2.5', collapsed && 'items-center')}>
        <div className="tint-accent">
          <NavItem item={{ path: '/chat', label: '打开对话', icon: MessageSquare, keywords: '' }} collapsed={collapsed} active={false} onNavigate={onNavigate} />
        </div>
        {!forceExpanded && (
          <div className={cn('mt-1 flex items-center', collapsed ? 'justify-center' : 'justify-between px-3')}>
            {!collapsed && <span className="text-[11px] text-text-disabled">Satrap Admin</span>}
            <button
              type="button"
              onClick={toggle}
              aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
              title={`${collapsed ? '展开侧栏' : '收起侧栏'} (Ctrl+B)`}
              className="grid h-8 w-8 place-items-center rounded-lg text-text-tertiary transition hover:bg-glass-hover hover:text-text-primary"
            >
              {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
