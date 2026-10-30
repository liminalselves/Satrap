// 新前端后端契约 (docs/development/ui-backend-contract-2026-10-27.md 2.5 节) 的类型与客户端
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { serviceFetch } from './serviceFetch';

// ==================== 共用 ====================

export type Service = 'control' | 'backend' | 'chat';
export type Purpose = 'conversation' | 'summary' | 'approval' | 'other';
export type SourceState = 'available' | 'stale' | 'offline' | 'auth_required' | 'error';
export type Bucket = 'minute' | 'hour' | 'day';
export interface TimeWindow { from: number; to: number; tz: string; bucket: Bucket | null }
export interface SourceCoverage {
  service: Service; state: SourceState; as_of: number | null;
  collection_started_at: number | null; reason_code: string | null;
  complete: boolean; gaps: { from: number; to: number; reason_code: string }[];
  gaps_truncated: boolean; dropped_events: number;
}
export interface AggregateMeta {
  generated_at: number; window: TimeWindow; complete: boolean;
  sources: SourceCoverage[]; warnings: string[];
}
export interface Page<T> { items: T[]; has_more: boolean; next_cursor: string | null }
export interface PublicFailure { code: string; message: string }

export const serviceUrl = (service: Service): string =>
  service === 'control' ? getControlApiUrl() : service === 'chat' ? getChatApiUrl() : getApiBaseUrl();

export const sourceStateLabels: Record<SourceState, string> = {
  available: '可用', stale: '快照', offline: '离线', auth_required: '需要认证', error: '读取失败',
};

// ==================== 健康 ====================

export interface ServiceHealth {
  service: Service; running: boolean; healthy: boolean; runtime_id: string; pid: number;
  started_at: number; uptime_seconds: number; generated_at: number;
  python: string; platform: string; version: string | null; revision: string | null;
}
export interface ServiceProbe { target_service: 'backend'; measured_at: number; latency_ms: number | null; state: SourceState }
export interface HealthSample { health: ServiceHealth; rtt_ms: number }

const healthPath: Record<Service, string> = { control: '/api/health', backend: '/api/health', chat: '/api/chat/health' };

// ==================== 统计 ====================

export interface MessageCounts {
  inbound: number | null; outbound: number | null;
  model_requests: number | null; commands: number | null;
}
export interface UsageTotals {
  calls: number; succeeded: number; failed: number; cancelled: number;
  usage_reported_calls: number; usage_missing_calls: number;
  input_tokens: number | null; output_tokens: number | null;
  cached_tokens: number | null; total_tokens: number | null;
  token_reported_calls: { input_tokens: number; output_tokens: number; cached_tokens: number; total_tokens: number };
  duration_p50_ms: number | null; duration_p95_ms: number | null;
  duration_method: 'exact' | 'histogram';
}
export interface MetricBucket { from: number; to: number; messages: MessageCounts; usage: UsageTotals }
export interface Comparison {
  current: number | null; previous: number | null;
  absolute_delta: number | null; percent_delta: number | null;
}
export interface AdapterMetrics { platform_id: string; self_id: string; messages: MessageCounts; series: MetricBucket[] }
export interface Overview extends AggregateMeta {
  messages: MessageCounts; usage: UsageTotals; series: MetricBucket[]; by_adapter: AdapterMetrics[];
  comparison: { mode: 'yesterday_same_elapsed'; baseline_window: TimeWindow; messages: Comparison; tokens: Comparison };
}
export interface ModelMetrics {
  model_config: string | null; model: string; provider: string | null;
  usage: UsageTotals; trend: MetricBucket[];
}
export interface ContextOccupancy {
  used: number | null; window: number | null; source: 'reported' | 'estimated' | 'unknown';
  as_of: number | null; model_config: string | null;
}
export interface SessionMetrics {
  platform_id: string; agent: string | null; session_id: string;
  usage: UsageTotals; trend: MetricBucket[]; context: ContextOccupancy;
}
export type ModelsResponse = AggregateMeta & { models: ModelMetrics[]; has_more: boolean; next_cursor: string | null };
export type SessionsResponse = AggregateMeta & { sessions: SessionMetrics[]; has_more: boolean; next_cursor: string | null };

