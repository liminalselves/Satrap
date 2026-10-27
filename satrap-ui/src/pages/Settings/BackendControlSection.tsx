import { Server, Terminal, Wifi } from 'lucide-react';
import { cn } from '@/utils/cn';
import { useBackendStore } from '@/stores/useBackendStore';
import { useServiceHealthStore } from '@/stores/useServiceHealthStore';
import { BackendControls, backendStateLabel } from '@/components/common/BackendControls';
import { Badge, StatusDot } from '@/components/kit';
import type { Service } from '@/api/contracts';
import { formatUptime } from './types';
import { Banner, SectionCard } from './shared';

const stateText: Record<string, string> = {
  running: '后端运行中',
  stopped: '后端未运行',
  unknown: '后端状态未知',
};

const serviceLabels: Record<Service, string> = { control: 'Control 服务', backend: '平台 Backend', chat: 'Chat 服务' };

export function BackendControlSection({ i, host, port, commonDirty }: {
  i: number; host: string; port: number; commonDirty: boolean;
}) {
  const runState = useBackendStore((state) => state.runState);
  const operation = useBackendStore((state) => state.operation);
  const services = useServiceHealthStore((state) => state.services);
  const online = useServiceHealthStore((state) => state.services.control.health?.running === true);

  const running = runState === 'running';
  const busy = operation !== null;
  const unknown = runState === 'unknown';
  const url = `http://${host || '127.0.0.1'}:${port || 19870}`;
  const backendHealth = services.backend.health;

  return (
    <SectionCard id="backend" i={i} tint={running ? 'green' : 'orange'} flowing={running || busy}
      icon={<Server className="h-4 w-4" />}
      title="后端控制" description="启动、停止或重启平台后端, 并确认三项服务的连接状态"
      actions={<>
        <Badge tone={running ? 'green' : unknown ? 'orange' : 'neutral'} dot live={running}>
          {backendStateLabel({ operation, runState })}
        </Badge>
      </>}>

      <div className="flex flex-col gap-5 rounded-2xl border border-[rgb(var(--tint)/0.24)] bg-[linear-gradient(135deg,rgb(var(--tint)/0.1),transparent_70%)] p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-4">
          <div className="relative grid h-14 w-14 flex-none place-items-center">
            {running && <span aria-hidden className="absolute inset-0 rounded-2xl bg-[rgb(var(--tint)/0.2)] animate-[ping_2.4s_var(--ease-out)_infinite] opacity-40" />}
            <div className="relative grid h-14 w-14 place-items-center rounded-2xl border border-[rgb(var(--tint)/0.3)] bg-[linear-gradient(135deg,rgb(var(--tint)/0.28),rgb(var(--tint)/0.06))] text-tint shadow-[inset_0_1px_0_var(--glass-highlight),0_12px_28px_-12px_rgb(var(--tint)/0.8)]">
              <Server className="h-6 w-6" />
            </div>
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider text-tint">
              <StatusDot tone={running ? 'green' : unknown ? 'orange' : 'neutral'} live={running} />
              {running ? 'HEALTHY' : runState.toUpperCase()}
            </div>
            <h3 className="display mt-0.5 text-xl font-semibold text-fg">{stateText[runState] ?? stateText.unknown}</h3>
            <p className="mt-0.5 text-[13px] text-fg-3">
              {running
                ? <>已持续运行 <span className="num font-mono text-fg-2">{formatUptime(backendHealth?.uptime_seconds ?? null)}</span>{backendHealth?.pid ? <> · PID <span className="num font-mono text-fg-2">{backendHealth.pid}</span></> : null}{backendHealth?.version ? <> · 版本 <span className="num font-mono text-fg-2">{backendHealth.version}</span></> : null}</>
                : unknown ? '控制服务没有响应, 请检查启动命令后刷新' : '启动后即可接收各平台消息'}
            </p>
          </div>
        </div>
        <div className="flex flex-none flex-wrap items-center gap-2">
          <BackendControls />
        </div>
      </div>

      {!online && !unknown && (
        <Banner tone="warning" title="控制服务未运行" className="mt-4">
          后端控制依赖控制服务。请运行 <code className="rounded bg-glass-hover px-1.5 py-0.5 font-mono text-[12px] text-fg">python -m satrap.core.backend.control_server</code>, 然后在顶栏状态中重试。
        </Banner>
      )}

      {commonDirty && (
        <Banner tone="warning" title="常用配置有未保存修改" className="mt-4" compact>
          重启后端会使用磁盘上的旧配置。请先在“常用配置”中保存。
        </Banner>
      )}

      <div className="mt-4 grid gap-2 sm:grid-cols-3">
        {(['control', 'backend', 'chat'] as Service[]).map((id) => {
          const sample = services[id];
          const health = sample.health;
          const state = sample.checking ? 'checking' : health?.running ? 'online' : sample.error ? 'offline' : 'unknown';
          const tone = state === 'online' ? 'green' : state === 'checking' ? 'orange' : 'red';
          return (
            <div key={id} className={cn('rounded-xl border border-line bg-glass p-3', `tint-${tone}`)}>
              <div className="flex items-center gap-2">
                <StatusDot tone={tone} live={state === 'online'} />
                <span className="text-[13px] font-medium text-fg">{serviceLabels[id]}</span>
                <span className="num ml-auto font-mono text-[11px] text-fg-3">
                  {state === 'checking' ? '检查中' : state === 'online' ? `${sample.rtt_ms ?? '—'}ms` : '离线'}
                </span>
              </div>
              <p className="mt-1 truncate font-mono text-[10.5px] text-fg-4">
                {health ? `${health.service} · ${health.revision || '无修订'}` : sample.error || '尚未读取'}
              </p>
              <p className="mt-0.5 truncate font-mono text-[10.5px] text-fg-4">
                {health ? `版本 ${health.version || '—'} · 运行 ${formatUptime(health.uptime_seconds)}` : '—'}
              </p>
            </div>
          );
        })}
      </div>

      <div className="mt-4 grid gap-3 rounded-xl border border-line bg-glass px-3.5 py-3 text-[12px] text-fg-3 sm:grid-cols-2">
        <span className="flex items-center gap-2"><Wifi className="h-3.5 w-3.5" />后端地址 <code className="truncate font-mono text-fg-2">{url}</code></span>
        <span className="flex items-center gap-2"><Terminal className="h-3.5 w-3.5" />控制服务 <code className="truncate font-mono text-fg-2">{services.control.health?.platform || '127.0.0.1:19871'}</code></span>
        <span className="text-fg-4">运行标识 PID {backendHealth?.pid ?? '—'} · 启动于 {backendHealth?.started_at ? new Date(backendHealth.started_at * 1000).toLocaleString('zh-CN') : '—'}</span>
        <span className="text-fg-4">重启会中断所有进行中的会话, 但不会丢失已保存配置</span>
      </div>
    </SectionCard>
  );
}
