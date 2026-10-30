import { useState, type ReactNode } from 'react';
import {
  Activity, BookOpen, Boxes, Clock, Database, Gauge, Hash, MessageSquare, RotateCcw, Settings2, Trash2, X, Zap,
} from 'lucide-react';
import { Badge, StatusDot } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { Drawer } from '@/components/kit/Drawer';
import { Tabs } from '@/components/kit/Tabs';
import { EmptyState, Sparkline } from '@/components/kit/Feedback';
import { cn } from '@/utils/cn';
import {
  canRetryRuntime, configState, formatDateTime, formatNumber, formatUptime, pluginState,
  relativeTime, sessionTone, type SessionUsageView, type SessionView,
} from './shared';

const pluginTone = (status: string): 'green' | 'orange' | 'neutral' | 'red' => {
  if (status === 'loaded') return 'green';
  if (status === 'pending') return 'orange';
  if (status === 'error') return 'red';
  return 'neutral';
};

const pluginLabel: Record<string, string> = { loaded: '已加载', pending: '待加载', disabled: '已停用', error: '失败' };

export interface InstanceInspectorProps {
  session: SessionView | null;
  open: boolean;
  usage: SessionUsageView;
  backendRunning: boolean;
  retrying: boolean;
  onClose: () => void;
  onOpenRag: () => void;
  onOpenPlugins: () => void;
  onRetry: () => void;
  onDelete: () => void;
}