export interface StatsWindowQuery {
  from?: number; to?: number; tz?: string; bucket?: Bucket;
  purpose?: Purpose | 'all'; limit?: number; cursor?: string;
}

export interface LogLevelCounts { [level: string]: number }
export interface LogStats {
  service: string; stream_id: string; generated_at: number; coverage_from: number; complete: boolean;
  window_seconds: number; bucket_seconds: number; window_from: number; window_to: number;
  boundary_resolution_seconds: number; by_level: LogLevelCounts; error_count: number;
  lines_per_minute: number | null; rate_window_seconds: number;
  series: { from: number; to: number; by_level: LogLevelCounts }[];
}

export interface TelemetrySettings {
  usage_enabled: boolean; activity_enabled: boolean;
  usage_retention_days: number; activity_retention_days: number; aggregate_retention_days: number;
  max_service_mb: number;
}

// ==================== 活动 ====================

export interface ObjectIdentity {
  platform_id: string; self_id: string; conversation_kind: 'group' | 'private' | 'chat'; target_id: string;
}
export type ApprovalSource = 'group' | 'invitation' | 'friend' | 'memory';
export interface ActionKey { platform_id: string; self_id: string; source: ApprovalSource; action_id: string }
export type ActivityRef =
  | { kind: 'approval'; key: ActionKey }
  | { kind: 'rag_job'; service: Service; platform_id: string; session_id: string; kb_id: string; job_id: string }
  | { kind: 'plugin_application'; service: Service; operation_id: string }
  | { kind: 'platform'; platform_id: string; self_id: string }
  | { kind: 'session'; platform_id: string; session_id: string };
export interface ActivityEvent {
  event_id: string; occurred_at: number; service: Service; kind: string;
  tone: 'info' | 'warning' | 'error'; title: string;
  object: ObjectIdentity | null; request_id: string | null; ref: ActivityRef | null;
  count: number;
}
export type ActivityResponse = AggregateMeta & Page<ActivityEvent>;

// ==================== 审批 ====================

export type ActionState = 'pending' | 'ready' | 'executing' | 'succeeded' | 'failed' | 'unknown' | 'rejected' | 'expired';
export interface UnifiedAction {
  platform_id: string; self_id: string; source: ApprovalSource; action_id: string;
  key: ActionKey; revision: number; group_id: string; group_name: string | null;
  action_type: string; state: ActionState; source_state: string;
  created_at: number; expires_at: number | null;
  decided_at: number | null; decided_by: { kind: 'panel' | 'platform_user' | 'system'; id: string | null } | null;
  decision: 'approved' | 'rejected' | null; note: string | null;
  risk: string; risk_source: string;
  params: Record<string, unknown>;
  target: { user_id?: string; nickname?: string; remark?: string };
  result: { reason?: string; message?: string; retcode?: number; verification?: string } | null;
  actor_kind: string; actor_id: string;
  requester: { id: string; kind: string; name: string | null } | null;
  request: { action_type: string; params: Record<string, unknown> } | null;
  can_decide: boolean; unavailable_reason: string;
}
export interface ApprovalPlatform {
  platform_id: string; current_account: string; online: boolean; accounts: string[]; pending_count: number | null;
}
export interface ApprovalCatalog {
  generated_at: number; items: UnifiedAction[]; total: number; complete: boolean; warnings: string[];
  platforms: ApprovalPlatform[]; pending_count: number;
  by_state: Partial<Record<ActionState, number>>; by_source: Partial<Record<ApprovalSource, number>>;
  today_decided: number; automatic_count: number; expired_count: number; decision_latency_ms: number | null;
  window: { from: number | null; to: number | null; tz: string; bucket: null };
  has_more: boolean; next_cursor: string | null;
  sources?: SourceCoverage[];
}
export interface ApprovalQuery {
  platform_id?: string; account?: string; source?: ApprovalSource | 'all'; state?: ActionState | 'all';
  target?: string; action_id?: string; q?: string; sort?: 'created' | 'expires'; order?: 'asc' | 'desc';
  from?: number; to?: number; tz?: string; limit?: number; cursor?: string;
}
export interface DecisionInput { key: ActionKey; expected_revision: number; approve: boolean; note?: string }
export interface DecisionOutput {
  key: ActionKey; result: 'applied' | 'conflict' | 'unavailable' | 'failed' | 'unknown';
  action: { revision: number; state: ActionState; source_state: string; decision: 'approved' | 'rejected' | null } | null;
  error: PublicFailure | null;
}

