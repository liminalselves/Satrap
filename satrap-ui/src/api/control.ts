import axios from 'axios';
import type { SessionPluginSettings } from './pluginSettings';
import type { ModelOptions } from '@/components/common/PluginConfigFields';
import type { RagResult } from './rag';
import { getControlApiUrl } from '@/utils/constants';
import { establishApiSession } from '@/api/auth';
import type {
  DiscoveredSessionClass,
  EdictumSessionConfig,
  EdictumAvailablePlugin,
  EdictumTypeDefinition,
  ModelConfig,
  ModelType,
  PlatformConfig,
  RuntimeSession,
  SessionClassConfig,
} from './types';
import type { StorageAuditResult } from './storage';
import type {
  ChatHistoryDeleteRequest,
  ChatHistoryDeleteResult,
  ChatHistoryQuery,
  ChatHistoryResult,
  ChatHistoryTrashResult,
} from './chat';

// 唤醒策略试算 (POST /config/wake-dry-run)
export interface WakeDryRunStep {
  text?: string;
  actor?: string;
  advance_seconds?: number;
  at_self?: boolean;
  submit?: boolean;
}

export interface WakeDryRunRequest {
  settings: Record<string, unknown>;
  group_id?: string;
  local_time?: string;
  steps?: WakeDryRunStep[];
  probe?: { text: string; at_self?: boolean; quote_self?: boolean };
}

export interface WakeDryRunDecision {
  triggered: boolean | null;
  rule: string;
  reason: string;
  matched?: string;
  score?: number | null;
}

export interface WakeDryRunResult {
  ok: boolean;
  error?: string;
  resolved?: Record<string, unknown>;
  explicit?: WakeDryRunDecision;
  automatic?: {
    mode: string;
    observed: number;
    steps: Array<{ index: number; kind: string; observed?: number; claimed?: number; decision?: WakeDryRunDecision }>;
    decision: WakeDryRunDecision;
    deadline_decision: WakeDryRunDecision;
    cooldown_remaining: number;
  };
}

// 后端控制 API 客户端(独立于主后端)
const controlClient = axios.create({
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

controlClient.interceptors.request.use((config) => {
  config.baseURL = getControlApiUrl();
  return config;
});

controlClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    const config = error.config as typeof error.config & { _satrapAuthRetry?: boolean };
    if (error.response?.status === 401 && config && !config._satrapAuthRetry) {
      config._satrapAuthRetry = true;
      await establishApiSession(getControlApiUrl());
      return controlClient.request(config);
    }
    return Promise.reject(error);
  },
);

export interface BackendStatus {
  running: boolean;
  managed: boolean;
  health?: {
    running: boolean;
    adapters?: Record<string, unknown>;
    sessions?: number;
    users?: number;
  };
}

export interface ControlResult {
  ok: boolean;
  message?: string;
  error?: string;
}

export interface ConfigResult {
  ok: boolean;
  config?: Record<string, unknown>;
  path?: string;
  exists?: boolean;
  message?: string;
  error?: string;
}

export interface PlatformConfigResult extends ControlResult {
  revision?: string;
  platforms?: PlatformConfig[];
  exists?: boolean;
}


export interface AsrTestResult extends ControlResult {
  text?: string;
  model?: string;
  language?: string;
  duration?: number;
  elapsed_ms?: number;
  converted_from?: string;
}

export interface SessionClassConfigPayload {
  name: string;
  class_path: string;
  is_async?: boolean;
  enabled?: boolean;
  context_key?: string;
  model_key?: string;
  description?: string;
  params?: Record<string, unknown>;
}

export interface EdictumConfigPayload {
  name: string;
  edictum_type: string;
  enabled?: boolean;
  description?: string;
  model_name?: string;
  params?: Record<string, unknown>;
  plugins?: EdictumSessionConfig['plugins'];
}

export interface EdictumConfigResult extends ControlResult {
  name?: string;
  config?: EdictumSessionConfig;
}

export function parseModelConfigs(data: unknown): Record<string, ModelConfig> {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new TypeError('模型配置接口返回了无效数据');
  }

  for (const config of Object.values(data)) {
    if (typeof config !== 'object' || config === null || Array.isArray(config)) {
      throw new TypeError('模型配置接口返回了无效配置项');
    }
  }

  return data as Record<string, ModelConfig>;
}

