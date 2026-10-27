import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useShallow } from 'zustand/react/shallow';
import { useBackendStore } from '@/stores/useBackendStore';
import { useApiSessionStore } from '@/stores/useApiSessionStore';
import { controlClient } from '@/api/control';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { copyTextToClipboard } from '@/utils/clipboard';
import { Card } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { BackendControls } from '@/components/common/BackendControls';
import { AdapterStatus } from '@/components/common/AdapterStatus';
import { PageHeader } from '@/components/common';
import { cn } from '@/utils/cn';
import {
  AlertCircle, ArrowUpRight, Bot, Check, Copy, Cpu, Globe, Server, Terminal, Wifi, WifiOff,
} from 'lucide-react';

type Tint = 'accent' | 'purple' | 'teal' | 'pink' | 'green' | 'orange' | 'red';

/** 数字缓动计数, 减少动画时直接显示终值 */
function CountUp({ value }: { value: number }) {
  const [shown, setShown] = useState(value);
  useEffect(() => {
    const reduced = document.documentElement.getAttribute('data-motion') === 'reduced'
      || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) { setShown(value); return; }
    const from = 0, start = performance.now();
    let frame = 0;
    const step = (time: number) => {
      const progress = Math.min(1, (time - start) / 900);
      setShown(Math.round(from + (value - from) * (1 - Math.pow(1 - progress, 4))));
      if (progress < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value]);
  return <span className="num">{shown.toLocaleString()}</span>;
}

function StatTile({ tint, icon, label, value, to, index }: { tint: Tint; icon: ReactNode; label: string; value: number; to: string; index: number }) {
  const navigate = useNavigate();
  return (
    <Card
      variant={tint === 'red' ? 'error' : tint}
      interactive
      role="link"
      tabIndex={0}
      aria-label={`${label} ${value}, 查看详情`}
      onClick={() => navigate(to)}
      onKeyDown={(event) => { if (event.key === 'Enter') navigate(to); }}
      className="group !p-5"
      style={{ ['--i' as string]: index }}
    >
      <div className="flex items-start justify-between">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-[var(--tint-fg)] shadow-[inset_0_1px_0_var(--glass-highlight)]">{icon}</div>
        <ArrowUpRight className="h-4 w-4 -translate-x-1 translate-y-1 text-text-disabled opacity-0 transition-all duration-300 group-hover:translate-x-0 group-hover:translate-y-0 group-hover:text-[var(--tint-fg)] group-hover:opacity-100" />
      </div>
      <p className="mt-4 text-[13px] text-text-tertiary">{label}</p>
      <p className="mt-0.5 text-[28px] font-semibold leading-none text-text-primary"><CountUp value={value} /></p>
    </Card>
  );
}

function ServiceHealth() {
  const sessions = useApiSessionStore((state) => state.sessions);
  const services = [
    { name: '后端服务', url: getApiBaseUrl() },
    { name: '控制服务', url: getControlApiUrl() },
    { name: 'Chat 服务', url: getChatApiUrl() },
  ];
  return (
    <Card className="!p-6" style={{ ['--i' as string]: 1 }}>
      <h3 className="text-[15px] font-semibold text-text-primary">服务连接</h3>
      <p className="mt-0.5 text-[13px] text-text-tertiary">三项服务各自独立认证</p>
      <ul className="mt-4 space-y-3">
        {services.map((service) => {
          const session = sessions[service.url.replace(/\/$/, '')];
          const tone = !session || session.state === 'checking' || session.state === 'password_required' ? 'tint-orange' : session.state === 'ready' ? 'tint-green' : 'tint-red';
          const label = !session || session.state === 'checking' ? '连接中' : session.state === 'ready' ? '已连接' : session.state === 'password_required' ? '需要密码' : '连接失败';
          return (
            <li key={service.name} className="flex items-center gap-3">
              <span className={cn('dot', tone)} data-live={session?.state === 'ready' ? '1' : undefined} />
              <span className="flex-1 text-sm text-text-secondary">{service.name}</span>
              <span className={cn('text-xs text-[var(--tint-fg)]', tone)}>{label}</span>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function QuickStart() {
  const [copied, setCopied] = useState('');
  const commands = [
    { label: '启动控制服务', command: 'python -m satrap.core.backend.control_server' },
    { label: '或直接运行后端', command: 'python -m satrap.main run' },
  ];
  const copy = (command: string) => {
    void copyTextToClipboard(command).then(() => {
      setCopied(command);
      setTimeout(() => setCopied(''), 1600);
    }).catch(() => undefined);
  };
  return (
    <div className="mt-5 grid gap-3 md:grid-cols-2">
      {commands.map((item, index) => (
        <div key={item.command} className="relative rounded-xl border border-glass-border bg-[rgb(0_0_0/0.18)] p-3.5 [[data-theme=light]_&]:bg-white/50">
          <div className="mb-1.5 flex items-center gap-2 text-[11px] font-medium text-text-disabled">
            <span className="grid h-4 w-4 place-items-center rounded-full bg-[rgb(var(--tint)/0.2)] text-[10px] text-[var(--tint-fg)]">{index + 1}</span>
            {item.label}
          </div>
          <code className="block truncate pr-10 font-mono text-[13px] text-text-primary"><span className="text-text-disabled">$ </span>{item.command}</code>
          <button type="button" onClick={() => copy(item.command)} aria-label={`复制命令 ${item.command}`}
            className="absolute bottom-3 right-3 grid h-7 w-7 place-items-center rounded-lg border border-glass-border bg-glass text-text-tertiary transition hover:text-text-primary">
            {copied === item.command ? <Check className="h-3.5 w-3.5 text-success animate-pop" /> : <Copy className="h-3.5 w-3.5" />}
          </button>
        </div>
      ))}
    </div>
  );
}

export function Dashboard() {
  const [counts, setCounts] = useState({ modelCount: 0, sessionCount: 0 });
  const [configError, setConfigError] = useState('');
  useEffect(() => {
    const poll = startVisiblePoll(async (signal) => {
      const { data } = await controlClient.get<{ model_counts: Record<string, number>; session_count: number }>('/config/overview', { signal });
      if (signal.aborted) return;
      setCounts({ modelCount: Object.values(data.model_counts).reduce((total, count) => total + count, 0), sessionCount: data.session_count });
      setConfigError('');
    }, { interval: 30000, onError: (error) => setConfigError(error instanceof Error ? error.message : '读取配置概览失败') });
    return () => poll.stop();
  }, []);
  const { health, healthAvailable, isRunning, runState, operation, controlStatus, statusConnected } = useBackendStore(useShallow((state) => ({
    health: state.health, healthAvailable: state.healthAvailable, isRunning: state.isRunning, runState: state.runState, operation: state.operation,
    controlStatus: state.controlStatus, statusConnected: state.statusConnected })));

  const adapters = useMemo(() => (isRunning && health?.adapters ? Object.entries(health.adapters) : []), [isRunning, health?.adapters]);
  const unknown = !controlStatus && !isRunning;
  const tone: Tint = operation ? 'accent' : isRunning ? 'green' : unknown || runState === 'unknown' ? 'orange' : 'red';
  const heroTitle = unknown ? '暂时无法确认后端状态' : operation ? { start: '后端启动中', stop: '后端停止中', restart: '后端重启中' }[operation]
    : runState === 'unknown' ? '后端状态未知' : isRunning ? '运行中' : '后端未运行';
  const heroHint = unknown ? '请检查控制服务是否已启动及连接地址是否正确, 然后在顶栏状态中重新连接'
    : isRunning ? `${adapters.length} 个适配器在线 · 状态${statusConnected ? '实时推送' : '轮询更新'}`
      : runState === 'stopped' ? '点击「启动后端」启动服务, 启动后即可接收各平台消息' : '正在确认后端运行状态';

  return (
    <div className="space-y-6">
      <PageHeader
        title="仪表盘"
        description="系统运行状态概览"
        actions={<Badge variant={statusConnected ? 'success' : 'default'} className="flex items-center gap-1">
          {statusConnected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
          {statusConnected ? '实时' : '轮询'}
        </Badge>}
      />

      <div className="stagger grid gap-4 lg:grid-cols-3">
        {/* 后端状态主卡: 运行时带流光边框 */}
        <Card variant={tone === 'red' ? 'error' : tone} flowing={isRunning || !!operation} className="relative overflow-hidden !p-6 lg:col-span-2" style={{ ['--i' as string]: 0 }}>
          <div aria-hidden className="pointer-events-none absolute -right-16 -top-24 h-72 w-72 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.32),transparent_65%)] blur-2xl" />
          <div className="relative flex flex-col gap-6">
            <div className="flex items-center gap-5">
              <div className="relative grid h-16 w-16 shrink-0 place-items-center">
                {isRunning && <span aria-hidden className="absolute inset-0 rounded-2xl bg-[rgb(var(--tint)/0.18)] opacity-40 animate-[ping_2.4s_var(--ease-out)_infinite]" />}
                <div className="relative grid h-16 w-16 place-items-center rounded-2xl border border-[rgb(var(--tint)/0.3)] bg-[linear-gradient(135deg,rgb(var(--tint)/0.3),rgb(var(--tint)/0.08))] text-[var(--tint-fg)] shadow-[inset_0_1px_0_var(--glass-highlight),0_12px_32px_-10px_rgb(var(--tint)/0.7)]">
                  {unknown ? <AlertCircle className="h-7 w-7" /> : <Server className="h-7 w-7" />}
                </div>
              </div>
              <div className="min-w-0">
                <p className="text-xs font-medium text-text-tertiary">后端状态</p>
                <h2 className="mt-1 text-2xl font-semibold text-text-primary">{heroTitle}</h2>
                <p className="mt-1 text-sm text-text-tertiary">{heroHint}</p>
              </div>
            </div>
            <BackendControls />
          </div>
          {unknown && <QuickStart />}
        </Card>

        <ServiceHealth />
      </div>

      {configError && <p role="alert" className="text-error">{configError}</p>}
      {isRunning && !healthAvailable && health?.error && (
        <Card variant="warning" className="!p-4">
          <div className="flex items-start gap-3">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0 text-warning" />
            <div>
              <h3 className="font-semibold text-text-primary">后端已启动, 暂时无法读取运行状态</h3>
              <p className="mt-0.5 text-sm text-text-secondary">{health.error}</p>
            </div>
          </div>
        </Card>
      )}

      <div className="stagger grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatTile index={2} tint="purple" icon={<Cpu className="h-[18px] w-[18px]" />} label="模型配置" value={counts.modelCount} to="/models" />
        <StatTile index={3} tint="teal" icon={<Bot className="h-[18px] w-[18px]" />} label="会话类" value={counts.sessionCount} to="/agents" />
        <StatTile index={4} tint="pink" icon={<Globe className="h-[18px] w-[18px]" />} label="适配器" value={adapters.length} to="/platforms" />
      </div>

      {adapters.length > 0 && (
        <Card className="!p-0 animate-rise">
          <div className="flex items-center justify-between px-6 pb-3 pt-5">
            <div>
              <h3 className="text-[15px] font-semibold text-text-primary">运行中的适配器</h3>
              <p className="mt-0.5 text-[13px] text-text-tertiary">平台连接、会话配置与配置应用状态</p>
            </div>
          </div>
          <div className="space-y-0.5 px-2 pb-2">
            {adapters.map(([id, info]) => (
              <div key={id} className="flex flex-wrap items-center justify-between gap-3 rounded-xl px-4 py-3 transition-colors hover:bg-glass-hover">
                <div className="flex min-w-0 items-center gap-3">
                  <span className={cn('grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]', info.status === 'running' ? 'tint-pink' : 'tint-orange')}>
                    <Globe className="h-4 w-4" />
                  </span>
                  <span className="truncate font-medium text-text-primary">{id}</span>
                  <Badge variant="info">{info.config_type || info.type || 'unknown'}</Badge>
                </div>
                <AdapterStatus info={info} application={health?.platform_config?.find((item) => item.id === id)} />
              </div>
            ))}
          </div>
        </Card>
      )}

      {unknown && (
        <p className="flex items-center gap-2 text-xs text-text-tertiary">
          <Terminal className="h-3.5 w-3.5" />控制服务恢复后, 页面会自动刷新状态
        </p>
      )}
    </div>
  );
}
