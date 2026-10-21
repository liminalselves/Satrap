import { establishApiSession } from '@/api/auth';
import { useApiSessionStore } from '@/stores/useApiSessionStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';

export function ApiSessionNotice() {
  const sessions = useApiSessionStore((state) => state.sessions);
  const backendStopped = useBackendStore((state) => state.runState === 'stopped' || state.operation !== null);
  const services = [{ name: '后端', url: getApiBaseUrl() }, { name: '控制服务', url: getControlApiUrl() }, { name: 'Chat', url: getChatApiUrl() }];
  const errors = services.flatMap((service) => {
    const session = sessions[service.url.replace(/\/$/, '')];
    if (!session?.error) return [];
    const connectionFailure = session.errorKind === 'network' || session.errorKind === 'timeout';
    if (service.name === '后端' && backendStopped && connectionFailure) return [];
    const authenticationFailure = session.status === 401 || session.status === 403;
    return [{ ...service, session, action: authenticationFailure ? '认证' : '连接',
      message: authenticationFailure ? '认证失败' : connectionFailure ? '连接失败' : '请求失败' }];
  });
  if (!errors.length) return null;
  return <aside className="fixed bottom-4 right-4 z-50 max-w-[calc(100vw-2rem)] space-y-2 rounded-lg border border-glass-border bg-bg-secondary p-3 shadow-lg">
    {errors.map((service) => <div key={service.name} className="flex items-start gap-3 text-sm">
      <p role="alert" className="max-w-sm text-error">{service.name}{service.message}: {service.session.error}</p>
      <button className="shrink-0 text-accent disabled:opacity-50" disabled={service.session.state === 'checking'} onClick={() => void establishApiSession(service.url).catch(() => undefined)}>
        {service.session.state === 'checking' ? `正在重试${service.name}${service.action}…` : `重试${service.name}${service.action}`}
      </button>
    </div>)}
  </aside>;
}