export function parseSessionClassConfigs(data: unknown): Record<string, SessionClassConfig> {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new TypeError('会话类配置接口返回了无效数据');
  }

  for (const config of Object.values(data)) {
    if (typeof config !== 'object' || config === null || Array.isArray(config)) {
      throw new TypeError('会话类配置接口返回了无效配置项');
    }
  }

  return data as Record<string, SessionClassConfig>;
}

export function parseEdictumTypes(data: unknown): EdictumTypeDefinition[] {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new TypeError('Edictum 类型接口返回了无效数据');
  }
  const types = (data as { types?: unknown }).types;
  if (!Array.isArray(types)) {
    throw new TypeError('Edictum 类型接口缺少 types 数组');
  }
  for (const item of types) {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) {
      throw new TypeError('Edictum 类型接口返回了无效类型项');
    }
  }
  return types as EdictumTypeDefinition[];
}

export function parseEdictumConfigs(data: unknown): Record<string, EdictumSessionConfig> {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new TypeError('Edictum 配置接口返回了无效数据');
  }
  for (const config of Object.values(data)) {
    if (typeof config !== 'object' || config === null || Array.isArray(config)) {
      throw new TypeError('Edictum 配置接口返回了无效配置项');
    }
  }
  return data as Record<string, EdictumSessionConfig>;
}

