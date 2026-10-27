import { useEffect, useRef, useState } from 'react';
import { RotateCw, Wifi, WifiOff } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import { establishApiSession } from '@/api/auth';
import { useApiSessionStore, type ApiSessionState } from '@/stores/useApiSessionStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { Link } from 'react-router-dom';
import { backendStateLabel } from '@/components/common/BackendControls';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { cn } from '@/utils/cn';

type Tone = 'green' | 'orange' | 'red' | 'neutral';

function serviceTone(session: ApiSessionState | undefined): Tone {
  if (!session || session.state === 'checking') return 'orange';
  if (session.state === 'ready') return 'green';
  if (session.state === 'password_required') return 'orange';
  return 'red';
}

const serviceLabel = (session: ApiSessionState | undefined) => !session || session.state === 'checking' ? '连接中'
  : session.state === 'ready' ? '已连接' : session.state === 'password_required' ? '需要密码' : '连接失败';

/**
 * 顶栏全局状态胶囊: 文字沿用后端运行态, 点击展开三项服务连接详情与逐项重试
 * 连接错误提示仍由 ApiSessionNotice 负责, 此处只做汇总与入口
 */
export function StatusPill() {
  const backend = useBackendStore(useShallow((state) => ({ runState: state.runState, operation: state.operation, isRunning: state.isRunning, statusConnected: state.statusConnected })));
  const sessions = useApiSessionStore((state) => state.sessions);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const services = [
    { name: '后端', url: getApiBaseUrl() },
    { name: '控制服务', url: getControlApiUrl() },
    { name: 'Chat', url: getChatApiUrl() },
  ].map((service) => ({ ...service, session: sessions[service.url.replace(/\/$/, '')] }));

  const tone: Tone = backend.runState === 'unknown' || backend.operation ? 'orange' : backend.isRunning ? 'green' : 'neutral';

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', escape);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('keydown', escape); };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="dialog"
        className={cn(
          'flex h-7 shrink-0 items-center gap-2 whitespace-nowrap rounded-full border px-2.5 text-[13px] font-medium transition-colors',
          tone !== 'neutral' && `tint-${tone}`,
          tone === 'neutral'
            ? 'border-glass-border bg-glass text-text-secondary hover:bg-glass-hover'
            : 'border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.1)] text-[var(--tint-fg)] hover:border-[rgb(var(--tint)/0.4)] hover:bg-[rgb(var(--tint)/0.16)]',
        )}
      >
        <span className={cn('dot', tone !== 'neutral' && `tint-${tone}`)} data-live={tone === 'green' ? '1' : undefined} />
        <span className="max-sm:sr-only">{backendStateLabel(backend)}</span>
      </button>

      {open && (
        <div role="dialog" aria-label="服务连接状态" className="surface surface-strong absolute left-0 top-[calc(100%+8px)] z-[70] w-[min(360px,calc(100vw-24px))] !rounded-2xl p-2 !shadow-[var(--shadow-3)] animate-pop" style={{ transformOrigin: 'top left' }}>
          <div className="px-2.5 pb-2 pt-1.5">
            <div className="text-sm font-semibold text-text-primary">服务连接</div>
            <div className="mt-0.5 flex items-center gap-1.5 text-xs text-text-tertiary">
              {backend.statusConnected ? <Wifi className="h-3 w-3 text-success" /> : <WifiOff className="h-3 w-3" />}
              {backend.statusConnected ? '状态实时推送' : '状态轮询中'}
            </div>
          </div>
          <ul className="space-y-1">
            {services.map((service) => {
              const serviceState = serviceTone(service.session);
              const failed = service.session?.state === 'error';
              return (
                <li key={service.name} className={cn('rounded-xl px-2.5 py-2.5', failed && 'tint-red bg-[rgb(var(--tint)/0.06)]')}>
                  <div className="flex items-center gap-3">
                    <span className={cn('dot', `tint-${serviceState}`)} data-live={service.session?.state === 'checking' ? '1' : undefined} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-[13px] font-medium text-text-primary">
                        {service.name}
                        <span className={cn('text-[11px] font-normal text-[var(--tint-fg)]', `tint-${serviceState}`)}>{serviceLabel(service.session)}</span>
                      </div>
                      <div className="truncate font-mono text-[11px] text-text-disabled">{service.url}</div>
                    </div>
                    {failed && (
                      <button
                        type="button"
                        onClick={() => void establishApiSession(service.url).catch(() => undefined)}
                        className="flex shrink-0 items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-accent transition hover:bg-glass-hover"
                      >
                        <RotateCw className="h-3.5 w-3.5" />重新连接
                      </button>
                    )}
                  </div>
                  {failed && service.session?.error && <p className="mt-1.5 pl-5 text-xs leading-5 text-text-tertiary">{service.session.error}</p>}
                </li>
              );
            })}
          </ul>
          <Link to="/" onClick={() => setOpen(false)} className="mt-2 block rounded-xl border-t border-glass-border px-2.5 pb-1.5 pt-2.5 text-xs text-accent transition hover:bg-glass-hover">
            在仪表盘中启动或停止后端 →
          </Link>
        </div>
      )}
    </div>
  );
}
