import { useState } from 'react';
import { Lock, RefreshCw, WifiOff } from 'lucide-react';
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
  const tint = errors.length ? 'tint-red' : 'tint-orange';
  return <aside aria-label="服务连接提示" className={`${tint} glass-toast ${errors.length ? 'glass-toast-error' : 'glass-toast-warning'} !fixed bottom-4 right-4 z-50 w-[min(420px,calc(100vw-2rem))] !rounded-2xl`}>
    <span aria-hidden className="absolute inset-x-0 top-0 h-[3px] bg-[linear-gradient(90deg,rgb(var(--tint)/0.95),rgb(var(--tint)/0.15))]" />
    <div className="flex items-center gap-2.5 px-4 pb-2 pt-3.5">
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]">
        {errors.length ? <WifiOff className="h-4 w-4" aria-hidden /> : <Lock className="h-4 w-4" aria-hidden />}
      </span>
      <div className="min-w-0">
        <p className="text-sm font-semibold text-text-primary">{errors.length ? '服务连接异常' : '需要登录'}</p>
        <p className="text-xs text-text-tertiary">其它可用功能不受影响</p>
      </div>
    </div>
    <div className="space-y-1.5 px-2.5 pb-2.5">
      {locked.map((service) => <ServicePassword key={service.name} name={service.name} url={service.url} />)}
      {errors.map((service) => {
        const checking = service.session.state === 'checking';
        return <div key={service.name} className="flex items-center gap-3 rounded-xl border border-glass-border bg-glass px-3 py-2.5">
          <span className={`dot ${tint} shrink-0`} data-live="1" aria-hidden />
          <p role="alert" className="min-w-0 flex-1 leading-snug">
            <span className="block text-sm font-medium text-[var(--tint-fg)]">{service.name}{service.message}</span>
            <span className="sr-only">: </span>
            <span className="block text-xs text-text-tertiary">{service.session.error}</span>
          </p>
          <button type="button" className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border border-[rgb(var(--rgb-accent)/0.3)] bg-[rgb(var(--rgb-accent)/0.1)] px-2.5 py-1 text-xs font-medium text-[var(--fg-accent)] transition hover:bg-[rgb(var(--rgb-accent)/0.18)] disabled:opacity-60"
            disabled={checking} onClick={() => void establishApiSession(service.url).catch(() => undefined)}>
            <RefreshCw className={`h-3 w-3 ${checking ? 'animate-spin' : ''}`} aria-hidden />
            {checking ? `正在重试${service.name}${service.action}…` : `重试${service.name}${service.action}`}
          </button>
        </div>;
      })}
    </div>
  </aside>;
}

/** 单个附属服务可独立恢复认证, 不阻断当前页面已经可用的功能 */
function ServicePassword({ name, url }: { name: string; url: string }) {
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return <form className="space-y-2 rounded-xl border border-glass-border bg-glass px-3 py-2.5" onSubmit={(event) => {
    event.preventDefault();
    if (!password || busy) return;
    setBusy(true); setError('');
    void passwordLogin(url, password, remember).then(() => window.location.reload()).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : String(reason)); setBusy(false);
    });
  }}>
    <p className="text-sm font-medium text-text-primary">{name}需要访问密码</p>
    <PasswordInput aria-label={`${name}访问密码`} value={password} onChange={(event) => setPassword(event.target.value)} required />
    <div className="flex items-center gap-3">
      <label className="flex flex-1 items-center gap-2 text-xs text-text-secondary"><input type="checkbox" className="accent-[rgb(var(--rgb-accent))]" checked={remember} onChange={(event) => setRemember(event.target.checked)} />记住此浏览器 (30 天)</label>
      <button className="glass-button glass-button-primary !rounded-full !px-3.5 !py-1 text-xs" disabled={busy || !password} type="submit">{busy ? '正在登录…' : `登录${name}`}</button>
    </div>
    {error && <p role="alert" className="text-xs text-error">{error}</p>}
  </form>;
}