export const controlApi = {
  refreshChatHistoryStorage: async () => (await controlClient.post<{ storage_size_bytes: number | null; storage_size_updated_at: number | null }>('/chat/history/storage', {}, { timeout: 300000 })).data,
  ragList: async (platformId: string, sessionId: string, kbId: string) => (await controlClient.get<RagResult>('/config/rag', { params: { platform_id: platformId, session_id: sessionId, kb_id: kbId } })).data,
  ragAction: async (platformId: string, sessionId: string, payload: Record<string, unknown>) => (await controlClient.post<Record<string, unknown>>('/config/rag', payload, { params: { platform_id: platformId, session_id: sessionId }, timeout: 300000 })).data,
  pluginModelOptions: async () => (await controlClient.get<{ options: ModelOptions }>('/config/plugin-model-options')).data,
  getSessionPluginConfig: async (platformId: string, sessionId: string, plugin: string) => (await controlClient.get<SessionPluginSettings>('/config/session-plugin-config', { params: { platform_id: platformId, session_id: sessionId, plugin } })).data,
  saveSessionPluginConfig: async (platformId: string, sessionId: string, plugin: string, overrides: Record<string, unknown>, revision: number) => (await controlClient.put<SessionPluginSettings>('/config/session-plugin-config', { overrides, expected_revision: revision }, { params: { platform_id: platformId, session_id: sessionId, plugin } })).data,
  // 获取后端状态
  status: async (): Promise<BackendStatus> => {
    const response = await controlClient.get<BackendStatus>('/status');
    return response.data;
  },

  // 启动后端
  start: async (): Promise<ControlResult> => {
    const response = await controlClient.post<ControlResult>('/start');
    return response.data;
  },

  // 停止后端
  stop: async (): Promise<ControlResult> => {
    const response = await controlClient.post<ControlResult>('/stop');
    return response.data;
  },

  // 重启后端
  restart: async (): Promise<ControlResult> => {
    const response = await controlClient.post<ControlResult>('/restart');
    return response.data;
  },

  // 读取配置文件
  getConfig: async (): Promise<ConfigResult> => {
    const response = await controlClient.get<ConfigResult>('/config');
    return response.data;
  },

  // 保存配置文件
  saveConfig: async (config: Record<string, unknown>): Promise<ConfigResult> => {
    const response = await controlClient.put<ConfigResult>('/config', config);
    return response.data;
  },

  // 创建默认配置文件
  createDefaultConfig: async (): Promise<ConfigResult> => {
    const response = await controlClient.post<ConfigResult>('/config/default');
    return response.data;
  },

  // 校验配置文件
  validateConfig: async (config: Record<string, unknown>): Promise<ConfigResult> => {
    const response = await controlClient.post<ConfigResult>('/config/validate', config);
    return response.data;
  },

  // 唤醒策略试算: 隔离窗口 + 当前草稿, 不触碰线上状态
  dryRunWake: async (payload: WakeDryRunRequest): Promise<WakeDryRunResult> => {
    const response = await controlClient.post<WakeDryRunResult>('/config/wake-dry-run', payload);
    return response.data;
  },

  // 读取平台配置
  listPlatforms: async (): Promise<PlatformConfigResult> => {
    const response = await controlClient.get<PlatformConfigResult>('/config/platforms');
    return response.data;
  },

  // 创建平台配置
  createPlatform: async (platform: PlatformConfig, revision: string): Promise<PlatformConfigResult> => {
    const response = await controlClient.post<PlatformConfigResult>('/config/platforms', platform, { params: { expected_revision: revision } });
    return response.data;
  },

  // 更新平台配置
  updatePlatform: async (originalId: string, platform: PlatformConfig, revision: string): Promise<PlatformConfigResult> => {
    const response = await controlClient.put<PlatformConfigResult>(
      `/config/platforms/${encodeURIComponent(originalId)}`,
      platform,
      { params: { expected_revision: revision } },
    );
    return response.data;
  },

  // 删除平台配置
  deletePlatform: async (platformId: string, revision: string): Promise<PlatformConfigResult> => {
    const response = await controlClient.delete<PlatformConfigResult>(
      `/config/platforms/${encodeURIComponent(platformId)}`,
      { params: { expected_revision: revision } },
    );
    return response.data;
  },

  listModels: async (type: ModelType): Promise<Record<string, ModelConfig>> => {
    const response = await controlClient.get<unknown>('/config/models', {
      params: { type },
    });
    return parseModelConfigs(response.data);
  },

  createModel: async (type: ModelType, name: string, config: Partial<ModelConfig>): Promise<ControlResult> => {
    const response = await controlClient.post<ControlResult>(
      `/config/models/${type}/${encodeURIComponent(name)}`,
      config,
    );
    return response.data;
  },

  updateModel: async (type: ModelType, name: string, config: Partial<ModelConfig>): Promise<ControlResult> => {
    const response = await controlClient.patch<ControlResult>(
      `/config/models/${type}/${encodeURIComponent(name)}`,
      config,
    );
    return response.data;
  },

  deleteModel: async (type: ModelType, name: string): Promise<ControlResult> => {
    const response = await controlClient.delete<ControlResult>(
      `/config/models/${type}/${encodeURIComponent(name)}`,
    );
    return response.data;
  },

  testAsrConfig: async (name: string, filename: string, audioBase64: string): Promise<AsrTestResult> => {
    try {
      const response = await controlClient.post<AsrTestResult>(
        `/config/models/asr/${encodeURIComponent(name)}/test`,
        { filename, audio_base64: audioBase64 },
        { timeout: 120000 },
      );
      return response.data;
    } catch (error) {
      // 服务端以 4xx/5xx 返回结构化错误时保留其 error 文案, 便于页面直接呈现拒绝原因
      const status = axios.isAxiosError(error) ? error.response?.status : undefined;
      if (status === 401 || status === 403) throw error;
      if (axios.isAxiosError(error) && error.response?.data && typeof error.response.data === 'object') {
        const data = error.response.data as Partial<AsrTestResult>;
        return { ok: false, error: data.error || error.message };
      }
      throw error;
    }
  },

  listSessionClasses: async (): Promise<Record<string, SessionClassConfig>> => {
    const response = await controlClient.get<unknown>('/config/session-classes');
    return parseSessionClassConfigs(response.data);
  },

  getSessionClass: async (name: string): Promise<SessionClassConfig> => {
    const response = await controlClient.get<SessionClassConfig>(
      `/config/session-classes/${encodeURIComponent(name)}`,
    );
    return response.data;
  },

  createSessionClass: async (config: SessionClassConfigPayload): Promise<ControlResult> => {
    const response = await controlClient.post<ControlResult>('/config/session-classes', config);
    return response.data;
  },

  updateSessionClass: async (
    name: string,
    config: Partial<SessionClassConfigPayload>,
  ): Promise<ControlResult> => {
    const response = await controlClient.patch<ControlResult>(
      `/config/session-classes/${encodeURIComponent(name)}`,
      config,
    );
    return response.data;
  },

  setSessionClassEnabled: async (name: string, enabled: boolean): Promise<ControlResult> => {
    const action = enabled ? 'enable' : 'disable';
    const response = await controlClient.post<ControlResult>(
      `/config/session-classes/${encodeURIComponent(name)}/${action}`,
    );
    return response.data;
  },

  deleteSessionClass: async (name: string): Promise<ControlResult> => {
    const response = await controlClient.delete<ControlResult>(
      `/config/session-classes/${encodeURIComponent(name)}`,
    );
    return response.data;
  },

  discoverSessionClasses: async (path?: string): Promise<{
    paths: string[];
    results: DiscoveredSessionClass[];
  }> => {
    const query = path ? `?path=${encodeURIComponent(path)}` : '';
    const response = await controlClient.get<{
      paths: string[];
      results: DiscoveredSessionClass[];
    }>(`/config/session/discovery${query}`);
    return response.data;
  },

  createSessionScanDirectory: async (path?: string): Promise<{ ok: boolean; path: string }> => {
    const response = await controlClient.post<{ ok: boolean; path: string }>(
      '/config/session/discovery/directories',
      { path },
    );
    return response.data;
  },

  queryChatHistory: async (query: ChatHistoryQuery = {}): Promise<ChatHistoryResult> => {
    const response = await controlClient.get<ChatHistoryResult>('/chat/history', { params: query });
    return response.data;
  },

  deleteChatHistory: async (data: ChatHistoryDeleteRequest): Promise<ChatHistoryDeleteResult> => {
    const response = await controlClient.post<ChatHistoryDeleteResult>('/chat/history/delete', data);
    return response.data;
  },

  listChatHistoryTrash: async (): Promise<ChatHistoryTrashResult> => {
    const response = await controlClient.get<ChatHistoryTrashResult>('/chat/history/trash');
    return response.data;
  },

  restoreChatHistory: async (archiveId: string): Promise<{ ok: boolean; session_id: string }> => {
    const response = await controlClient.post<{ ok: boolean; session_id: string }>(
      '/chat/history/trash/restore',
      { archive_id: archiveId },
    );
    return response.data;
  },

  purgeChatHistory: async (archiveId: string): Promise<{ ok: boolean }> => {
    const response = await controlClient.post<{ ok: boolean }>(
      '/chat/history/trash/purge',
      { archive_id: archiveId },
    );
    return response.data;
  },

  listEdictumTypes: async (): Promise<EdictumTypeDefinition[]> => {
    const response = await controlClient.get<unknown>('/config/edictum/types');
    return parseEdictumTypes(response.data);
  },

  listEdictumPlugins: async (): Promise<EdictumAvailablePlugin[]> => {
    const response = await controlClient.get<{ plugins: EdictumAvailablePlugin[] }>(
      '/config/edictum/plugins',
    );
    return response.data.plugins;
  },

  listEdictumSessions: async (): Promise<Record<string, EdictumSessionConfig>> => {
    const response = await controlClient.get<unknown>('/config/edictum/sessions');
    return parseEdictumConfigs(response.data);
  },

  getEdictumSession: async (name: string): Promise<EdictumSessionConfig> => {
    const response = await controlClient.get<EdictumSessionConfig>(
      `/config/edictum/sessions/${encodeURIComponent(name)}`,
    );
    return response.data;
  },

  createEdictumSession: async (config: EdictumConfigPayload): Promise<EdictumConfigResult> => {
    const response = await controlClient.post<EdictumConfigResult>(
      '/config/edictum/sessions',
      config,
    );
    return response.data;
  },

  updateEdictumSession: async (
    name: string,
    config: Partial<EdictumConfigPayload>,
  ): Promise<EdictumConfigResult> => {
    const response = await controlClient.patch<EdictumConfigResult>(
      `/config/edictum/sessions/${encodeURIComponent(name)}`,
      config,
    );
    return response.data;
  },

  setEdictumSessionEnabled: async (
    name: string,
    enabled: boolean,
  ): Promise<EdictumConfigResult> => {
    const action = enabled ? 'enable' : 'disable';
    const response = await controlClient.post<EdictumConfigResult>(
      `/config/edictum/sessions/${encodeURIComponent(name)}/${action}`,
    );
    return response.data;
  },

  deleteEdictumSession: async (name: string): Promise<ControlResult> => {
    const response = await controlClient.delete<ControlResult>(
      `/config/edictum/sessions/${encodeURIComponent(name)}`,
    );
    return response.data;
  },

  listSessionInstances: async (): Promise<{ sessions: RuntimeSession[] }> => {
    const response = await controlClient.get<{ sessions: RuntimeSession[] }>(
      '/config/session-instances',
    );
    return response.data;
  },

  createSessionInstance: async (data: {
    class_name?: string;
    session_provider?: string;
    session_type?: string;
    session_id?: string;
    platform_id?: string;
    adapter_id?: string;
    llm_name?: string;
    params?: Record<string, unknown>;
  }): Promise<{ ok: boolean; session: RuntimeSession }> => {
    const response = await controlClient.post<{ ok: boolean; session: RuntimeSession }>(
      '/config/session-instances',
      data,
    );
    return response.data;
  },

  deleteSessionInstance: async (platformId: string, sessionId: string): Promise<{
    ok: boolean;
    deleted_count: number;
    deleted_ids: string[];
  }> => {
    const response = await controlClient.delete<{
      ok: boolean;
      deleted_count: number;
      deleted_ids: string[];
    }>(`/config/session-instances/${encodeURIComponent(sessionId)}?platform_id=${encodeURIComponent(platformId)}`);
    return response.data;
  },

  bulkDeleteSessionInstances: async (data: {
    mode: 'empty' | 'single' | 'selected';
    session_refs?: Array<{ platform_id: string; session_id: string }>;
  }): Promise<{
    ok: boolean;
    deleted_count: number;
    deleted_ids: string[];
  }> => {
    const response = await controlClient.post<{
      ok: boolean;
      deleted_count: number;
      deleted_ids: string[];
    }>('/config/session-instances/bulk-delete', data);
    return response.data;
  },

  auditStorage: async (): Promise<StorageAuditResult> => {
    const response = await controlClient.get<StorageAuditResult>('/storage/audit');
    return response.data;
  },

  cleanupStorage: async (itemIds: string[]): Promise<{
    ok: boolean;
    results: Array<{ item_id: string; ok: boolean; error?: string }>;
  }> => {
    const response = await controlClient.post<{
      ok: boolean;
      results: Array<{ item_id: string; ok: boolean; error?: string }>;
    }>('/storage/cleanup', { item_ids: itemIds });
    return response.data;
  },

  restoreStorageArchive: async (platformId: string, archiveId: string): Promise<{
    ok: boolean;
    platform_id: string;
    session_id: string;
    archive_id: string;
  }> => {
    const response = await controlClient.post<{
      ok: boolean;
      platform_id: string;
      session_id: string;
      archive_id: string;
    }>('/storage/trash/restore', {
      platform_id: platformId,
      archive_id: archiveId,
    });
    return response.data;
  },

  purgeStorageArchive: async (platformId: string, archiveId: string): Promise<{ ok: boolean }> => {
    const response = await controlClient.post<{ ok: boolean }>('/storage/trash/purge', {
      platform_id: platformId,
      archive_id: archiveId,
    });
    return response.data;
  },

  purgeStorageArchives: async (data: {
    archive_refs?: Array<{ platform_id: string; archive_id: string }>;
    older_than_days?: number;
    platform_id?: string;
  }): Promise<{
    ok: boolean;
    results: Array<{ platform_id: string; archive_id: string; ok: boolean; error?: string }>;
  }> => {
    const response = await controlClient.post<{
      ok: boolean;
      results: Array<{ platform_id: string; archive_id: string; ok: boolean; error?: string }>;
    }>('/storage/trash/purge-batch', data);
    return response.data;
  },
};
