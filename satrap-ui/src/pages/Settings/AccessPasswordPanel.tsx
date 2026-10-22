import { useCallback, useEffect, useState } from 'react';
import { KeyRound, RefreshCw } from 'lucide-react';
import { getPasswordStatus, setDashboardPassword } from '@/api/auth';
import { getControlApiUrl } from '@/utils/constants';
import { Button } from '@/components/ui/Button';
import { PasswordInput } from '@/components/ui/PasswordInput';
import { toast } from '@/components/ui/Toast';

export function AccessPasswordPanel({ active }: { active: boolean }) {
  const [required, setRequired] = useState<boolean | null>(null);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const baseUrl = getControlApiUrl();

  const load = useCallback(async () => {
    if (!active) return;
    try {
      setRequired(await getPasswordStatus(baseUrl));
      setLoadError('');
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : '读取失败');
    }
  }, [active, baseUrl]);

  useEffect(() => { void load(); }, [load]);

  const submit = async (mode: 'set' | 'clear') => {
    if (busy) return;
    if (mode === 'set') {
      if (newPassword.length < 8) { setError('新密码至少需要 8 个字符'); return; }
      if (newPassword !== confirmPassword) { setError('两次输入的新密码不一致'); return; }
    }
    setBusy(true);
    setError('');
    try {
      const enabled = await setDashboardPassword(baseUrl, required ? currentPassword : null, mode === 'set' ? newPassword : null);
      setRequired(enabled);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      toast('success', enabled ? '访问密码已更新, 下次进入前端需要密码' : '访问密码已清除, 前端恢复免密进入');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="访问密码设置" className="space-y-5 pt-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-medium text-text-primary">访问密码</h2>
          <p className="text-sm text-text-secondary mt-1">设置后进入前端需要输入密码; API 与 api-token 访问不受影响。未设置时前端免密进入。</p>
        </div>
        <Button variant="ghost" onClick={() => void load()} disabled={busy}>
          <RefreshCw className="h-4 w-4 mr-2" />刷新状态
        </Button>
      </div>
      {error && <div role="alert" className="rounded-sm border border-error/30 bg-error/10 p-3 text-sm text-error">{error}</div>}
      {loadError && <div role="alert" className="rounded-sm border border-error/30 bg-error/10 p-3 text-sm text-error">读取访问密码状态失败: {loadError}</div>}
      <div className="rounded-sm bg-glass p-3 text-sm">
        <span className="text-text-tertiary">当前状态: </span>
        {required === null ? '尚未读取' : required ? '已启用密码' : '免密进入'}
      </div>
      {required && (
        <div>
          <label htmlFor="access-current" className="block mb-1 text-sm text-text-secondary">当前密码</label>
          <PasswordInput id="access-current" autoComplete="current-password" value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)} disabled={busy} />
        </div>
      )}
      <div className="grid gap-5 sm:grid-cols-2">
        <div>
          <label htmlFor="access-new" className="block mb-1 text-sm text-text-secondary">{required ? '新密码' : '设置密码'}</label>
          <PasswordInput id="access-new" autoComplete="new-password" value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)} disabled={busy} placeholder="至少 8 个字符" />
        </div>
        <div>
          <label htmlFor="access-confirm" className="block mb-1 text-sm text-text-secondary">确认新密码</label>
          <PasswordInput id="access-confirm" autoComplete="new-password" value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)} disabled={busy} />
        </div>
      </div>
      <div className="flex flex-wrap gap-3">
        <Button variant="primary" onClick={() => void submit('set')} disabled={busy || required === null || !newPassword || (required === true && !currentPassword)}>
          <KeyRound className="h-4 w-4 mr-2" />{busy ? '保存中…' : required ? '修改密码' : '启用密码'}
        </Button>
        {required === true && (
          <Button variant="danger" onClick={() => void submit('clear')} disabled={busy || !currentPassword}>
            {busy ? '处理中…' : '清除密码'}
          </Button>
        )}
      </div>
      <p className="text-sm text-text-tertiary">密码以 PBKDF2-SHA256 加盐哈希保存在配置文件, 明文不出现在存储中。忘记密码可在服务器上编辑配置文件的 api.dashboard_password 字段。</p>
    </section>
  );
}
