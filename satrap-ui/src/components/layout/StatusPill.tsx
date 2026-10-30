import { useEffect, useRef, useState } from 'react';
import { Play, RotateCw, Square, Wifi, WifiOff, Zap } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import { establishApiSession } from '@/api/auth';
import type { Service } from '@/api/contracts';
import { useApiSessionStore, type ApiSessionState } from '@/stores/useApiSessionStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { startServiceHealthPolling, useServiceHealthStore, type ServiceSample } from '@/stores/useServiceHealthStore';
import { backendStateLabel } from '@/components/common/BackendControls';
import { Button, StatusDot } from '@/components/kit';
import { toast } from '@/components/ui/Toast';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { cn } from '@/utils/cn';

type Tone = 'green' | 'orange' | 'red' | 'neutral';

function rowState(session: ApiSessionState | undefined, sample: ServiceSample): { tone: Tone; label: string; failed: boolean } {
  if (session?.state === 'password_required') return { tone: 'orange', label: '需要密码', failed: false };
  if (session?.state === 'error') return { tone: 'red', label: '连接失败', failed: true };
  if (sample.health) return sample.health.healthy ? { tone: 'green', label: '在线', failed: false } : { tone: 'orange', label: '运行异常', failed: false };
  if (sample.error) return { tone: 'red', label: '离线', failed: true };
  if (!session || session.state === 'checking' || sample.checking) return { tone: 'orange', label: '连接中', failed: false };
  return { tone: 'green', label: '已连接', failed: false };
}

function uptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
  return `${Math.floor(seconds / 86400)} 天 ${Math.floor((seconds % 86400) / 3600)} 小时`;
}

/**
 * 顶栏全局状态胶囊: 文字沿用后端运行态, 点击展开三项服务的健康, 耗时与逐项重试
 * 连接错误提示仍由 ApiSessionNotice 负责, 此处只做汇总与入口
 */
export function StatusPill() {
  const backend = useBackendStore(useShallow((state) => ({ runState: state.runState, operation: state.operation, isRunning: state.isRunning,
    statusConnected: state.statusConnected, controlBackend: state.controlBackend, hasControl: !!state.controlStatus })));
  const sessions = useApiSessionStore((state) => state.sessions);
  const samples = useServiceHealthStore((state) => state.services);
  const probeMs = useServiceHealthStore((state) => state.backendProbeMs);
  const refresh = useServiceHealthStore((state) => state.refresh);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const services: { id: Service; name: string; url: string }[] = [
    { id: 'backend', name: '后端', url: getApiBaseUrl() },
    { id: 'control', name: '控制服务', url: getControlApiUrl() },
    { id: 'chat', name: 'Chat', url: getChatApiUrl() },
  ];

  const tone: Tone = backend.runState === 'unknown' || backend.operation ? 'orange' : backend.isRunning ? 'green' : 'neutral';

  useEffect(() => startServiceHealthPolling(), []);
  useEffect(() => { if (open) void refresh(); }, [open, refresh]);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', escape);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('keydown', escape); };
  }, [open]);

  const control = async (operation: 'start' | 'stop') => {
    const result = await backend.controlBackend(operation);
    toast(result.ok ? 'success' : 'error', result.ok ? (operation === 'start' ? '后端已启动' : '后端已停止') : result.error || '操作失败');
    void refresh('backend');
  };

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-haspopup="dialog"
        className={cn(
          'ctl ctl-sm whitespace-nowrap !rounded-full !pl-2.5 !pr-3',
          tone !== 'neutral' && `tint-${tone}`,
          tone !== 'neutral' && 'border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.1)] text-tint hover:border-[rgb(var(--tint)/0.4)] hover:bg-[rgb(var(--tint)/0.16)]',
        )}
      >
        <StatusDot tone={tone} live={tone === 'green' || tone === 'orange'} />
        <span className="text-[13px] max-sm:sr-only">{backendStateLabel(backend)}</span>
      </button>

      {open && (
        <div role="dialog" aria-label="服务连接状态" className="surface surface-strong absolute left-0 top-[calc(100%+8px)] z-[130] w-[min(360px,calc(100vw-24px))] !rounded-2xl p-2 !shadow-[var(--shadow-3)] animate-pop" style={{ transformOrigin: 'top left' }}>
          <div className="flex items-center justify-between gap-2 px-2.5 pb-2 pt-1.5">
            <div>
              <div className="text-sm font-semibold text-fg">服务连接</div>
              <div className="mt-0.5 flex items-center gap-1.5 text-xs text-fg-3">
                {backend.statusConnected ? <Wifi className="h-3 w-3 text-green" /> : <WifiOff className="h-3 w-3" />}
                {backend.statusConnected ? '状态实时推送' : '状态轮询中'}
              </div>
            </div>
            {backend.hasControl && (backend.isRunning || backend.operation === 'stop'
              ? <Button size="sm" variant="danger" icon={<Square className="h-3.5 w-3.5" />} loading={backend.operation === 'stop'} disabled={!!backend.operation} onClick={() => void control('stop')}>停止后端</Button>
              : <Button size="sm" variant="primary" icon={<Play className="h-3.5 w-3.5" />} loading={backend.operation === 'start'} disabled={!!backend.operation || backend.runState === 'unknown'} onClick={() => void control('start')}>启动后端</Button>)}
          </div>
          <ul className="space-y-1">
            {services.map((service) => {
              const session = sessions[service.url.replace(/\/$/, '')];
              const sample = samples[service.id];
              const state = rowState(session, sample);
              const error = session?.state === 'error' ? session.error : sample.error;
              return (
                <li key={service.id} className={cn('rounded-xl border border-transparent px-2.5 py-2.5 transition-colors', state.failed && 'tint-red border-[rgb(var(--tint)/0.15)] bg-[rgb(var(--tint)/0.06)]')}>
                  <div className="flex items-center gap-3">
                    <StatusDot tone={state.tone} live={state.label === '连接中'} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-[13px] font-medium text-fg">
                        {service.name}
                        <span className={cn('text-[11px] font-normal text-tint', `tint-${state.tone}`)}>{state.label}</span>
                      </div>
                      <div className="num truncate font-mono text-[11px] text-fg-4">{service.url}</div>
                      {sample.health && (
                        <div className="num mt-0.5 text-[11px] text-fg-3">
                          已运行 {uptime(sample.health.uptime_seconds)} · PID {sample.health.pid}
                          {service.id === 'backend' && probeMs !== null && ` · 控制探测 ${Math.round(probeMs)}ms`}
                        </div>
                      )}
                    </div>
                    {state.failed ? (
                      <Button size="sm" variant="ghost" disabled={sample.checking}
                        icon={<RotateCw className={cn('h-3.5 w-3.5', sample.checking && 'animate-spin-slow')} />}
                        onClick={() => { void establishApiSession(service.url).catch(() => undefined).then(() => refresh(service.id)); }}>
                        重新连接
                      </Button>
                    ) : sample.rtt_ms !== null && (
                      <span className="num flex shrink-0 items-center gap-1 text-xs text-fg-3" title="浏览器往返耗时"><Zap className="h-3 w-3" />{sample.rtt_ms}ms</span>
                    )}
                  </div>
                  {state.failed && error && <p className="mt-1.5 pl-5 text-xs leading-5 text-fg-3">{error}</p>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