export const approvalSourceLabels: Record<ApprovalSource, string> = {
  group: '群管理', invitation: '入群邀请', friend: '好友申请', memory: '记忆提案',
};
export const approvalStateLabels: Record<ActionState, string> = {
  pending: '待审批', ready: '准备执行', executing: '执行中', succeeded: '已生效', failed: '执行失败',
  unknown: '结果未知', rejected: '已拒绝', expired: '已过期',
};

// ==================== 知识库任务 ====================

export interface RagScope { platform_id: string; session_id?: string; kb_id: string }
export interface RagProgress { unit: 'documents' | 'chunks'; done: number; total: number | null; percent: number | null }
export interface RagJob {
  job_id: string; operation_id: string; owner_service: 'control' | 'chat'; owner_runtime_id: string | null;
  platform_id: string; session_id: string; kb_id: string; action: 'ingest' | 'rebuild';
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted';
  phase: 'queued' | 'parsing' | 'splitting' | 'embedding' | 'publishing' | 'finished';
  progress: RagProgress; created_at: number; started_at: number | null; finished_at: number | null;
  error: PublicFailure | null;
  result: { document_ids: string[]; indexed_documents: number; indexed_chunks: number; generation: string } | null;
}
export interface RagJobDocument {
  document_id: string; source_id: string | null; name: string; size_bytes: number | null;
  created_at: number | null; updated_at: number; last_indexed_at: number | null;
  status: 'queued' | 'indexing' | 'ready' | 'failed';
  error: PublicFailure | null; job_id: string | null; chunk_count: number;
}
export interface RagChunk { chunk_id?: string; index?: number; text?: string; [field: string]: unknown }
export interface RagJobSubmit {
  operation_id: string; expected_revision: number; action: 'ingest' | 'rebuild'; [field: string]: unknown;
}

export const ragPhaseLabels: Record<RagJob['phase'], string> = {
  queued: '排队中', parsing: '解析', splitting: '切分', embedding: '向量化', publishing: '发布', finished: '完成',
};

/** Chat 平台默认直连 Chat 服务, 不依赖 Control 在线 */
function ragTarget(scope: RagScope, via?: 'control'): { base: string; prefix: string; query: Record<string, string> } {
  const chat = scope.platform_id === 'chat' && via !== 'control';
  const query: Record<string, string> = { session_id: scope.session_id || '', kb_id: scope.kb_id };
  if (!chat) query.platform_id = scope.platform_id;
  return chat ? { base: getChatApiUrl(), prefix: '/api/chat/rag', query } : { base: getControlApiUrl(), prefix: '/config/rag', query };
}

// ==================== 历史 ====================

export interface StoredMessagePage<T = Record<string, unknown>> {
  items: T[]; has_more: boolean; next_cursor: string | null; as_of: number; consistency: 'live'; source: 'storage';
}
export interface StoredMessageQuery {
  layer: 'context' | 'history'; q?: string; role?: string; limit?: number; cursor?: string; order?: 'asc' | 'desc';
}
export interface ConversationAggregates { platforms: number; conversations: number; contexts: number; display_turns: number }

// ==================== 设置 ====================

export interface YamlIssue { path: string | null; line: number | null; column: number | null; code: string; message: string }
export interface YamlValidation { valid: boolean; errors: YamlIssue[] }
export interface StorageMaintenance { trash_retention_days: number; auto_purge_enabled: boolean; revision: string }

// ==================== 平台启停 / 插件健康 ====================

