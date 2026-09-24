import { apiClient } from './client';
import type { BackendHealth, PlatformConfigApplication } from './types';

export interface EdictumPluginReloadSessionResult {
  ok: boolean;
  platform_id: string;
  session_id: string;
  config_name?: string;
  action?: 'noop' | 'reconcile_plugins' | 'restart' | 'unload' | 'activate' | string;
  changed_fields?: string[];
  old_runtime_preserved?: boolean;
  restart_required?: boolean;
  drift?: number;
  revision?: number;
  applied_fingerprint?: string;
  desired_fingerprint?: string;
  plugins?: Array<{
    plugin: string;
    action?: string;
    status: 'applied' | 'rolled_back' | 'rollback_error' | 'restart_required' | 'error' | string;
    changes?: string[];
    error?: string;
    active?: boolean;
  }>;
  failed?: number;
  error?: string;
}

export interface EdictumPluginPreviewResult {
  ok: boolean;
  edictum_sessions: EdictumPluginReloadSessionResult[];
}

export interface WakeStatusResult {
  status: string;
  request_id: string;
  reason?: string;
  adapter_id?: string;
  target?: string;
  operator?: string;
  detail?: string;
  created_at?: number;
  updated_at?: number;
}

export interface WakeRejectionRecord {
  recorded_at: string;
  adapter_id: string;
  session_id: string;
  actor_id: string;
  stage: string;
  decision: string;
  reason: string;
  message_id: string;
  request_id: string;
  send_status: string;
}

/** 阶段诊断记录: 决策/限流/补全/模型/发送各阶段就地采集的脱敏事实 */
export interface RequestDiagnosticRecord {
  recorded_at: string;
  adapter_id: string;
  session_id: string;
  actor_id: string;
  stage: string;
  decision: string;
  reason: string;
  message_id: string;
  request_id: string;
  send_status: string;
  self_id: string;
  status: string;
  reason_code: string;
  turn_id: string;
  attachments: string;
  notes: string;
}

/** 按请求汇总的阶段摘要, 最新在前 */
export interface RequestDiagnosticSummary {
  request_id: string;
  adapter_id: string;
  session_id: string;
  actor_id: string;
  self_id: string;
  message_id: string;
  recorded_at: string;
  stages: string[];
  statuses: Record<string, string>;
  reason_codes: string[];
  attachments: string;
  notes: string;
  send_status: string;
  turn_id: string;
}

export interface RequestDiagnosticsResult {
  records: RequestDiagnosticSummary[];
  available: boolean;
  reason?: string;
  capacity?: number;
  records_per_request?: number;
  requests_total?: number;
  records_total?: number;
}

export interface RequestDiagnosticDetail {
  status?: string;
  request_id: string;
  adapter_id?: string;
  records?: RequestDiagnosticRecord[];
  stages?: string[];
  available?: boolean;
  truncated?: boolean;
  reason?: 'not_found' | 'scheduler_unavailable' | string;
}

export interface ConfigReloadResult {
  platforms?: PlatformConfigApplication[];
  ok: boolean;
  edictum_sessions: EdictumPluginReloadSessionResult[];
}

export const backendApi = {
  wakePlatform: (payload: { adapter_id: string; group_id: string; user_id: string; request_id: string; prompt?: string; message_id?: string }) =>
    apiClient.post<{ status: 'accepted' | 'already_pending' | 'no_pending' | 'rejected'; reason?: string; state?: string }>('/api/platforms/wake', payload),

  // 手动唤醒请求状态查询 (批次三端点, 重启后可查)
  getWakeStatus: (requestId: string, adapterId?: string) =>
    apiClient.get<WakeStatusResult>(`/api/platforms/wake/${encodeURIComponent(requestId)}`, adapterId ? { adapter_id: adapterId } : undefined),

  // 唤醒决策/限流拒绝记录, 最新在前
  listWakeRejections: (adapterId?: string, limit = 20) =>
    apiClient.get<{ records: WakeRejectionRecord[] }>('/api/platforms/wake/rejections', {
      ...(adapterId ? { adapter_id: adapterId } : {}), limit,
    }),

  // 按请求关联的近期阶段诊断 (决策/限流/补全/模型/发送), 最新在前
  listRequestDiagnostics: (params: { adapterId?: string; stage?: string; requestId?: string; limit?: number } = {}) =>
    apiClient.get<RequestDiagnosticsResult>('/api/platforms/wake/diagnostics', {
      ...(params.adapterId ? { adapter_id: params.adapterId } : {}),
      ...(params.stage ? { stage: params.stage } : {}),
      ...(params.requestId ? { request_id: params.requestId } : {}),
      limit: params.limit ?? 20,
    }),

  // 单个请求的完整阶段明细
  getRequestDiagnostic: (requestId: string, adapterId?: string) =>
    apiClient.get<RequestDiagnosticDetail>(`/api/platforms/wake/diagnostics/${encodeURIComponent(requestId)}`, adapterId ? { adapter_id: adapterId } : undefined),
  // 获取后端健康状态
  health: () => apiClient.get<BackendHealth>('/api/health'),

  // 重载配置
  reloadConfig: (revision?: string) => apiClient.post<ConfigReloadResult>('/api/config/reload', revision ? { expected_config_revision: revision } : undefined),

  // 预览 Edictum 插件变更对活跃会话的影响
  previewEdictumPlugins: (data: {
    config_name?: string;
    plugins?: unknown[];
    session_refs?: Array<{ platform_id: string; session_id: string }>;
  }) => apiClient.post<EdictumPluginPreviewResult>('/api/edictum/plugins/preview', data),

  // 协调或重试 Edictum 活跃会话的插件状态
  reconcileEdictumPlugins: (data: {
    config_name?: string;
    session_refs?: Array<{ platform_id: string; session_id: string }>;
    concurrency?: number;
  }) => apiClient.post<ConfigReloadResult>('/api/edictum/plugins/reconcile', data),

  // 预览 Edictum 完整配置变更
  previewEdictumRuntime: (data: {
    config_name?: string;
    config?: Record<string, unknown>;
    session_refs?: Array<{ platform_id: string; session_id: string }>;
  }) => apiClient.post<EdictumPluginPreviewResult>('/api/edictum/runtime/preview', data),

  // 应用 Edictum 完整配置变更
  reconcileEdictumRuntime: (data: {
    config_name?: string;
    session_refs?: Array<{ platform_id: string; session_id: string }>;
    concurrency?: number;
  }) => apiClient.post<ConfigReloadResult>('/api/edictum/runtime/apply', data),

  // 关闭后端
  shutdown: () => apiClient.post<{ ok: boolean }>('/api/shutdown'),
};
