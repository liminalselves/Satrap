import { Outlet, useLocation } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { CommandPalette } from './CommandPalette';

export function AppLayout() {
  const [menuOpen, setMenuOpen] = useState(false);
  const { pathname } = useLocation();

  useEffect(() => setMenuOpen(false), [pathname]);

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar className="hidden md:flex" />
      {menuOpen && (
        <div className="fixed inset-0 z-[100] md:hidden">
          <button className="absolute inset-0 bg-black/40 backdrop-blur-sm animate-fade" aria-label="关闭导航菜单" onClick={() => setMenuOpen(false)} />
          <div className="relative h-full w-fit" style={{ animation: 'rise-in 360ms var(--ease-out) both' }}>
            <Sidebar forceExpanded className="surface-strong" onNavigate={() => setMenuOpen(false)} />
          </div>
        </div>
      )}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Header onOpenMenu={() => setMenuOpen(true)} />
        <main className="scroll min-h-0 flex-1">
          {/* key 让路由切换时重新播放进场动画; h-full 供日志等全高页面使用 */}
          <div key={pathname} className="mx-auto h-full w-full max-w-[1400px] px-3 py-5 md:px-6 md:py-6">
            <Outlet />
          </div>
        </main>
      </div>
      <CommandPalette />
    </div>
  );
}