export interface PlatformToggleResult {
  saved: boolean; desired_enabled: boolean; revision: string; operation_id: string;
  ok?: boolean; runtime?: { status: 'applied' | 'pending' | 'unconfirmed' | 'failed'; reason_code: string | null };
  [field: string]: unknown;
}
export type PluginHealthReason = 'ok' | 'instance_error' | 'execution_off' | 'all_disabled' | 'not_used' | 'source_unavailable' | 'not_loaded';
export interface PluginHealth {
  state: 'healthy' | 'attention' | 'disabled' | 'unknown'; as_of: number | null;
  /** 判定依据; 旧后端不返回 */
  reason?: PluginHealthReason;
  coverage: { complete: boolean; expected_services: string[]; sources: { service: string; state: SourceState; as_of: number | null }[] };
  instances: Record<string, unknown>[];
}

// ==================== 客户端 ====================

const control = <T>(path: string, options?: Parameters<typeof serviceFetch>[2]) => serviceFetch<T>(getControlApiUrl(), path, options);

export const healthApi = {
  /** 读取服务健康并记录浏览器往返耗时 */
  sample: async (service: Service, signal?: AbortSignal): Promise<HealthSample> => {
    const started = performance.now();
    const health = await serviceFetch<ServiceHealth>(serviceUrl(service), healthPath[service], { signal, background: true });
    return { health, rtt_ms: performance.now() - started };
  },
  probe: (signal?: AbortSignal) => control<{ running: boolean; managed: boolean; probe?: ServiceProbe }>('/status', { signal, background: true }),
};

export const statsApi = {
  overview: (query: StatsWindowQuery & { platform_id?: string } = {}, signal?: AbortSignal) =>
    control<Overview>('/api/stats/overview', { query: { ...query }, signal, background: true }),
  models: (query: StatsWindowQuery & { model_config?: string } = {}, signal?: AbortSignal) =>
    control<ModelsResponse>('/api/stats/models', { query: { ...query }, signal, background: true }),
  sessions: (query: StatsWindowQuery & { platform_id?: string; agent?: string; session_id?: string } = {}, signal?: AbortSignal) =>
    control<SessionsResponse>('/api/stats/sessions', { query: { ...query }, signal, background: true }),
  logs: (service: Service, query: { window_seconds?: 120 | 600 | 1800; bucket_seconds?: 10 | 60 } = {}, signal?: AbortSignal) =>
    serviceFetch<LogStats>(serviceUrl(service), '/api/logs/stats', { query: { ...query }, signal, background: true }),
  telemetry: () => control<{ settings: TelemetrySettings; revision: string }>('/config/telemetry'),
  saveTelemetry: (settings: TelemetrySettings, expected_revision: string) =>
    control<{ settings: TelemetrySettings; revision: string }>('/config/telemetry', { method: 'PUT', body: { settings, expected_revision } }),
};

export const activityApi = {
  list: (query: { from?: number; to?: number; kind?: string; tone?: ActivityEvent['tone']; platform_id?: string; limit?: number; cursor?: string } = {}, signal?: AbortSignal) =>
    control<ActivityResponse>('/api/activity', { query: { ...query }, signal, background: true }),
};

export const approvalsApi = {
  list: (query: ApprovalQuery = {}, signal?: AbortSignal) =>
    control<ApprovalCatalog>('/api/platform-actions', { query: { ...query }, signal }),
  summary: (query: Pick<ApprovalQuery, 'platform_id' | 'account' | 'source' | 'from' | 'to' | 'tz'> = {}, signal?: AbortSignal) =>
    control<ApprovalCatalog>('/api/platform-actions/summary', { query: { ...query }, signal, background: true }),
  decide: (items: DecisionInput[]) => {
    if (items.length < 1 || items.length > 20) throw new Error('每批需提交 1 至 20 项审批');
    return control<{ items: DecisionOutput[] }>('/api/platform-actions/decisions', { method: 'POST', body: { items } });
  },
};

