import { Link, useLocation } from 'react-router-dom';
import { ChevronRight, Menu, MessageSquare, Moon, Search, Sun } from 'lucide-react';
import { useTheme } from '@/hooks/useTheme';
import { useGlassReflect } from '@/hooks/useGlassReflect';
import { cn } from '@/utils/cn';
import { LogoutButton } from '@/components/common/LogoutButton';
import { findNav } from './nav';
import { StatusPill } from './StatusPill';
import { useCommandPalette } from './CommandPalette';

export function Header({ onOpenMenu }: { onOpenMenu?: () => void }) {
  const { theme, toggleTheme } = useTheme();
  const { pathname } = useLocation();
  const { group, item } = findNav(pathname);
  const openPalette = useCommandPalette((state) => state.setOpen);
  const headerRef = useGlassReflect<HTMLElement>({
    reflectRange: 150,
  });

  return (
    <header ref={headerRef} className="h-14 glass-header sticky top-2 z-40 flex items-center gap-3 px-3">
      <button className="theme-toggle md:hidden" onClick={onOpenMenu} aria-label="打开导航菜单"><Menu className="h-4 w-4" /></button>

      {/* 面包屑: 分组 › 当前页 */}
      <nav aria-label="面包屑" className="flex min-w-0 items-center gap-1.5 pl-1 text-sm">
        {group && (
          <>
            <span className={cn(`tint-${group.tint}`, 'hidden items-center gap-1.5 text-text-tertiary sm:flex')}>
              <span className="h-1.5 w-1.5 rounded-full bg-[var(--tint-fg)]" />
              {group.label}
            </span>
            <ChevronRight className="hidden h-3.5 w-3.5 text-text-disabled sm:block" />
          </>
        )}
        <span aria-current="page" className="truncate font-semibold text-text-primary">{item?.label ?? '管理面板'}</span>
      </nav>

      <StatusPill />

      <div className="ml-auto flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => openPalette(true)}
          aria-label="打开命令面板"
          className="glass-button hidden h-8 w-56 !justify-start gap-2 !px-2.5 text-[13px] !font-normal text-text-tertiary lg:inline-flex"
        >
          <Search className="h-3.5 w-3.5" />
          搜索页面或操作…
          <span className="ml-auto flex gap-0.5"><kbd>Ctrl</kbd><kbd>K</kbd></span>
        </button>
        <button type="button" className="theme-toggle lg:hidden" onClick={() => openPalette(true)} aria-label="搜索" title="搜索 (Ctrl+K)">
          <Search className="h-4 w-4 text-text-secondary" />
        </button>
        <div className="mx-1 hidden h-5 w-px bg-[var(--glass-border-strong)] sm:block" />
        <LogoutButton />
        <Link to="/chat" className="theme-toggle" title="对话" aria-label="打开对话">
          <MessageSquare className="h-4 w-4 text-text-secondary" />
        </Link>
        <button
          onClick={(event) => toggleTheme({ x: event.clientX, y: event.clientY })}
          className="theme-toggle"
          title={theme === 'dark' ? '切换到亮色模式' : '切换到暗色模式'}
        >
          <span className="relative h-4 w-4">
            <Sun className={cn('absolute inset-0 h-4 w-4 text-warning transition-all duration-500 [transition-timing-function:var(--ease-spring)]', theme === 'dark' ? 'rotate-0 scale-100 opacity-100' : '-rotate-90 scale-0 opacity-0')} />
            <Moon className={cn('absolute inset-0 h-4 w-4 text-accent transition-all duration-500 [transition-timing-function:var(--ease-spring)]', theme === 'light' ? 'rotate-0 scale-100 opacity-100' : 'rotate-90 scale-0 opacity-0')} />
          </span>
        </button>
      </div>
    </header>
  );
}
