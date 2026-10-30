import { useState } from 'react';
import { LogOut } from 'lucide-react';
import { logoutBrowser } from '@/api/auth';
import { toast } from '@/components/ui/Toast';
import { IconButton } from '@/components/kit';

export function LogoutButton() {
  const [busy, setBusy] = useState(false);
  return (
    <IconButton label="退出登录" loading={busy} onClick={async () => {
      setBusy(true);
      try { await logoutBrowser(); }
      catch (error) { toast('error', error instanceof Error ? error.message : '退出失败'); }
      finally { setBusy(false); }
    }}>
      <LogOut className="h-4 w-4" />
    </IconButton>
  );
}