export const ragJobsApi = {
  jobs: (scope: RagScope, query: { operation_id?: string; limit?: number; cursor?: string } = {}, via?: 'control', signal?: AbortSignal) => {
    const target = ragTarget(scope, via);
    return serviceFetch<Page<RagJob>>(target.base, `${target.prefix}/jobs`, { query: { ...target.query, ...query }, signal });
  },
  job: (scope: RagScope, jobId: string, via?: 'control', signal?: AbortSignal) => {
    const target = ragTarget(scope, via);
    return serviceFetch<{ job: RagJob }>(target.base, `${target.prefix}/jobs/${encodeURIComponent(jobId)}`, { query: target.query, signal });
  },
  submit: (scope: RagScope, payload: RagJobSubmit, via?: 'control') => {
    const target = ragTarget(scope, via);
    return serviceFetch<{ job: RagJob }>(target.base, `${target.prefix}/jobs`, { method: 'POST', query: target.query, body: { kb_id: scope.kb_id, ...payload } });
  },
  upload: (scope: RagScope, file: File, params: { operation_id: string; expected_revision: number; source?: string }, via?: 'control') => {
    const target = ragTarget(scope, via);
    return serviceFetch<{ job: RagJob }>(target.base, `${target.prefix}/jobs/upload`, {
      method: 'POST', raw: file, query: { ...target.query, file_name: file.name, ...params },
    });
  },
  documents: (scope: RagScope, query: { limit?: number; cursor?: string } = {}, via?: 'control', signal?: AbortSignal) => {
    const target = ragTarget(scope, via);
    return serviceFetch<Page<RagJobDocument>>(target.base, `${target.prefix}/documents`, { query: { ...target.query, ...query }, signal });
  },
  chunks: (scope: RagScope, documentId: string, query: { include_text?: boolean; limit?: number; cursor?: string } = {}, via?: 'control', signal?: AbortSignal) => {
    const target = ragTarget(scope, via);
    return serviceFetch<Page<RagChunk>>(target.base, `${target.prefix}/documents/${encodeURIComponent(documentId)}/chunks`, { query: { ...target.query, ...query }, signal });
  },
};

export const historyApi = {
  messages: <T = Record<string, unknown>>(platformId: string, conversationId: string, query: StoredMessageQuery, signal?: AbortSignal) =>
    platformId === 'chat'
      ? serviceFetch<StoredMessagePage<T>>(getChatApiUrl(), `/api/chat/conversations/${encodeURIComponent(conversationId)}/messages`, { query: { ...query }, signal })
      : control<StoredMessagePage<T>>('/config/conversations/messages', { query: { platform_id: platformId, conversation_id: conversationId, ...query }, signal }),
  aggregates: (query: Record<string, string | number | undefined> = {}, signal?: AbortSignal) =>
    control<{ aggregates?: ConversationAggregates; aggregates_complete?: boolean; [field: string]: unknown }>('/config/conversations', { query: { ...query, include: 'aggregates' }, signal }),
};

export const settingsApi = {
  validateYaml: (yaml: string, signal?: AbortSignal) => control<YamlValidation>('/config/validate-yaml', { method: 'POST', body: { yaml }, signal }),
  maintenance: () => control<StorageMaintenance>('/config/storage-maintenance'),
  saveMaintenance: (value: { trash_retention_days: number; auto_purge_enabled: boolean }, expected_revision: string) =>
    control<StorageMaintenance>('/config/storage-maintenance', { method: 'PUT', body: { ...value, expected_revision } }),
};

export const platformToggleApi = {
  set: (platformId: string, enabled: boolean, expected_revision: string, operation_id?: string) =>
    control<PlatformToggleResult>(`/api/platforms/${encodeURIComponent(platformId)}/${enabled ? 'enable' : 'disable'}`, {
      method: 'POST', body: operation_id ? { expected_revision, operation_id } : { expected_revision },
    }),
};

export const pluginHealthApi = {
  list: <T extends { name: string }>(signal?: AbortSignal) =>
    control<{ plugins?: (T & { health: PluginHealth })[]; [field: string]: unknown }>('/config/plugins', { query: { include: 'health' }, signal }),
};
