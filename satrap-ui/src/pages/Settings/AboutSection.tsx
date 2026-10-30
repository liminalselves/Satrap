import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ArrowUpRight, BookOpen, Check, Copy, Cpu, HardDrive, Info, RefreshCw, ScrollText, Server, Sparkles, Terminal,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, StatusDot, toast, type Tone } from '@/components/kit';
import { Logo } from '@/components/common/Logo';
import type { Service } from '@/api/contracts';
import { useServiceHealthStore } from '@/stores/useServiceHealthStore';
import { formatTime } from '@/utils/format';
import { formatUptime } from './types';
import { KeyValue, SectionCard } from './shared';

const FRONTEND_VERSION = __APP_VERSION__;
const stack = ['React 18', 'TypeScript', 'Vite', 'Tailwind CSS', 'Zustand', 'React Router'];
const serviceLabels: Record<Service, string> = { control: 'Control 服务', backend: '平台 Backend', chat: 'Chat 服务' };
const serviceOrder: Service[] = ['control', 'backend', 'chat'];

export function AboutSection({ i, active, configPath, revision, backendRunning, controlOnline }: {
  i: number;
  active: boolean;
  configPath: string;
  revision: string;
  backendRunning: boolean;
  controlOnline: boolean;
}) {
  const navigate = useNavigate();
  const services = useServiceHealthStore((state) => state.services);
  const backendProbeMs = useServiceHealthStore((state) => state.backendProbeMs);
  const refresh = useServiceHealthStore((state) => state.refresh);
  const [copied, setCopied] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const controlHealth = services.control.health;
  const backendHealth = services.backend.health;
  // 前端随源码构建; 后端报告的版本不同说明两者来自不同发行, 界面与接口可能不匹配
  const versionMismatch = Boolean(backendHealth?.version && backendHealth.version !== FRONTEND_VERSION);
  const environment = backendHealth ?? controlHealth ?? services.chat.health;

  const diagnostics = useMemo(() => ({
    frontend: FRONTEND_VERSION,
    config_path: configPath || null,
    config_revision: revision || null,
    backend_running: backendRunning,
    control_online: controlOnline,
    services: Object.fromEntries(serviceOrder.map((service) => [service, {
      running: services[service].health?.running ?? null,
      version: services[service].health?.version ?? null,
      revision: services[service].health?.revision ?? null,
      python: services[service].health?.python ?? null,
      platform: services[service].health?.platform ?? null,
      uptime_seconds: services[service].health?.uptime_seconds ?? null,
      rtt_ms: services[service].rtt_ms,
      error: services[service].error,
    }])),
  }), [configPath, revision, backendRunning, controlOnline, services]);

  const copyDiagnostics = () => {
    void navigator.clipboard?.writeText(JSON.stringify(diagnostics, null, 2));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
    toast.success('诊断信息已复制', '可以粘贴到问题反馈中');
  };

  const reload = () => {
    if (refreshing) return;
    setRefreshing(true);
    void refresh().finally(() => setRefreshing(false));
  };

  const serviceState = (service: Service): { tone: Tone; label: string } => {
    const sample = services[service];
    if (sample.checking) return { tone: 'orange', label: '检查中' };
    if (sample.health?.running) return { tone: 'green', label: '运行中' };
    if (sample.error) return { tone: 'red', label: '离线' };
    return { tone: 'neutral', label: '未知' };
  };

  return (
    <SectionCard id="about" i={i} tint="orange" icon={<Info className="h-4 w-4" />}
      title="关于" description="版本信息、服务运行环境与技术栈。界面偏好已移动到“外观”分节"
      actions={<>
        <Badge tone={active ? 'green' : 'neutral'} dot live={active}>{active ? '实时读取服务健康' : '切换到本分节后刷新'}</Badge>
        <Button size="sm" variant="ghost" icon={<RefreshCw className={cn('h-4 w-4', refreshing && 'animate-spin-slow')} />}
          loading={refreshing} onClick={reload}>刷新</Button>
      </>}>

      <div className="flex flex-col gap-4 rounded-2xl border border-[rgb(var(--tint)/0.22)] bg-[linear-gradient(135deg,rgb(var(--tint)/0.12),transparent_70%)] p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-4">
          <Logo className="h-14 w-14 flex-none" />
          <div>
            <h3 className="display text-xl font-semibold text-fg">Satrap 管理面板</h3>
            <p className="mt-0.5 text-[12.5px] text-fg-3">
              前端 <span className="num font-mono text-fg-2">{FRONTEND_VERSION}</span>
              {' · '}后端 <span className="num font-mono text-fg-2">{backendHealth?.version || '—'}</span>
              {versionMismatch && <Badge tone="orange" className="ml-2">前后端版本不一致</Badge>}
            </p>
            <p className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-fg-4">
              <Sparkles className="h-3 w-3" />单机部署 · 控制服务与平台后端分工运行
            </p>
          </div>
        </div>
        <div className="flex flex-none flex-wrap gap-2">
          <Button size="sm" variant="primary" icon={<BookOpen className="h-3.5 w-3.5" />} onClick={() => navigate('/docs')}>使用文档</Button>
          <Button size="sm" icon={<RefreshCw className="h-3.5 w-3.5" />} loading={refreshing} onClick={reload}>检查服务状态</Button>
          <Button size="sm" variant="ghost" icon={copied ? <Check className="h-3.5 w-3.5 text-green" /> : <Copy className="h-3.5 w-3.5" />} onClick={copyDiagnostics}>
            {copied ? '已复制' : '复制诊断信息'}
          </Button>
        </div>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          { label: '前端版本', value: FRONTEND_VERSION, note: '当前构建', tint: 'accent' as const },
          { label: '平台后端', value: backendRunning ? '运行中' : '未运行', note: backendHealth?.version ? `版本 ${backendHealth.version}` : '版本未知', tint: backendRunning ? 'green' as const : 'orange' as const },
          { label: '控制服务', value: controlOnline ? '在线' : '离线', note: controlHealth?.revision ? `修订 ${controlHealth.revision}` : '修订未知', tint: controlOnline ? 'green' as const : 'red' as const },
          { label: '配置修订', value: revision || '—', note: configPath ? '已读取配置文件' : '尚未读取配置', tint: 'purple' as const },
        ].map((item) => (
          <div key={item.label} className={cn('rounded-xl border border-line bg-glass p-3.5', `tint-${item.tint}`)}>
            <p className="text-[11.5px] text-fg-3">{item.label}</p>
            <p className="mt-1 truncate text-lg font-semibold text-tint">{item.value}</p>
            <p className="mt-0.5 truncate text-[11px] text-fg-4">{item.note}</p>
          </div>
        ))}
      </div>

      <div className="mt-4 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Server className="h-3.5 w-3.5 text-orange" />
          <h3 className="text-[13px] font-medium text-fg">服务运行环境</h3>
          <span className="text-[11px] text-fg-4">数据来自各服务健康接口, 未知值显示为 —</span>
        </div>
        <div className="grid gap-3 lg:grid-cols-3">
          {serviceOrder.map((service) => {
            const sample = services[service];
            const health = sample.health;
            const state = serviceState(service);
            return (
              <div key={service} className={cn('rounded-xl border border-line bg-glass p-3.5', `tint-${state.tone === 'neutral' ? 'accent' : state.tone}`)}>
                <div className="flex items-center gap-2">
                  <StatusDot tone={state.tone} live={state.label === '运行中'} />
                  <span className="text-[13px] font-medium text-fg">{serviceLabels[service]}</span>
                  <span className="ml-auto flex items-center gap-1.5 text-[11px] text-fg-3">
                    {sample.rtt_ms !== null ? `${sample.rtt_ms}ms` : '—'}
                    <Badge tone={state.tone}>{state.label}</Badge>
                  </span>
                </div>
                <div className="mt-1">
                  <KeyValue label="版本" mono>{health?.version || '—'}</KeyValue>
                  <KeyValue label="修订" mono>{health?.revision || '—'}</KeyValue>
                  <KeyValue label="Python" mono>{health?.python || '—'}</KeyValue>
                  <KeyValue label="平台" mono>{health?.platform || '—'}</KeyValue>
                  <KeyValue label="运行时长" mono>{health ? formatUptime(health.uptime_seconds) : '—'}</KeyValue>
                </div>
                <p className={cn('mt-1 truncate text-[10.5px]', sample.error ? 'text-red' : 'text-fg-4')} title={sample.error || undefined}>
                  {sample.error || (health ? `启动于 ${formatTime(health.started_at)}` : '尚未读取健康状态')}
                </p>
              </div>
            );
          })}
        </div>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-line bg-glass p-3.5">
          <h3 className="text-[13px] font-medium text-fg">运行环境</h3>
          <div className="mt-1 divide-y divide-[var(--glass-border)]">
            <KeyValue label="配置文件" mono>{configPath || '—'}</KeyValue>
            <KeyValue label="配置修订" mono>{revision || '—'}</KeyValue>
            <KeyValue label="Python" mono>{environment?.python || '—'}</KeyValue>
            <KeyValue label="操作系统" mono>{environment?.platform || '—'}</KeyValue>
            <KeyValue label="后端进程" mono>{backendHealth?.pid ? String(backendHealth.pid) : '—'}</KeyValue>
            <KeyValue label="后端运行时长" mono>{backendHealth ? formatUptime(backendHealth.uptime_seconds) : '—'}</KeyValue>
            <KeyValue label="控制服务探测" mono>{backendProbeMs === null ? '—' : `${backendProbeMs}ms`}</KeyValue>
          </div>
        </div>

        <div className="rounded-xl border border-line bg-glass p-3.5">
          <h3 className="text-[13px] font-medium text-fg">技术栈</h3>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {stack.map((tech) => <Badge key={tech} tone="accent">{tech}</Badge>)}
          </div>
          <h3 className="mt-5 text-[13px] font-medium text-fg">相关入口</h3>
          <div className="mt-2 space-y-1.5">
            {[
              { to: '/docs', label: '使用文档', hint: '界面与功能说明', icon: <BookOpen className="h-3.5 w-3.5" /> },
              { to: '/logs', label: '运行日志', hint: '实时服务日志', icon: <ScrollText className="h-3.5 w-3.5" /> },
              { to: '/logs/debug', label: '调试日志', hint: '按会话查看请求链路', icon: <Terminal className="h-3.5 w-3.5" /> },
              { to: '/platforms', label: '平台配置', hint: '适配器与连接状态', icon: <Cpu className="h-3.5 w-3.5" /> },
            ].map((item) => (
              <Link key={item.to} to={item.to}
                className="group flex items-center gap-2.5 rounded-lg px-2 py-2 text-[12.5px] text-fg-2 transition-colors hover:bg-glass-hover hover:text-fg">
                <span className="grid h-7 w-7 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.12)] text-tint">{item.icon}</span>
                <span className="min-w-0 flex-1">
                  <span className="block font-medium">{item.label}</span>
                  <span className="block text-[10.5px] text-fg-4">{item.hint}</span>
                </span>
                <ArrowUpRight className="h-3.5 w-3.5 flex-none text-fg-4 transition-transform duration-200 group-hover:-translate-y-0.5 group-hover:translate-x-0.5 group-hover:text-tint" />
              </Link>
            ))}
            <div className="flex items-center gap-2.5 rounded-lg px-2 py-2 text-[12.5px] text-fg-3">
              <span className="grid h-7 w-7 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.12)] text-tint"><HardDrive className="h-3.5 w-3.5" /></span>
              <span className="min-w-0 flex-1">
                <span className="block font-medium">控制服务</span>
                <span className="block font-mono text-[10.5px] text-fg-4">{controlHealth?.platform || '127.0.0.1:19871'}</span>
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-4 text-[11px] text-fg-4">
        <span>Satrap © 2026 · 版本与运行环境来自真实服务健康接口</span>
        <span>{backendHealth ? `后端启动于 ${formatTime(backendHealth.started_at)}` : '后端启动时间未知'}</span>
      </div>
    </SectionCard>
  );
}
