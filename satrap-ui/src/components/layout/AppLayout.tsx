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
      {menuOpen && <div className="fixed inset-0 z-50 md:hidden">
        <button className="absolute inset-0 bg-black/40 backdrop-blur-sm" aria-label="关闭导航菜单" onClick={() => setMenuOpen(false)} />
        <div className="relative h-full w-fit animate-rise">
          <Sidebar forceExpanded className="!fixed !inset-y-0 !left-0 !top-0 z-10 flex" onNavigate={() => setMenuOpen(false)} />
        </div>
      </div>}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <Header onOpenMenu={() => setMenuOpen(true)} />
        <main className="flex-1 min-h-0 p-3 md:p-6 overflow-auto custom-scrollbar">
          {/* key 让路由切换时重新播放进场动画 */}
          <div key={pathname} className="h-full animate-rise">
            <Outlet />
          </div>
        </main>
      </div>
      <CommandPalette />
    </div>
  );
}
