import { useLocation } from 'react-router-dom';
import { ChevronRight, Menu, Moon, Search, Sun } from 'lucide-react';
import { useTheme } from '@/hooks/useTheme';
import { cn } from '@/utils/cn';
import { LogoutButton } from '@/components/common/LogoutButton';
import { IconButton } from '@/components/kit';
import { findNav } from './nav';
import { StatusPill } from './StatusPill';
import { useCommandPalette } from './CommandPalette';

export function Header({ onOpenMenu }: { onOpenMenu?: () => void }) {
  const { theme, toggleTheme } = useTheme();
  const { pathname } = useLocation();
  const { group, item } = findNav(pathname);
  const openPalette = useCommandPalette((state) => state.setOpen);

  return (
    <header className="surface z-40 mx-2 mt-2 flex h-14 flex-none items-center gap-3 !rounded-2xl px-3 md:ml-0">
      <IconButton label="打开导航菜单" className="md:hidden" onClick={onOpenMenu}><Menu className="h-4 w-4" /></IconButton>

      {/* 面包屑: 分组 › 当前页 */}
      <nav aria-label="面包屑" className="flex min-w-0 items-center gap-1.5 pl-1 text-sm">
        {group && (
          <>
            <span className={cn(`tint-${group.tint}`, 'hidden items-center gap-1.5 text-fg-3 sm:flex')}>
              <span className="h-1.5 w-1.5 rounded-full bg-tint" />
              {group.label}
            </span>
            <ChevronRight className="hidden h-3.5 w-3.5 text-fg-4 sm:block" />
          </>
        )}
        <span aria-current="page" className="truncate font-semibold text-fg">{item?.label ?? '管理面板'}</span>
      </nav>

      <div className="ml-1"><StatusPill /></div>

      <div className="ml-auto flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => openPalette(true)}
          aria-label="打开命令面板"
          className="ctl ctl-sm hidden w-56 !justify-start !px-2.5 text-fg-3 lg:inline-flex"
        >
          <Search className="h-3.5 w-3.5" />
          <span className="text-[13px]">搜索页面或操作…</span>
          <span className="ml-auto flex gap-0.5"><kbd>Ctrl</kbd><kbd>K</kbd></span>
        </button>
        <IconButton label="搜索" shortcut="Ctrl K" className="lg:hidden" onClick={() => openPalette(true)}><Search className="h-4 w-4" /></IconButton>
        <div className="mx-1 hidden h-5 w-px bg-[var(--glass-border-strong)] sm:block" />
        <IconButton
          label={theme === 'dark' ? '切换到亮色模式' : '切换到暗色模式'}
          onClick={(event) => toggleTheme({ x: event.clientX, y: event.clientY })}
        >
          <span className="relative h-4 w-4">
            <Sun className={cn('absolute inset-0 h-4 w-4 text-orange transition-all duration-500 [transition-timing-function:var(--ease-spring)]', theme === 'dark' ? 'rotate-0 scale-100 opacity-100' : '-rotate-90 scale-0 opacity-0')} />
            <Moon className={cn('absolute inset-0 h-4 w-4 text-accent transition-all duration-500 [transition-timing-function:var(--ease-spring)]', theme === 'light' ? 'rotate-0 scale-100 opacity-100' : 'rotate-90 scale-0 opacity-0')} />
          </span>
        </IconButton>
        <LogoutButton />
      </div>
    </header>
  );
}
