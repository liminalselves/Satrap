import { Outlet } from 'react-router-dom';
import { useState } from 'react';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { ToastContainer, useToasts } from '@/components/ui/Toast';

export function AppLayout() {
  const { toasts, closeToast } = useToasts();
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar className="hidden md:flex" />
      {menuOpen && <div className="fixed inset-0 z-50 md:hidden">
        <button className="absolute inset-0 bg-black/40" aria-label="关闭导航菜单" onClick={() => setMenuOpen(false)} />
        <Sidebar className="!fixed !inset-y-0 !left-0 !top-0 z-10 flex" onNavigate={() => setMenuOpen(false)} />
      </div>}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <Header onOpenMenu={() => setMenuOpen(true)} />
        <main className="flex-1 min-h-0 p-3 md:p-6 overflow-auto custom-scrollbar">
          <Outlet />
        </main>
      </div>
      <ToastContainer toasts={toasts} onClose={closeToast} />
    </div>
  );
}
