import { NavLink, useLocation } from 'react-router-dom';
import { MessageSquare, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { cn } from '@/utils/cn';
import { usePrefsStore } from '@/stores/usePrefsStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { useServiceHealthStore } from '@/stores/useServiceHealthStore';
import { StatusDot, Tooltip } from '@/components/kit';
import { Logo } from '@/components/common/Logo';
import { navGroups, findNav, type NavItem as NavEntry } from './nav';

const APPROVALS_PATH = '/platforms/approvals';

// 导航项: 悬停显色由全局反光引擎驱动 (.nav-item)
function NavItem({ item, collapsed, active, badge, onNavigate }: { item: NavEntry; collapsed: boolean; active: boolean; badge?: number | null; onNavigate?: () => void }) {
  const link = (
    <NavLink
      to={item.path}
      end
      onClick={onNavigate}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'nav-item group relative flex h-9 items-center gap-3 rounded-[10px] text-[13.5px] font-medium',
        collapsed ? 'mx-auto w-10 justify-center' : 'mx-2.5 px-3',
        active ? 'text-tint' : 'text-fg-2',
      )}
    >
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
            'absolute left-0 top-1/2 w-[3px] -translate-y-1/2 rounded-r-full bg-tint transition-all duration-300 [transition-timing-function:var(--ease-spring)]',
            active ? 'h-4 opacity-100' : 'h-0 opacity-0',
          )}
        />
      )}
      <item.icon className={cn('relative h-[17px] w-[17px] flex-none transition-transform duration-300 group-hover:scale-110', active ? 'text-tint' : 'text-fg-3 group-hover:text-tint')} />
      {collapsed ? <span className="sr-only">{item.label}</span> : <span className="relative truncate">{item.label}</span>}
      {!!badge && (
        collapsed
          ? <span aria-hidden className="absolute right-1 top-1 h-2 w-2 rounded-full bg-tint shadow-[0_0_8px_rgb(var(--tint))]" />
          : <span aria-hidden className="num relative ml-auto rounded-full bg-[rgb(var(--tint)/0.18)] px-1.5 text-[11px] leading-[18px] text-tint">{badge > 99 ? '99+' : badge}</span>
      )}
    </NavLink>
  );
  return collapsed ? <Tooltip content={badge ? `${item.label} · ${badge} 项待处理` : item.label} side="right">{link}</Tooltip> : link;
}

export function Sidebar({ className, onNavigate, forceExpanded }: { className?: string; onNavigate?: () => void; forceExpanded?: boolean }) {
  const { pathname } = useLocation();
  const prefCollapsed = usePrefsStore((state) => state.sidebarCollapsed);
  const toggle = usePrefsStore((state) => state.toggleSidebar);
  const runState = useBackendStore((state) => state.runState);
  const pending = useServiceHealthStore((state) => state.pendingApprovals);
  const control = useServiceHealthStore((state) => state.services.control.health);
  const collapsed = !forceExpanded && prefCollapsed;
  const current = findNav(pathname).item?.path;
  const build = control?.version ? `v${control.version}` : control?.revision ? `build ${control.revision.slice(0, 7)}` : 'Satrap Admin';

  return (
    <aside
      aria-label="主导航"
      className={cn('surface m-2 flex h-[calc(100vh-16px)] flex-none flex-col !rounded-[20px] transition-[width] duration-500 [transition-timing-function:var(--ease-out)]', className)}
      style={{ width: collapsed ? 'var(--sidebar-w-collapsed)' : 'var(--sidebar-w)' }}
    >
      <div className={cn('flex h-16 flex-none items-center', collapsed ? 'justify-center' : 'gap-3 px-4')}>
        <div className="relative h-9 w-9 flex-none">
          <Logo className="h-9 w-9" />
          <span className="absolute -bottom-0.5 -right-0.5 rounded-full bg-[var(--bg-base)] p-[2px]">
            <StatusDot tone={runState === 'running' ? 'green' : runState === 'stopped' ? 'neutral' : 'orange'} className="!h-2 !w-2" />
          </span>
        </div>
        {!collapsed && (
          <div className="min-w-0 animate-fade">
            <div className="display text-[15px] font-semibold leading-5 text-fg">Satrap</div>
            <div className="text-[11px] text-fg-3">管理面板</div>
          </div>
        )}
      </div>

      <div className="divider mx-3" />

      <nav className="scroll fade-edges flex-1 py-3">
        {navGroups.map((group, index) => (
          <div key={group.id} className={cn(`tint-${group.tint}`, index > 0 && 'mt-3')}>
            {collapsed ? (
              index > 0 && <div className="mx-auto mb-3 h-px w-6 bg-[var(--glass-border-strong)]" />
            ) : (
              <div className="mb-1 flex items-center gap-2 px-5 text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-4">
                <span className="h-1.5 w-1.5 rounded-full bg-tint opacity-70" />
                {group.label}
              </div>
            )}
            <div className="space-y-0.5">
              {group.items.map((item) => (
                <NavItem key={item.path} item={item} collapsed={collapsed} active={current === item.path || (current === '/docs' && item.path === '/settings')}
                  badge={item.path === APPROVALS_PATH ? pending : undefined} onNavigate={onNavigate} />
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="divider mx-3" />

      <div className={cn('flex flex-none flex-col gap-1 p-2.5', collapsed && 'items-center')}>
        <div className="tint-accent">
          <NavItem item={{ path: '/chat', label: '打开对话', icon: MessageSquare, keywords: '' }} collapsed={collapsed} active={false} onNavigate={onNavigate} />
        </div>
        {!forceExpanded && (
          <div className={cn('mt-1 flex items-center', collapsed ? 'justify-center' : 'justify-between px-3')}>
            {!collapsed && <span className="num truncate text-[11px] text-fg-4">{build}</span>}
            <Tooltip content={collapsed ? '展开侧栏' : '收起侧栏'} shortcut="Ctrl B" side="right">
              <button
                type="button"
                onClick={toggle}
                aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
                className="grid h-8 w-8 place-items-center rounded-lg text-fg-3 transition hover:bg-glass-hover hover:text-fg"
              >
                {collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
              </button>
            </Tooltip>
          </div>
        )}
      </div>
    </aside>
  );
}
