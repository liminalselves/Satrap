import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  AlertCircle, ArrowLeft, BookOpen, Boxes, ChevronRight, Clock, MessageSquare, RefreshCw, RotateCcw,
  Search, Settings2, Sparkles, Trash2, Zap,
} from 'lucide-react';
import { Badge, StatusDot } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { Card } from '@/components/kit/Card';
import { CountUp, EmptyState, PageHeader, Skeleton, Sparkline } from '@/components/kit/Feedback';
import { Input } from '@/components/kit/Field';
import { Modal } from '@/components/kit/Modal';
import { Tabs } from '@/components/kit/Tabs';
import { toast } from '@/components/kit/Toast';
import { useConfirm } from '@/hooks/useConfirm';
import { useBackendStore } from '@/stores/useBackendStore';
import { sessionApi } from '@/api/session';
import { edictumApi } from '@/api/edictum';
import { controlApi } from '@/api/control';
import { groupApi } from '@/api/groups';
import { RagManager } from '@/components/common/RagManager';
import { SessionPluginSettingsModal } from '@/components/common/SessionPluginSettingsModal';
import type { RuntimeSession } from '@/api/types';
import { cn } from '@/utils/cn';
import {
  canRetryRuntime, configState, coverageText, errorMessage, formatCompact, formatDateTime,
  normalizeSession, pluginState, relativeTime, runtimeKey, sessionTone, usageFor, useSessionMetrics,
  type SessionUsageView, type SessionView,
} from './shared';
import { InstanceInspector } from './InstanceInspector';
import { ManualWakeModal } from './ManualWakeModal';

type Filter = 'all' | 'active' | 'persisted' | 'issues';

const PLATFORM_SESSION_EXCLUDED_IDS = new Set(['chat']);

const providerIcon = (session: SessionView) => (
  session.provider === 'edictum' ? <Sparkles className="h-3 w-3" /> : <Boxes className="h-3 w-3" />
);

function TokenCell({ usage, session }: { usage: SessionUsageView; session: SessionView }) {
  return (
    <div className="tint-green flex items-center gap-2">
      {usage.trend.length > 1 && <Sparkline data={usage.trend} className="h-6 w-16" fill={false} />}
      <span className="num font-mono text-[10.5px] text-fg-3">
        {usage.total !== null ? formatCompact(usage.total) : '—'}
      </span>
      <span className="sr-only">{`${session.platformId}/${session.sessionId} 近 14 日 token 用量`}</span>
    </div>
  );
}

/** 主行: 路由目标 + 短码; 完整 ID 由外层 title 与检查器提供 */
function SessionTitle({ session }: { session: SessionView }) {
  return (
    <div className="flex min-w-0 items-baseline gap-1.5 text-[12.5px] font-medium text-fg">
      <span className="truncate">{session.target ?? '未绑定对话'}</span>
      <span className="flex-none font-mono text-[11px] text-fg-3">{session.shortId}</span>
    </div>
  );
}

