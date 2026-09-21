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

export interface ConfigReloadResult {
  platforms?: PlatformConfigApplication[];
  ok: boolean;
  edictum_sessions: EdictumPluginReloadSessionResult[];
}

export const backendApi = {
  wakePlatform: (payload: { adapter_id: string; group_id: string; user_id: string; request_id: string; prompt?: string; message_id?: string }) =>
    apiClient.post<{ status: 'accepted' | 'already_pending' | 'no_pending' | 'rejected'; reason?: string; state?: string }>('/api/platforms/wake', payload),
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
