import { useState } from 'react';
import { establishApiSession, passwordLogin } from '@/api/auth';
import { PasswordInput } from '@/components/ui/PasswordInput';
import { useApiSessionStore } from '@/stores/useApiSessionStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';

export function ApiSessionNotice() {
  const sessions = useApiSessionStore((state) => state.sessions);
  const backendStopped = useBackendStore((state) => state.runState === 'stopped' || state.operation !== null);
  const services = [{ name: '后端', url: getApiBaseUrl() }, { name: '控制服务', url: getControlApiUrl() }, { name: 'Chat', url: getChatApiUrl() }];
  const locked = services.filter((service) => sessions[service.url.replace(/\/$/, '')]?.state === 'password_required');
  const errors = services.flatMap((service) => {
    const session = sessions[service.url.replace(/\/$/, '')];
    if (!session?.error) return [];
    const connectionFailure = session.errorKind === 'network' || session.errorKind === 'timeout';
    if (service.name === '后端' && backendStopped && connectionFailure) return [];
    const authenticationFailure = session.status === 401 || session.status === 403;
    return [{ ...service, session, action: authenticationFailure ? '认证' : '连接',
      message: authenticationFailure ? '认证失败' : connectionFailure ? '连接失败' : '请求失败' }];
  });
  if (!errors.length && !locked.length) return null;
  return <aside className="fixed bottom-4 right-4 z-50 max-w-[calc(100vw-2rem)] space-y-2 rounded-lg border border-glass-border bg-bg-secondary p-3 shadow-lg">
    {locked.map((service) => <ServicePassword key={service.name} name={service.name} url={service.url} />)}
    {errors.map((service) => <div key={service.name} className="flex items-start gap-3 text-sm">
      <p role="alert" className="max-w-sm text-error">{service.name}{service.message}: {service.session.error}</p>
      <button className="shrink-0 text-accent disabled:opacity-50" disabled={service.session.state === 'checking'} onClick={() => void establishApiSession(service.url).catch(() => undefined)}>
        {service.session.state === 'checking' ? `正在重试${service.name}${service.action}…` : `重试${service.name}${service.action}`}
      </button>
    </div>)}
  </aside>;
}

/** 单个附属服务可独立恢复认证, 不阻断当前页面已经可用的功能 */
function ServicePassword({ name, url }: { name: string; url: string }) {
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return <form className="max-w-sm space-y-2" onSubmit={(event) => {
    event.preventDefault();
    if (!password || busy) return;
    setBusy(true); setError('');
    void passwordLogin(url, password, remember).then(() => window.location.reload()).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : String(reason)); setBusy(false);
    });
  }}>
    <p className="text-sm">{name}需要访问密码</p>
    <PasswordInput aria-label={`${name}访问密码`} value={password} onChange={(event) => setPassword(event.target.value)} required />
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />记住此浏览器 (30 天)</label>
    {error && <p role="alert" className="text-sm text-error">{error}</p>}
    <button className="text-sm text-accent" disabled={busy || !password} type="submit">{busy ? '正在登录…' : `登录${name}`}</button>
  </form>;
}
