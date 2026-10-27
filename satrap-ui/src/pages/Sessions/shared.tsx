import { useEffect, useRef, useState } from 'react';
import { statsApi, sourceStateLabels, type SourceCoverage } from '@/api/contracts';
import type { EdictumSessionConfig, RuntimeSession, SessionClassConfig } from '@/api/types';

/* ------------------------------------------------------------------ 文案与时区 */

export const providerLabel = (provider: string | undefined): string => (
  (provider || 'session_class') === 'edictum' ? 'Edictum' : '会话类'
);

export const providerTone = (provider: string | undefined): 'teal' | 'accent' => (
  (provider || 'session_class') === 'edictum' ? 'teal' : 'accent'
);

export const formatNumber = (value: number | null | undefined): string => (
  value === null || value === undefined || !Number.isFinite(value) ? '—' : value.toLocaleString()
);

export const formatCompact = (value: number | null | undefined): string => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (Math.abs(value) >= 10_000) return `${Math.round(value / 1000)}K`;
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)}K`;
  return value.toLocaleString();
};

export const relativeTime = (seconds: number | null | undefined): string => {
  if (!seconds) return '—';
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - seconds);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
  return new Date(seconds * 1000).toLocaleDateString('zh-CN');
};

export const formatDateTime = (seconds: number | null | undefined): string => {
  if (!seconds) return '—';
  return new Date(seconds * 1000).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
};

export const formatUptime = (startedAt: number | null | undefined): string => {
  if (!startedAt) return '—';
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours >= 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
};

/* ------------------------------------------------------------------ Agent 目录 */

export type Provider = 'session_class' | 'edictum';

/** 目录里的一个命名 Agent; 实例计数与更新时间来自后端元数据, 缺失保持未知 */
export interface AgentEntry {
  name: string;
  provider: Provider;
  enabled: boolean;
  description: string;
  model_name: string;
  class_path?: string;
  is_async?: boolean;
  context_key?: string;
  model_key?: string;
  edictum_type?: string;
  params: Record<string, unknown>;
  plugins: EdictumSessionConfig['plugins'];
  activeInstances: number | null;
  runningInstances: number | null;
  updatedAt: number | null;
  runtimeSource?: {
    state?: string;
    complete?: boolean;
    as_of?: number | null;
    reason_code?: string | null;
  };
}

/** 后端目录附带的实例计数与更新时间; 运行服务离线时计数为 null */
export interface AgentConfigMetadata {
  updated_at?: number | null;
  active_instances?: number | null;
  running_instances?: number | null;
  runtime_source?: AgentEntry['runtimeSource'];
}

const countOrNull = (value: unknown): number | null => (
  typeof value === 'number' && Number.isFinite(value) ? value : null
);

const timeOrNull = (value: unknown): number | null => (
  typeof value === 'number' && Number.isFinite(value) ? value : null
);

/** 会话类模板 -> 统一 Agent 目录项 */
export function sessionClassEntry(name: string, config: SessionClassConfig): AgentEntry {
  const meta = config as SessionClassConfig & AgentConfigMetadata;
  const params = config.params || {};
  const modelKey = config.model_key || '';
  return {
    name,
    provider: 'session_class',
    enabled: config.enabled !== false,
    description: config.description || '',
    model_name: modelKey && params[modelKey] !== undefined ? String(params[modelKey]) : '',
    class_path: config.class_path,
    is_async: Boolean(config.is_async),
    context_key: config.context_key,
    model_key: modelKey || undefined,
    params,
    plugins: [],
    activeInstances: countOrNull(meta.active_instances),
    runningInstances: countOrNull(meta.running_instances),
    updatedAt: timeOrNull(meta.updated_at),
    runtimeSource: meta.runtime_source,
  };
}

/** Edictum 命名配置 -> 统一 Agent 目录项 */
export function edictumEntry(name: string, config: EdictumSessionConfig): AgentEntry {
  const meta = config as EdictumSessionConfig & AgentConfigMetadata;
  return {
    name,
    provider: 'edictum',
    enabled: config.enabled !== false,
    description: config.description || '',
    model_name: config.model_name || '',
    edictum_type: config.edictum_type,
    params: config.params || {},
    plugins: config.plugins || [],
    activeInstances: countOrNull(meta.active_instances),
    runningInstances: countOrNull(meta.running_instances),
    updatedAt: timeOrNull(meta.updated_at),
    runtimeSource: meta.runtime_source,
  };
}

/** 未知计数返回 null, 不能被当作 0 */
export function sumKnown(values: Array<number | null>): number | null {
  return values.every((value): value is number => value !== null) ? values.reduce((total, value) => total + value, 0) : null;
}

/* ------------------------------------------------------------------ 运行时模型 */

export interface RuntimePluginState {
  name: string;
  enabled: boolean;
  status: string;
  error?: string | null;
  last_error?: string | null;
  last_operation_status?: string;
  drift?: boolean;
  restart_required?: boolean;
  revision?: number;
  capabilities?: {
    applied?: Record<string, Record<string, boolean>>;
    desired?: Record<string, Record<string, boolean>>;
  };
}

export interface PluginSummary {
  total: number;
  loaded: number;
  errors: number;
  pending: number;
  drift: number;
  restart_required: number;
}

export interface NormalizedConfigState {
  status: string;
  error?: string | null;
  drift: boolean;
  revision?: number;
  appliedAt?: number | null;
  appliedRevision?: string | null;
  lastRestartedAt?: number | null;
  hasSnapshot: boolean;
}

export interface SessionView {
  raw: RuntimeSession;
  key: string;
  platformId: string;
  sessionId: string;
  active: boolean;
  provider: string;
  sessionType: string;
  createdAt: number;
  lastUsedAt: number;
  messageCount: number;
  sessionConfig: Record<string, unknown>;
  plugins: RuntimePluginState[];
  pluginSummary: PluginSummary;
  pluginRevision?: number;
  config: NormalizedConfigState;
  startedAt?: number;
  lastError?: string;
}

export const runtimeKey = (session: Pick<RuntimeSession, 'platform_id' | 'session_id'>): string => (
  `${session.platform_id}\u0000${session.session_id}`
);

/** 把热/冷两种响应归一成同一展示模型, 缺失字段保持未知而不是伪造 0 */
export function normalizeSession(session: RuntimeSession): SessionView {
  const runtime = (session.runtime || {}) as RuntimeSession['runtime'] & {
    started_at?: number;
    config_applied_at?: number | null;
    applied_revision?: string | null;
  };
  const rawConfig = runtime?.config as (NormalizedConfigState & {
    config_applied_at?: number | null;
    applied_revision?: string | null;
    last_restarted_at?: number | null;
  }) | undefined;
  const summary = runtime?.plugin_summary;
  const config: NormalizedConfigState = {
    status: rawConfig?.status || 'unknown',
    error: rawConfig?.error ?? null,
    drift: Boolean(rawConfig?.drift),
    revision: rawConfig?.revision,
    appliedAt: runtime?.config_applied_at ?? rawConfig?.config_applied_at ?? null,
    appliedRevision: runtime?.applied_revision ?? rawConfig?.applied_revision ?? null,
    lastRestartedAt: rawConfig?.last_restarted_at ?? null,
    hasSnapshot: Boolean(rawConfig),
  };
  return {
    raw: session,
    key: runtimeKey(session),
    platformId: session.platform_id,
    sessionId: session.session_id,
    active: Boolean(session.active),
    provider: session.provider_name || 'session_class',
    sessionType: session.session_type || session.session_type_name || '—',
    createdAt: session.created_at || 0,
    lastUsedAt: session.last_used_at || 0,
    messageCount: session.message_count || 0,
    sessionConfig: session.session_config || {},
    plugins: (runtime?.plugins || []) as RuntimePluginState[],
    pluginSummary: {
      total: summary?.total ?? 0,
      loaded: summary?.loaded ?? 0,
      errors: summary?.errors ?? 0,
      pending: summary?.pending ?? 0,
      drift: summary?.drift ?? 0,
      restart_required: summary?.restart_required ?? 0,
    },
    pluginRevision: runtime?.plugin_revision,
    config,
    startedAt: runtime?.started_at,
    lastError: (runtime as { last_error?: string } | undefined)?.last_error,
  };
}

export interface StateChip {
  tone: 'green' | 'orange' | 'red' | 'teal' | 'neutral';
  label: string;
  title?: string;
}

export function configState(session: SessionView): StateChip | null {
  if (session.provider !== 'edictum' || !session.active) return null;
  const config = session.config;
  if (!config.hasSnapshot) return { tone: 'neutral', label: '未知' };
  if (config.status === 'error') return { tone: 'red', label: '重启失败', title: config.error ?? undefined };
  if (config.drift || config.status === 'restart_pending') return { tone: 'orange', label: '等待热重启', title: config.error ?? undefined };
  if (config.status === 'restarting') return { tone: 'teal', label: '重启中' };
  return { tone: 'green', label: '已应用' };
}

export function pluginState(session: SessionView): StateChip {
  if (session.provider !== 'edictum') return { tone: 'neutral', label: '—' };
  if (!session.active) return { tone: 'neutral', label: '未加载' };
  const summary = session.pluginSummary;
  if (summary.total === 0) return { tone: 'neutral', label: '无插件' };
  const errors = session.plugins
    .filter((plugin) => plugin.status === 'error' || plugin.drift)
    .map((plugin) => `${plugin.name}: ${plugin.error || plugin.last_error || '目标状态与实际状态不一致'}`)
    .join('\n');
  if (summary.restart_required > 0) return { tone: 'orange', label: `需重启 ${summary.restart_required}`, title: errors };
  if (summary.drift > 0) return { tone: 'orange', label: `漂移 ${summary.drift}/${summary.total}`, title: errors };
  if (summary.errors > 0) return { tone: 'red', label: `失败 ${summary.errors}/${summary.total}`, title: errors };
  if (summary.pending > 0) return { tone: 'orange', label: `待加载 ${summary.pending}/${summary.total}`, title: errors };
  return { tone: 'green', label: `已加载 ${summary.loaded}/${summary.total}` };
}

export function sessionTone(session: SessionView): 'green' | 'orange' | 'red' | 'neutral' {
  if (!session.active) return 'neutral';
  const summary = session.pluginSummary;
  if (session.config.status === 'error' || summary.errors > 0) return 'red';
  if (session.config.drift || session.config.status === 'restart_pending' || summary.drift > 0
    || summary.restart_required > 0 || summary.pending > 0) return 'orange';
  return 'green';
}

export function canRetryRuntime(session: SessionView, backendRunning: boolean): boolean {
  if (!backendRunning || !session.active || session.provider !== 'edictum') return false;
  const config = session.config;
  const summary = session.pluginSummary;
  return Boolean(config.drift) || config.status === 'error' || summary.drift > 0
    || summary.errors > 0 || summary.restart_required > 0;
}

/* ------------------------------------------------------------------ 逐实例统计 */

export interface SessionUsageView {
  known: boolean;
  total: number | null;
  input: number | null;
  output: number | null;
  trend: number[];
  contextUsed: number | null;
  contextWindow: number | null;
  contextSource: 'reported' | 'estimated' | 'unknown';
}

export interface SessionMetricsView {
  loading: boolean;
  complete: boolean;
  sources: SourceCoverage[];
  warnings: string[];
  byKey: Map<string, SessionUsageView>;
}

export const UNKNOWN_USAGE: SessionUsageView = {
  known: false, total: null, input: null, output: null, trend: [],
  contextUsed: null, contextWindow: null, contextSource: 'unknown',
};

/** 近 14 日逐实例用量与上下文占用; 读取失败时保持未知, 不伪装成 0 */
export function useSessionMetrics(enabled: boolean): SessionMetricsView {
  const [view, setView] = useState<SessionMetricsView>({
    loading: false, complete: false, sources: [], warnings: [], byKey: new Map(),
  });
  const lifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!enabled) {
      setView({ loading: false, complete: false, sources: [], warnings: [], byKey: new Map() });
      return;
    }
    const controller = new AbortController();
    lifetime.current = controller;
    setView((current) => ({ ...current, loading: true }));
    statsApi.sessions({ bucket: 'day', limit: 100 }, controller.signal).then((response) => {
      if (controller.signal.aborted) return;
      const byKey = new Map<string, SessionUsageView>();
      for (const item of response.sessions || []) {
        const key = `${item.platform_id}\u0000${item.session_id}`;
        const total = item.usage?.total_tokens ?? null;
        const input = item.usage?.input_tokens ?? null;
        const output = item.usage?.output_tokens ?? null;
        const trend = (item.trend || []).map((bucket) => {
          const usage = bucket?.usage;
          if (!usage) return 0;
          return usage.total_tokens ?? ((usage.input_tokens || 0) + (usage.output_tokens || 0));
        });
        byKey.set(key, {
          known: total !== null || input !== null || output !== null,
          total,
          input,
          output,
          trend,
          contextUsed: item.context?.used ?? null,
          contextWindow: item.context?.window ?? null,
          contextSource: item.context?.source ?? 'unknown',
        });
      }
      setView({
        loading: false,
        complete: Boolean(response.complete),
        sources: response.sources || [],
        warnings: response.warnings || [],
        byKey,
      });
    }).catch(() => {
      if (!controller.signal.aborted) {
        setView({ loading: false, complete: false, sources: [], warnings: [], byKey: new Map() });
      }
    });
    return () => controller.abort();
  }, [enabled]);
  return view;
}

export function usageFor(metrics: SessionMetricsView, session: Pick<SessionView, 'platformId' | 'sessionId'>): SessionUsageView {
  return metrics.byKey.get(`${session.platformId}\u0000${session.sessionId}`) ?? UNKNOWN_USAGE;
}

export function coverageText(sources: SourceCoverage[]): string {
  if (sources.length === 0) return '统计来源未知';
  return sources.map((source) => `${source.service} ${sourceStateLabels[source.state]}`).join(' · ');
}

/* ------------------------------------------------------------------ 平台列表 */

export interface PlatformLite {
  id: string;
  type: string;
  enable?: boolean;
  session_provider?: string;
  session_type?: string;
}

/** 展开平台绑定信息 (session_type 可能是字符串或绑定对象) */
export function platformList(value: unknown): PlatformLite[] {
  const platforms = (value as { platforms?: unknown } | null)?.platforms;
  if (!Array.isArray(platforms)) return [];
  return platforms.filter((item): item is PlatformLite => (
    !!item && typeof item === 'object' && typeof (item as { id?: unknown }).id === 'string'
  ));
}

/* ------------------------------------------------------------------ 错误文案 */

export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') return error;
  return '未知错误';
}
