import { establishApiSession } from '@/api/auth';
import { useApiSessionStore } from '@/stores/useApiSessionStore';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';

export function ApiSessionNotice() {
  const sessions = useApiSessionStore((state) => state.sessions);
  const services = [{ name: '后端', url: getApiBaseUrl() }, { name: '控制服务', url: getControlApiUrl() }, { name: 'Chat', url: getChatApiUrl() }];
  const errors = services.filter((service) => !!sessions[service.url.replace(/\/$/, '')]?.error);
  if (!errors.length) return null;
  return <aside className="fixed bottom-4 right-4 z-50 max-w-[calc(100vw-2rem)] space-y-2 rounded-lg border border-glass-border bg-bg-secondary p-3 shadow-lg">
    {errors.map((service) => <div key={service.name} className="flex items-start gap-3 text-sm">
      <p role="alert" className="max-w-sm text-error">{service.name}认证失败: {sessions[service.url.replace(/\/$/, '')]?.error}</p>
      <button className="shrink-0 text-accent disabled:opacity-50" disabled={sessions[service.url.replace(/\/$/, '')]?.state === 'checking'} onClick={() => void establishApiSession(service.url).catch(() => undefined)}>
        {sessions[service.url.replace(/\/$/, '')]?.state === 'checking' ? `正在重试${service.name}认证…` : `重试${service.name}认证`}
      </button>
    </div>)}
  </aside>;
}
