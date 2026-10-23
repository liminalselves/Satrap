import { useState } from 'react';
import { LogOut } from 'lucide-react';
import { logoutBrowser } from '@/api/auth';
import { toast } from '@/components/ui/Toast';

export function LogoutButton() {
  const [busy, setBusy] = useState(false);
  return <button className="theme-toggle" title="退出登录" disabled={busy} onClick={async () => {
    setBusy(true);
    try { await logoutBrowser(); }
    catch (error) { toast('error', error instanceof Error ? error.message : '退出失败'); }
    finally { setBusy(false); }
  }}><LogOut className="h-4 w-4 text-text-secondary" /></button>;
}
