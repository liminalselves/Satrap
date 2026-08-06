import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { Header } from './Header';
import { ToastContainer, useToasts } from '@/components/ui/Toast';

export function AppLayout() {
  const { toasts, closeToast } = useToasts();

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <Header />
        <main className="flex-1 p-6 overflow-auto custom-scrollbar">
          <Outlet />
        </main>
      </div>
      <ToastContainer toasts={toasts} onClose={closeToast} />
    </div>
  );
}