export function InstancesPanel() {
  const confirm = useConfirm('instances');
  const [params] = useSearchParams();
  const groupAdapter = params.get('groupAdapter') ?? '';
  const groupAccount = params.get('groupAccount') ?? '';
  const groupId = params.get('groupId') ?? '';
  const groupFilter = Boolean(groupAdapter && groupAccount && groupId);
  const sessionRunning = useBackendStore((state) => state.isRunning);
  const [sessions, setSessions] = useState<RuntimeSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [lastRefresh, setLastRefresh] = useState('刚刚');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [inspectorKey, setInspectorKey] = useState<string | null>(null);
  const [ragSession, setRagSession] = useState<SessionView | null>(null);
  const [pluginSession, setPluginSession] = useState<SessionView | null>(null);
  const [overridePlugins, setOverridePlugins] = useState<string[]>([]);
  const [wakeOpen, setWakeOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [retryingKey, setRetryingKey] = useState<string | null>(null);
  const [groupSessionIds, setGroupSessionIds] = useState<string[] | null>(null);
  const [groupError, setGroupError] = useState('');
  const [groupLoading, setGroupLoading] = useState(groupFilter);

  const metrics = useSessionMetrics(true);
  const views = useMemo(() => sessions.map(normalizeSession), [sessions]);

  const fetchSessions = useCallback(async () => {
    try {
      const result = await sessionApi.listRuntime(sessionRunning);
      const next = (result.sessions || []).filter((session) => !PLATFORM_SESSION_EXCLUDED_IDS.has(session.platform_id));
      setSessions(next);
      setLoadError('');
      const available = new Set(next.map(runtimeKey));
      setSelected((current) => new Set(Array.from(current).filter((key) => available.has(key))));
      setInspectorKey((current) => (current && available.has(current) ? current : null));
    } catch (error) {
      setSessions([]);
      setLoadError(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, [sessionRunning]);

  useEffect(() => { void fetchSessions(); }, [fetchSessions]);

  useEffect(() => {
    setSelected(new Set());
    if (!groupFilter) { setGroupSessionIds(null); setGroupError(''); setGroupLoading(false); return; }
    let cancelled = false;
    setGroupLoading(true);
    setGroupSessionIds(null);
    setGroupError('');
    groupApi.config(groupAdapter, groupId, groupAccount, sessionRunning).then((result) => {
      if (!cancelled) setGroupSessionIds(result.session_instances?.session_ids || []);
    }).catch((error) => {
      if (!cancelled) setGroupError(errorMessage(error));
    }).finally(() => {
      if (!cancelled) setGroupLoading(false);
    });
    return () => { cancelled = true; };
  }, [groupAdapter, groupAccount, groupId, groupFilter, sessionRunning]);

  useEffect(() => {
    if (!pluginSession) return;
    let cancelled = false;
    setOverridePlugins(pluginSession.plugins.map((plugin) => plugin.name));
    controlApi.listEdictumPlugins().then((plugins) => {
      if (!cancelled) setOverridePlugins(plugins.map((plugin) => plugin.name));
    }).catch(() => { /* 读取失败时保留运行时插件名 */ });
    return () => { cancelled = true; };
  }, [pluginSession]);

  const groupSessions = useMemo(() => groupFilter
    ? views.filter((session) => session.platformId === groupAdapter && Boolean(groupSessionIds?.includes(session.sessionId)))
    : views, [groupFilter, views, groupAdapter, groupSessionIds]);

  const counts = useMemo(() => {
    const active = groupSessions.filter((session) => session.active).length;
    const issues = groupSessions.filter((session) => session.active && (sessionTone(session) === 'orange' || sessionTone(session) === 'red')).length;
    return { all: groupSessions.length, active, persisted: groupSessions.length - active, issues };
  }, [groupSessions]);

  const visible = useMemo(() => {
    const text = query.trim().toLowerCase();
    return groupSessions.filter((session) => {
      if (filter === 'active' && !session.active) return false;
      if (filter === 'persisted' && session.active) return false;
      if (filter === 'issues' && !(session.active && (sessionTone(session) === 'orange' || sessionTone(session) === 'red'))) return false;
      if (!text) return true;
      return `${session.platformId} ${session.sessionId} ${session.target ?? ''} ${session.sessionType} ${session.provider}`.toLowerCase().includes(text);
    });
  }, [groupSessions, filter, query]);

  const allSelected = visible.length > 0 && visible.every((session) => selected.has(session.key));
  const inspected = views.find((session) => session.key === inspectorKey) ?? null;
  const totalMessages = groupSessions.reduce((total, session) => total + session.messageCount, 0);
  const knownTotals = groupSessions
    .map((session) => usageFor(metrics, session).total)
    .filter((value): value is number => value !== null);
  const totalTokens = knownTotals.length > 0 ? knownTotals.reduce((sum, value) => sum + value, 0) : null;

  const refresh = () => {
    setRefreshing(true);
    void fetchSessions().then(() => {
      setRefreshing(false);
      setLastRefresh(new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }));
      toast.success('会话实例已刷新', '运行时快照与消息计数已同步');
    });
  };

  const toggleSelected = (key: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  const toggleAll = (checked: boolean) => {
    setSelected(checked ? new Set(visible.map((session) => session.key)) : new Set());
  };

  const retryRuntime = async (session: SessionView) => {
    setRetryingKey(session.key);
    try {
      const requiresRestart = session.config.drift
        || session.config.status === 'restart_pending'
        || session.pluginSummary.restart_required > 0;
      const applied = requiresRestart
        ? await sessionApi.restartRuntime(session.platformId, session.sessionId)
        : (await edictumApi.retryRuntimeChanges({
          session_refs: [{ platform_id: session.platformId, session_id: session.sessionId }],
        })).edictum_sessions[0];
      const message = applied?.ok
        ? (requiresRestart ? '会话已按冷配置重新激活' : '插件状态已同步')
        : '插件仍存在漂移, 请查看错误详情';
      if (applied?.ok) toast.success(message);
      else toast.warning(message);
      await fetchSessions();
    } catch (error) {
      toast.error('插件重试失败', errorMessage(error));
    } finally {
      setRetryingKey(null);
    }
  };

  const deleteSessions = async (refs: Array<{ platform_id: string; session_id: string }>, label: string, mode: 'single' | 'bulk') => {
    setDeleting(true);
    try {
      if (mode === 'single') {
        await sessionApi.deleteRuntime(refs[0].platform_id, refs[0].session_id, sessionRunning);
        toast.success(`会话实例已删除: ${refs[0].session_id}`);
      } else {
        const result = await sessionApi.bulkDeleteRuntime({
          mode: label === 'empty' ? 'empty' : label === 'single' ? 'single' : 'selected',
          session_refs: label === 'selected' ? refs : undefined,
        }, sessionRunning);
        toast.success(`已删除 ${result.deleted_count} 个会话实例`);
      }
      setSelected(new Set());
      setInspectorKey(null);
      await fetchSessions();
    } catch (error) {
      toast.error(mode === 'single' ? '删除失败' : '批量删除失败', errorMessage(error));
    } finally {
      setDeleting(false);
    }
  };

  const requestBulk = async (mode: 'empty' | 'single' | 'selected') => {
    if (groupFilter && mode !== 'selected') return;
    const pool = groupFilter ? groupSessions : views;
    const refs = mode === 'selected'
      ? visible.filter((session) => selected.has(session.key)).map((session) => ({ platform_id: session.platformId, session_id: session.sessionId }))
      : pool.filter((session) => session.messageCount === (mode === 'empty' ? 0 : 1)).map((session) => ({ platform_id: session.platformId, session_id: session.sessionId }));
    if (mode === 'selected' && refs.length === 0) {
      toast.warning('请先选择要删除的会话实例');
      return;
    }
    if (refs.length === 0) {
      toast.info(mode === 'empty' ? '没有消息数为 0 的会话实例' : '没有消息数为 1 的会话实例');
      return;
    }
    const description = mode === 'empty'
      ? '所有消息数为 0 的会话实例'
      : mode === 'single'
        ? '所有消息数为 1 的会话实例'
        : `选中的 ${refs.length} 个会话实例`;
    if (!(await confirm(`确定要删除${description}吗? 此操作会同时清理用户绑定和上下文路由`))) return;
    await deleteSessions(refs, mode, 'bulk');
  };

  const requestDelete = async (session: SessionView) => {
    if (!(await confirm(`确定要删除 ${session.platformId} 的会话实例 "${session.sessionId}" 吗? 此操作会同时清理用户绑定和上下文路由`))) return;
    await deleteSessions([{ platform_id: session.platformId, session_id: session.sessionId }], 'single', 'single');
  };

  const hasFilter = query.trim().length > 0 || filter !== 'all';
  const coverage = metrics.sources.length > 0 || metrics.warnings.length > 0;

  return (
    <div className="space-y-5 pb-6">
      <PageHeader
        eyebrow={<Link to="/conversations" className="inline-flex items-center gap-1 text-xs text-fg-3 transition hover:text-fg"><ArrowLeft className="h-3 w-3" />对话记录</Link>}
        title="对话实例"
        description="已持久化的平台 Provider 会话实例, 不包含 Chat 对话历史"
        actions={<>
          <Badge tone={sessionRunning ? 'green' : 'orange'} dot live={sessionRunning}>
            {sessionRunning ? '后端热管理' : '冷管理'}
          </Badge>
          <span className="hidden text-[11px] text-fg-4 sm:inline">更新于 {lastRefresh}</span>
          <Button size="sm" icon={<RefreshCw className={cn('h-4 w-4', refreshing && 'animate-spin-slow')} />} onClick={refresh} disabled={refreshing} title="刷新会话实例">刷新</Button>
          <Button size="sm" variant="tinted" tint="green" icon={<Zap className="h-4 w-4" />} disabled={!sessionRunning} onClick={() => setWakeOpen(true)}>手动唤醒群聊</Button>
        </>}
      />

      {groupFilter && (
        <Card tint="green" padding="md" className="animate-rise">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <p className="flex items-center gap-2 text-[13px] font-medium text-fg">
                <StatusDot tone={groupError ? 'red' : 'green'} live={!groupError} />
                筛选机器人 {groupAccount} 的群 {groupId}
              </p>
              <p className="mt-1 text-xs text-fg-3">
                {groupError ? groupError : groupLoading ? '正在读取群会话归属…' : `已归属 ${groupSessionIds?.length ?? 0} 个范围会话. 旧版按用户共享的历史无法按群筛选.`}
              </p>
              {groupError && <p role="alert" className="mt-1 text-xs text-red">{groupError}</p>}
            </div>
            <Link to="/conversations/instances" className="ctl ctl-ghost ctl-sm text-green">清除筛选</Link>
          </div>
        </Card>
      )}

      <Card tint="green" padding="md" className="animate-rise" style={{ animationDelay: '40ms' }}>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
            {[
              { label: '本地实例', value: counts.all, icon: <Boxes className="h-3.5 w-3.5" /> },
              { label: '活跃', value: counts.active, icon: <StatusDot tone="green" live /> },
              { label: '消息总数', value: totalMessages, icon: <MessageSquare className="h-3.5 w-3.5" /> },
              { label: '近 14 日 Token', value: totalTokens, icon: <Sparkles className="h-3.5 w-3.5" /> },
              { label: '需处理', value: counts.issues, icon: <AlertCircle className="h-3.5 w-3.5" /> },
            ].map((stat) => (
              <div key={stat.label}>
                <div className="flex items-center gap-1 text-[11px] text-fg-4">{stat.icon}{stat.label}</div>
                <div className={cn('display mt-0.5 text-[20px] font-semibold leading-none', stat.label === '需处理' && stat.value !== null && stat.value > 0 ? 'text-orange' : 'text-fg')}>
                  {stat.value === null ? '—' : <CountUp value={stat.value} />}
                </div>
              </div>
            ))}
          </div>
          <div className="flex flex-col items-end gap-1 text-[11px] text-fg-4">
            <span className="flex items-center gap-2">
              <StatusDot tone="green" live />
              {sessionRunning ? '运行时快照实时同步' : '后端已停止, 展示持久化快照'}
            </span>
            {coverage && (
              <span title={[...metrics.warnings, coverageText(metrics.sources)].filter(Boolean).join('\n')} className="flex items-center gap-1.5">
                <Badge tone={metrics.complete ? 'green' : 'orange'}>{metrics.complete ? '统计完整' : '统计不完整'}</Badge>
                {metrics.loading && <span>读取统计…</span>}
              </span>
            )}
          </div>
        </div>
      </Card>

      <Card padding="none" className="animate-rise" style={{ animationDelay: '70ms' }}>
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2.5 sm:px-4">
          <Tabs
            size="sm"
            label="实例筛选"
            value={filter}
            onChange={setFilter}
            items={[
              { value: 'all', label: '全部', count: counts.all },
              { value: 'active', label: '活跃', count: counts.active },
              { value: 'persisted', label: '已持久化', count: counts.persisted },
              { value: 'issues', label: '需处理', count: counts.issues },
            ]}
          />
          <div className="w-44 max-w-[44vw] sm:w-56">
            <Input leading={<Search className="h-4 w-4" />} placeholder="搜索平台、群号或 Session ID…" value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索会话实例" />
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <Button size="sm" variant="ghost" disabled={deleting || groupFilter} onClick={() => void requestBulk('empty')} className="text-red hover:text-red">删除无消息</Button>
            <Button size="sm" variant="ghost" disabled={deleting || groupFilter} onClick={() => void requestBulk('single')} className="text-red hover:text-red">删除仅 1 条</Button>
            <Button size="sm" variant="danger" disabled={deleting || selected.size === 0} onClick={() => void requestBulk('selected')}>
              删除所选{selected.size > 0 ? ` (${selected.size})` : ''}
            </Button>
          </div>
        </div>

        {loadError && (
          <div role="alert" className="border-b border-line bg-[rgb(var(--rgb-red)/0.06)] px-4 py-2 text-xs text-red">
            读取会话实例失败: {loadError}
          </div>
        )}

        {loading ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2, 3, 4, 5, 6].map((index) => (
              <div key={index} className="flex items-center gap-3">
                <Skeleton className="h-4 w-4" />
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-4 flex-1" />
                <Skeleton className="h-4 w-20" />
                <Skeleton className="h-4 w-16" />
              </div>
            ))}
          </div>
        ) : visible.length === 0 ? (
          <EmptyState
            tint="green"
            icon={<Boxes className="h-6 w-6" />}
            title={groupError ? '无法读取此群的实例' : hasFilter ? '没有符合条件的会话实例' : '暂无会话实例'}
            description={groupError ? '请清除筛选后查看全部实例, 或确认群号与机器人账号是否正确' : groupFilter ? '实例可能尚未创建, 或尚未在此平台加载' : hasFilter ? '调整筛选条件或搜索关键字' : '在 Agent 配置中创建会话后, 实例会出现在这里'}
            actions={groupFilter
              ? <Link to="/conversations/instances" className="ctl ctl-sm">清除筛选</Link>
              : hasFilter
                ? <Button onClick={() => { setQuery(''); setFilter('all'); }}>清除筛选</Button>
                : <Button variant="tinted" tint="green" icon={<Zap className="h-4 w-4" />} disabled={!sessionRunning} onClick={() => setWakeOpen(true)}>手动唤醒</Button>}
          />
        ) : (
          <>
            {/* 桌面表格 */}
            <div className="scroll hidden max-h-[calc(100vh-350px)] overflow-auto lg:block">
              <table className="w-full min-w-[980px] text-sm">
                <thead className="sticky top-0 z-[2] bg-[var(--glass-bg-strong)] backdrop-blur-xl">
                  <tr className="whitespace-nowrap border-b border-line text-left text-[10.5px] uppercase tracking-wider text-fg-4">
                    <th className="w-10 px-3 py-2.5">
                      <input type="checkbox" aria-label="选择全部会话实例" checked={allSelected} onChange={(event) => toggleAll(event.target.checked)} className="h-3.5 w-3.5 accent-[rgb(var(--rgb-green))]" />
                    </th>
                    <th className="px-2 py-2.5 font-medium">会话</th>
                    <th className="px-2 py-2.5 font-medium">Agent</th>
                    <th className="px-2 py-2.5 font-medium">状态</th>
                    <th className="px-2 py-2.5 font-medium">配置</th>
                    <th className="px-2 py-2.5 font-medium">插件</th>
                    <th className="px-2 py-2.5 text-right font-medium">消息</th>
                    <th className="px-2 py-2.5 font-medium">最近使用</th>
                    <th className="px-2 py-2.5 font-medium">Token (14日)</th>
                    <th className="px-3 py-2.5 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((session) => {
                    const config = configState(session);
                    const plugins = pluginState(session);
                    const usage = usageFor(metrics, session);
                    const canRetry = canRetryRuntime(session, sessionRunning);
                    return (
                      <tr
                        key={session.key}
                        tabIndex={0}
                        onClick={() => setInspectorKey(session.key)}
                        onKeyDown={(event) => { if (event.key === 'Enter') setInspectorKey(session.key); }}
                        className="cursor-pointer whitespace-nowrap border-b border-line transition-colors last:border-0 hover:bg-glass-hover focus-visible:bg-glass-hover"
                      >
                        <td className="px-3 py-2.5" onClick={(event) => event.stopPropagation()}>
                          <input
                            type="checkbox"
                            aria-label={`选择会话实例 ${session.sessionId}`}
                            checked={selected.has(session.key)}
                            onChange={(event) => toggleSelected(session.key, event.target.checked)}
                            className="h-3.5 w-3.5 accent-[rgb(var(--rgb-green))]"
                          />
                        </td>
                        <td className="px-2 py-2.5">
                          <div className="flex items-center gap-2">
                            <span className={cn('grid h-7 w-7 flex-none place-items-center rounded-lg', session.active ? 'bg-[rgb(var(--rgb-green)/0.14)] text-green' : 'bg-glass-hover text-fg-3')}>
                              <Boxes className="h-3.5 w-3.5" />
                            </span>
                            <div className="min-w-0 max-w-[360px]" title={session.sessionId}>
                              <SessionTitle session={session} />
                              <div className="truncate font-mono text-[10.5px] text-fg-4">{session.platformId}</div>
                            </div>
                          </div>
                        </td>
                        <td className="px-2 py-2.5">
                          <div className="flex items-center gap-1.5">
                            <span className={cn('flex items-center gap-1', session.provider === 'edictum' ? 'text-teal' : 'text-accent')}>{providerIcon(session)}</span>
                            <div className="min-w-0">
                              <div className="max-w-[150px] truncate text-[12px] text-fg-2">{session.sessionType}</div>
                              <div className="text-[10px] text-fg-4">{session.provider === 'edictum' ? 'Edictum' : '会话类'}</div>
                            </div>
                          </div>
                        </td>
                        <td className="px-2 py-2.5">
                          <Badge tone={session.active ? 'green' : 'neutral'} dot live={session.active}>{session.active ? '活跃' : '已持久化'}</Badge>
                        </td>
                        <td className="px-2 py-2.5">
                          {config ? <span title={config.title}><Badge tone={config.tone}>{config.label}</Badge></span> : <span className="text-xs text-fg-4">—</span>}
                        </td>
                        <td className="px-2 py-2.5">
                          {plugins.tone === 'neutral'
                            ? <span className="text-xs text-fg-4">{plugins.label}</span>
                            : <span title={plugins.title}><Badge tone={plugins.tone}>{plugins.label}</Badge></span>}
                        </td>
                        <td className="num px-2 py-2.5 text-right font-mono text-xs text-fg-2">{session.messageCount.toLocaleString()}</td>
                        <td className="px-2 py-2.5">
                          <div className="text-[12px] text-fg-2">{relativeTime(session.lastUsedAt)}</div>
                          <div className="num font-mono text-[10px] text-fg-4">{formatDateTime(session.lastUsedAt)}</div>
                        </td>
                        <td className="px-2 py-2.5"><TokenCell usage={usage} session={session} /></td>
                        <td className="px-3 py-2.5">
                          <div className="flex items-center justify-end gap-0.5" onClick={(event) => event.stopPropagation()}>
                            <IconButton label={`知识库 ${session.sessionId}`} title="知识库" size="sm" onClick={() => setRagSession(session)}><BookOpen className="h-3.5 w-3.5" /></IconButton>
                            <IconButton label={`会话插件参数 ${session.sessionId}`} title="会话插件参数" size="sm" onClick={() => setPluginSession(session)}><Settings2 className="h-3.5 w-3.5" /></IconButton>
                            {canRetry && (
                              <IconButton label={`重试会话插件 ${session.sessionId}`} title="重试会话插件" size="sm" disabled={retryingKey !== null} onClick={() => void retryRuntime(session)}>
                                <RotateCcw className={cn('h-3.5 w-3.5', retryingKey === session.key && 'animate-spin-slow')} />
                              </IconButton>
                            )}
                            <IconButton label={`删除会话实例 ${session.sessionId}`} title="删除会话实例" size="sm" disabled={deleting} onClick={() => void requestDelete(session)} className="text-red hover:text-red"><Trash2 className="h-3.5 w-3.5" /></IconButton>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* 窄屏卡片 */}
            <div className="space-y-2 p-3 lg:hidden">
              {visible.map((session) => {
                const config = configState(session);
                const plugins = pluginState(session);
                const usage = usageFor(metrics, session);
                const canRetry = canRetryRuntime(session, sessionRunning);
                return (
                  <div
                    key={session.key}
                    role="button"
                    tabIndex={0}
                    onClick={() => setInspectorKey(session.key)}
                    onKeyDown={(event) => { if (event.key === 'Enter') setInspectorKey(session.key); }}
                    className="rounded-xl border border-line bg-glass p-3 transition-colors hover:bg-glass-hover"
                  >
                    <div className="flex items-start gap-2">
                      <input
                        type="checkbox"
                        aria-label={`选择会话实例 ${session.sessionId}`}
                        checked={selected.has(session.key)}
                        onClick={(event) => event.stopPropagation()}
                        onChange={(event) => toggleSelected(session.key, event.target.checked)}
                        className="mt-1 h-3.5 w-3.5 flex-none accent-[rgb(var(--rgb-green))]"
                      />
                      <div className="min-w-0 flex-1" title={session.sessionId}>
                        <SessionTitle session={session} />
                        <div className="mt-0.5 text-[10.5px] text-fg-4">{session.platformId} · {session.sessionType} · {session.provider === 'edictum' ? 'Edictum' : '会话类'}</div>
                      </div>
                      <Badge tone={session.active ? 'green' : 'neutral'} dot live={session.active}>{session.active ? '活跃' : '持久化'}</Badge>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {config && <span title={config.title}><Badge tone={config.tone}>{config.label}</Badge></span>}
                      {plugins.tone !== 'neutral' && <span title={plugins.title}><Badge tone={plugins.tone}>{plugins.label}</Badge></span>}
                      <Badge><MessageSquare className="h-3 w-3" />{session.messageCount} 条</Badge>
                      <Badge><Clock className="h-3 w-3" />{relativeTime(session.lastUsedAt)}</Badge>
                    </div>
                    <div className="mt-2 flex items-center justify-between gap-2">
                      <div className="tint-green flex min-w-0 flex-1 items-center gap-2">
                        {usage.trend.length > 1 && <Sparkline data={usage.trend} className="h-6 w-20" fill={false} />}
                        <span className="num font-mono text-[10px] text-fg-4">{usage.total !== null ? `${formatCompact(usage.total)} tok` : '暂无 token'}</span>
                      </div>
                      <div className="flex items-center gap-0.5" onClick={(event) => event.stopPropagation()}>
                        <IconButton label="知识库" size="sm" onClick={() => setRagSession(session)}><BookOpen className="h-3.5 w-3.5" /></IconButton>
                        <IconButton label="插件参数" size="sm" onClick={() => setPluginSession(session)}><Settings2 className="h-3.5 w-3.5" /></IconButton>
                        {canRetry && <IconButton label="重试同步" size="sm" disabled={retryingKey !== null} onClick={() => void retryRuntime(session)}><RotateCcw className={cn('h-3.5 w-3.5', retryingKey === session.key && 'animate-spin-slow')} /></IconButton>}
                        <IconButton label="删除" size="sm" onClick={() => void requestDelete(session)} className="text-red hover:text-red"><Trash2 className="h-3.5 w-3.5" /></IconButton>
                        <ChevronRight className="h-4 w-4 text-fg-4" />
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-4 py-2 text-[10.5px] text-fg-4">
              <span>共 {visible.length} 个实例 · 已选 {selected.size} 个</span>
              <span>{sessionRunning ? '热管理: 创建、删除与重试均直接作用于运行时' : '冷管理: 操作写入持久化配置, 后端启动后生效'}</span>
            </div>
          </>
        )}
      </Card>

      <InstanceInspector
        key={`inspect:${inspected ? inspected.key : 'none'}`}
        open={inspected !== null}
        session={inspected}
        usage={inspected ? usageFor(metrics, inspected) : usageFor(metrics, { platformId: '', sessionId: '' })}
        onClose={() => setInspectorKey(null)}
        backendRunning={sessionRunning}
        retrying={inspected ? retryingKey === inspected.key : false}
        onOpenRag={() => { if (inspected) setRagSession(inspected); }}
        onOpenPlugins={() => { if (inspected) setPluginSession(inspected); }}
        onRetry={() => { if (inspected) void retryRuntime(inspected); }}
        onDelete={() => { if (inspected) void requestDelete(inspected); }}
      />

      <Modal open={ragSession !== null} title="会话知识库" onClose={() => setRagSession(null)} size="lg">
        {ragSession && <RagManager context={{ platformId: ragSession.platformId, sessionId: ragSession.sessionId, via: 'control' }} />}
      </Modal>
      <SessionPluginSettingsModal
        context={pluginSession ? { platformId: pluginSession.platformId, sessionId: pluginSession.sessionId } : null}
        plugins={overridePlugins}
        onClose={() => setPluginSession(null)}
      />
      <ManualWakeModal open={wakeOpen} onClose={() => setWakeOpen(false)} backendRunning={sessionRunning} />
    </div>
  );
}