export function InstanceInspector({
  session, open, usage, backendRunning, retrying, onClose, onOpenRag, onOpenPlugins, onRetry, onDelete,
}: InstanceInspectorProps) {
  const [tab, setTab] = useState<'overview' | 'plugins' | 'config'>('overview');
  if (!session) return null;
  const tone = sessionTone(session);
  const config = configState(session);
  const plugins = pluginState(session);
  const contextPct = usage.contextUsed !== null && usage.contextWindow
    ? Math.min(100, Math.round((usage.contextUsed / Math.max(1, usage.contextWindow)) * 100))
    : null;
  const canRetry = canRetryRuntime(session, backendRunning);
  const pluginIssues = session.plugins.filter((item) => item.status === 'error' || item.drift || item.restart_required);
  const appliedConfig = (session.raw.runtime?.config as { applied?: unknown } | undefined)?.applied;

  return (
    <Drawer open={open} onClose={onClose} label={`会话实例 ${session.sessionId}`} width={780}>
      <div className="flex items-center justify-between border-b border-line px-5 py-2.5">
        <span className="flex items-center gap-2 text-[11px] text-fg-4">
          <Boxes className="h-3.5 w-3.5" />实例检查器
          <span className="font-mono">{session.platformId} / {session.sessionId}</span>
        </span>
        <IconButton label="关闭检查器" shortcut="Esc" size="sm" onClick={onClose}><X className="h-4 w-4" /></IconButton>
      </div>

      <div className="scroll min-h-0 flex-1">
        <div className={cn('relative overflow-hidden rounded-b-[22px] px-5 pb-4 pt-4', `tint-${tone}`)}>
          <div aria-hidden className="pointer-events-none absolute -right-12 -top-16 h-52 w-52 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.32),transparent_65%)] blur-2xl" />
          <div className="relative flex items-start gap-3">
            <div className="grid h-11 w-11 flex-none place-items-center rounded-2xl border border-[rgb(var(--tint)/0.28)] bg-[rgb(var(--tint)/0.16)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">
              <Boxes className="h-5 w-5" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="truncate text-[16px] font-semibold text-fg">
                  {session.target ?? '未绑定对话'} <span className="font-mono text-[13px] font-normal text-fg-3">{session.shortId}</span>
                </h2>
                <Badge tone={session.active ? 'green' : 'neutral'} dot live={session.active}>{session.active ? '活跃' : '已持久化'}</Badge>
                {config && <span title={config.title}><Badge tone={config.tone}>{config.label}</Badge></span>}
                {plugins.tone !== 'neutral' && <span title={plugins.title}><Badge tone={plugins.tone}>{plugins.label}</Badge></span>}
              </div>
              <p className="mt-1 text-xs text-fg-3">
                {session.platformId} · {session.provider === 'edictum' ? 'Edictum' : '会话类'} · <span className="font-mono">{session.sessionType}</span>
              </p>
              <p className="num mt-0.5 font-mono text-[10.5px] text-fg-4">创建于 {formatDateTime(session.createdAt)} · 最近使用 {relativeTime(session.lastUsedAt)}</p>
            </div>
          </div>
          <div className="relative mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat icon={<MessageSquare className="h-3 w-3" />} label="消息数" value={session.messageCount.toLocaleString()} />
            <Stat icon={<Clock className="h-3 w-3" />} label="运行时长" value={formatUptime(session.startedAt)} />
            <Stat icon={<Activity className="h-3 w-3" />} label="最近活跃" value={relativeTime(session.lastUsedAt)} />
            <Stat icon={<Hash className="h-3 w-3" />} label="插件修订" value={String(session.pluginRevision ?? '—')} />
          </div>
        </div>

        <div className="space-y-4 px-5 py-4">
          <div className="tint-green rounded-2xl border border-line bg-glass p-3.5">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <h3 className="flex items-center gap-1.5 text-[13px] font-semibold text-fg"><Zap className="h-3.5 w-3.5 text-tint" />Token 用量</h3>
                <p className="mt-0.5 text-[11px] text-fg-3">近 14 日统计 · 输入 / 输出 / 上下文</p>
              </div>
              <Badge tone={usage.known ? 'green' : 'neutral'}>
                {usage.total !== null ? `${formatNumber(usage.total)} tokens` : '未知'}
              </Badge>
            </div>
            {usage.trend.length > 1 ? (
              <Sparkline data={usage.trend} className="mt-2 h-10" />
            ) : (
              <div className="mt-2 flex h-10 items-center rounded-lg border border-dashed border-[var(--glass-border-strong)] px-3 text-[11px] text-fg-4">
                没有可绘制的 14 日趋势
              </div>
            )}
            <div className="mt-2 grid grid-cols-3 gap-2">
              <MiniStat label="输入" value={usage.input !== null ? formatNumber(usage.input) : '—'} />
              <MiniStat label="输出" value={usage.output !== null ? formatNumber(usage.output) : '—'} />
              <MiniStat label="上下文" value={contextPct !== null ? `${contextPct}%` : '—'} />
            </div>
            <div className="mt-2.5">
              <div className="mb-1 flex justify-between text-[10.5px] text-fg-4">
                <span>上下文占用 {usage.contextSource === 'estimated' ? '(估算)' : ''}</span>
                <span className="num font-mono">
                  {usage.contextUsed !== null ? formatNumber(usage.contextUsed) : '—'} / {usage.contextWindow !== null ? formatNumber(usage.contextWindow) : '—'}
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-glass-hover">
                <div
                  className={cn('h-full rounded-full bg-[linear-gradient(90deg,rgb(var(--tint)/0.55),rgb(var(--tint)))]', contextPct === null ? 'tint-neutral' : contextPct > 80 ? 'tint-red' : contextPct > 55 ? 'tint-orange' : 'tint-green')}
                  style={{ width: `${contextPct ?? 0}%`, transition: 'width 500ms var(--ease-out)' }}
                />
              </div>
            </div>
            {!usage.known && <p className="mt-2 text-[10.5px] text-fg-4">统计服务未返回此实例的用量, 显示为未知而不是 0.</p>}
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Tabs size="sm" label="实例详情" value={tab} onChange={setTab} items={[
              { value: 'overview', label: '概览' },
              { value: 'plugins', label: '插件', count: session.plugins.length || undefined },
              { value: 'config', label: '配置' },
            ]} />
            <span className="ml-auto flex items-center gap-2 text-[11px] text-fg-4">
              <StatusDot tone={session.active ? 'green' : 'neutral'} live={session.active} />
              {session.active ? (backendRunning ? '运行时热管理' : '后端已停止') : '持久化实例'}
            </span>
          </div>

          {tab === 'overview' && (
            <div className="space-y-3 animate-fade">
              {(session.config.error || session.lastError) && (
                <div className="tint-red flex gap-2 rounded-xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)] px-3 py-2 text-xs text-fg-2">
                  <Gauge className="mt-0.5 h-3.5 w-3.5 flex-none text-tint" />
                  <span>{session.config.error || session.lastError}</span>
                </div>
              )}
              <div className="rounded-2xl border border-line bg-glass px-3.5 py-1">
                <KeyValueRow label="会话状态">{session.active ? '活跃, 可接收消息' : '已持久化, 未加载运行时'}</KeyValueRow>
                <KeyValueRow label="配置状态">{config ? <Badge tone={config.tone}>{config.label}</Badge> : '不适用 (会话类)'}</KeyValueRow>
                <KeyValueRow label="插件状态">{plugins.tone === 'neutral' ? plugins.label : <Badge tone={plugins.tone}>{plugins.label}</Badge>}</KeyValueRow>
                <KeyValueRow label="Provider"><span className="font-mono">{session.provider}</span></KeyValueRow>
                <KeyValueRow label="Agent / 会话类"><span className="font-mono">{session.sessionType}</span></KeyValueRow>
                <KeyValueRow label="平台实例"><span className="font-mono">{session.platformId}</span></KeyValueRow>
                <KeyValueRow label="会话 ID"><span className="break-all font-mono text-[11px]">{session.sessionId}</span></KeyValueRow>
                {session.raw.route && !session.raw.route.current && <KeyValueRow label="路由">历史绑定 (已被新实例替换)</KeyValueRow>}
                <KeyValueRow label="配置修订">{session.config.revision ?? '—'}</KeyValueRow>
                <KeyValueRow label="已应用修订"><span className="font-mono">{session.config.appliedRevision ?? '—'}</span></KeyValueRow>
                <KeyValueRow label="插件修订">{session.pluginRevision ?? '—'}</KeyValueRow>
                <KeyValueRow label="激活时间">{session.startedAt ? formatDateTime(session.startedAt) : '—'}</KeyValueRow>
                <KeyValueRow label="配置应用时间">{session.config.appliedAt ? formatDateTime(session.config.appliedAt) : '—'}</KeyValueRow>
                <KeyValueRow label="创建时间">{formatDateTime(session.createdAt)}</KeyValueRow>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                <button type="button" onClick={onOpenRag} className="flex items-center gap-2.5 rounded-xl border border-line bg-glass px-3 py-2.5 text-left transition hover:bg-glass-hover">
                  <span className="grid h-8 w-8 place-items-center rounded-lg bg-[rgb(var(--rgb-green)/0.14)] text-green"><Database className="h-4 w-4" /></span>
                  <span className="min-w-0"><span className="block text-[12.5px] font-medium text-fg">知识库</span><span className="block text-[11px] text-fg-3">管理此会话的 RAG 知识与文档</span></span>
                </button>
                <button type="button" onClick={onOpenPlugins} className="flex items-center gap-2.5 rounded-xl border border-line bg-glass px-3 py-2.5 text-left transition hover:bg-glass-hover">
                  <span className="grid h-8 w-8 place-items-center rounded-lg bg-[rgb(var(--rgb-teal)/0.14)] text-teal"><Settings2 className="h-4 w-4" /></span>
                  <span className="min-w-0"><span className="block text-[12.5px] font-medium text-fg">会话插件参数</span><span className="block text-[11px] text-fg-3">仅覆盖当前实例, 其余参数继续继承</span></span>
                </button>
              </div>
            </div>
          )}

          {tab === 'plugins' && (
            <div className="space-y-2 animate-fade">
              {session.provider !== 'edictum' ? (
                <div className="rounded-2xl border border-line">
                  <EmptyState tint="teal" icon={<Settings2 className="h-5 w-5" />} title="会话类实例不加载 Edictum 插件" description="插件能力只属于 Edictum 命名会话; 会话类实例可通过会话参数继承配置." className="py-8" />
                </div>
              ) : session.plugins.length === 0 ? (
                <div className="rounded-2xl border border-line">
                  <EmptyState tint="teal" icon={<Settings2 className="h-5 w-5" />} title="当前实例没有插件" description="在 Agent 配置的插件管理中添加插件后, 活跃实例会在保存时同步." className="py-8" />
                </div>
              ) : (
                <>
                  {pluginIssues.length > 0 && (
                    <div className="tint-orange flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[rgb(var(--tint)/0.24)] bg-[rgb(var(--tint)/0.08)] px-3 py-2 text-xs text-fg-2">
                      <span>{pluginIssues.length} 个插件存在漂移或加载问题, 可重试同步或按冷配置重新激活.</span>
                      {canRetry && <Button size="sm" variant="tinted" tint="orange" icon={<RotateCcw className={cn('h-3.5 w-3.5', retrying && 'animate-spin-slow')} />} loading={retrying} onClick={onRetry}>重试同步</Button>}
                    </div>
                  )}
                  {session.plugins.map((plugin) => (
                    <div key={plugin.name} className="rounded-xl border border-line bg-glass px-3 py-2.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-[12.5px] font-medium text-fg">{plugin.name}</span>
                        <Badge tone={pluginTone(plugin.status)} dot>{pluginLabel[plugin.status] ?? plugin.status}</Badge>
                        {!plugin.enabled && <Badge>已停用</Badge>}
                        {plugin.drift && <Badge tone="orange">漂移</Badge>}
                        {plugin.restart_required && <Badge tone="orange">需重启</Badge>}
                        <span className="num ml-auto font-mono text-[10.5px] text-fg-4">rev {plugin.revision ?? '—'}</span>
                      </div>
                      {(plugin.error || plugin.last_error) && <p className="mt-1.5 break-words text-[11.5px] text-red">{plugin.error || plugin.last_error}</p>}
                      {plugin.capabilities && (
                        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[10.5px] text-fg-4">
                          {Object.entries(plugin.capabilities.desired ?? {}).map(([kind, values]) => (
                            <span key={kind}>{kind}: {Object.entries(values).filter(([, enabled]) => enabled).length} 项期望</span>
                          ))}
                          {Object.keys(plugin.capabilities.desired ?? {}).length === 0 && <span>能力快照为空</span>}
                        </div>
                      )}
                    </div>
                  ))}
                  <p className="rounded-xl border border-line bg-glass px-3 py-2 text-[11px] text-fg-4">
                    插件状态来自运行时快照. 修改插件配置请前往 Agent 配置的插件管理, 只影响当前实例的参数请在「会话插件参数」中覆盖.
                  </p>
                </>
              )}
            </div>
          )}

          {tab === 'config' && (
            <div className="space-y-3 animate-fade">
              <div className="rounded-2xl border border-line bg-glass px-3.5 py-1">
                <KeyValueRow label="模型"><span className="font-mono">{String(session.sessionConfig.model_name ?? session.sessionConfig.model ?? '—')}</span></KeyValueRow>
                <KeyValueRow label="思考强度">{String(session.sessionConfig.thinking ?? '默认')}</KeyValueRow>
                <KeyValueRow label="命令历史">{session.sessionConfig.command_history_enabled ? '写入上下文' : '不写入'}</KeyValueRow>
                <KeyValueRow label="配置状态">{config ? <Badge tone={config.tone}>{config.label}</Badge> : '不适用 (会话类)'}</KeyValueRow>
                <KeyValueRow label="错误详情">{session.config.error || '无'}</KeyValueRow>
              </div>
              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <h3 className="text-[12.5px] font-medium text-fg">会话参数快照</h3>
                  <span className="text-[10.5px] text-fg-4">session_config</span>
                </div>
                <pre className="scroll max-h-72 rounded-xl border border-line bg-[rgb(0_0_0/0.22)] p-3 font-mono text-[11.5px] leading-5 text-fg-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)]">
                  {JSON.stringify(session.sessionConfig, null, 2)}
                </pre>
              </div>
              {appliedConfig !== undefined && (
                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <h3 className="text-[12.5px] font-medium text-fg">运行时已应用配置</h3>
                    <span className="text-[10.5px] text-fg-4">runtime.config.applied</span>
                  </div>
                  <pre className="scroll max-h-72 rounded-xl border border-line bg-[rgb(0_0_0/0.22)] p-3 font-mono text-[11.5px] leading-5 text-fg-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)]">
                    {JSON.stringify(appliedConfig, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-5 py-3">
        <span className="text-[11px] text-fg-4">实例级操作仅影响当前平台会话</span>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="ghost" icon={<BookOpen className="h-3.5 w-3.5" />} onClick={onOpenRag}>知识库</Button>
          <Button size="sm" variant="ghost" icon={<Settings2 className="h-3.5 w-3.5" />} onClick={onOpenPlugins}>插件参数</Button>
          {canRetry && <Button size="sm" variant="tinted" tint="orange" icon={<RotateCcw className={cn('h-3.5 w-3.5', retrying && 'animate-spin-slow')} />} loading={retrying} onClick={onRetry}>重试同步</Button>}
          <Button size="sm" variant="danger" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={onDelete}>删除实例</Button>
        </div>
      </div>
    </Drawer>
  );
}

function Stat({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="rounded-xl border border-line bg-glass px-2.5 py-2">
      <div className="flex items-center gap-1 text-[10.5px] text-fg-4">{icon}{label}</div>
      <div className="num mt-0.5 truncate font-mono text-[12.5px] font-semibold text-fg">{value}</div>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-glass px-2 py-1.5">
      <div className="text-[10px] text-fg-4">{label}</div>
      <div className="num font-mono text-xs text-fg-2">{value}</div>
    </div>
  );
}

function KeyValueRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line py-2.5 last:border-0">
      <span className="flex-none text-[12.5px] text-fg-3">{label}</span>
      <span className="min-w-0 text-right text-[12.5px] text-fg-2">{children}</span>
    </div>
  );
}
