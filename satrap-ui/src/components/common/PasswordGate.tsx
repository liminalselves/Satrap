import { useState } from 'react';
import { passwordLogin } from '@/api/auth';
import { PasswordInput } from '@/components/ui/PasswordInput';
import { useApiSessionStore } from '@/stores/useApiSessionStore';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';

export function PasswordGate() {
  const sessions = useApiSessionStore((state) => state.sessions);
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const services = [getApiBaseUrl(), getControlApiUrl(), getChatApiUrl()]
    .map((url) => url.replace(/\/$/, ''));
  const required = services.filter((url) => sessions[url]?.state === 'password_required');
  // 任一服务尚未得出状态(连接中或密码探测中)时也先盖住, 避免密码页出现前短暂露出前端
  const unsettled = services.some((url) => sessions[url]?.state === 'checking' || !sessions[url]);
  if (!required.length && !unsettled) return null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError('');
    const results = await Promise.allSettled(required.map((url) => passwordLogin(url, password)));
    setBusy(false);
    const failure = results.find((result) => result.status === 'rejected') as PromiseRejectedResult | undefined;
    if (failure) setError(failure.reason instanceof Error ? failure.reason.message : '登录失败');
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-bg-base">
      {required.length > 0 ? (
        <form onSubmit={(e) => void submit(e)} className="glass-card w-full max-w-sm space-y-4 rounded-lg p-6">
          <div>
            <h2 className="text-lg font-semibold text-text-primary">输入访问密码</h2>
            <p className="mt-1 text-sm text-text-tertiary">此部署已启用访问密码, 输入后进入管理界面; 持有 API Token 也可在地址栏用 #token= 直连</p>
          </div>
          <PasswordInput
            autoFocus required value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="访问密码"
          />
          {error && <p role="alert" className="text-sm text-error">{error}</p>}
          <button type="submit" disabled={busy || !password}
            className="w-full rounded-md bg-accent px-3 py-2 text-sm font-medium text-white disabled:opacity-50">
            {busy ? '验证中…' : '进入'}
          </button>
        </form>
      ) : (
        <div className="text-sm text-text-tertiary">正在连接服务…</div>
      )}
    </div>
  );
}
