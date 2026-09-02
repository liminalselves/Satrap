import axios from 'axios';
import { getControlApiUrl } from '@/utils/constants';
import { establishApiSession } from '@/api/auth';
import type {
  DiscoveredSessionClass,
  EdictumSessionConfig,
  EdictumAvailablePlugin,
  EdictumTypeDefinition,
  EmbeddingConfig,
  LLMConfig,
  PlatformConfig,
  ReRankConfig,
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
  platforms?: PlatformConfig[];
  exists?: boolean;
}

type ModelType = 'llm' | 'embedding' | 'rerank';
type ModelConfig = LLMConfig | EmbeddingConfig | ReRankConfig;

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

  // 读取平台配置
  listPlatforms: async (): Promise<PlatformConfigResult> => {
    const response = await controlClient.get<PlatformConfigResult>('/config/platforms');
    return response.data;
  },

  // 创建平台配置
  createPlatform: async (platform: PlatformConfig): Promise<PlatformConfigResult> => {
    const response = await controlClient.post<PlatformConfigResult>('/config/platforms', platform);
    return response.data;
  },

  // 更新平台配置
  updatePlatform: async (originalId: string, platform: PlatformConfig): Promise<PlatformConfigResult> => {
    const response = await controlClient.put<PlatformConfigResult>(
      `/config/platforms/${encodeURIComponent(originalId)}`,
      platform,
    );
    return response.data;
  },

  // 删除平台配置
  deletePlatform: async (platformId: string): Promise<PlatformConfigResult> => {
    const response = await controlClient.delete<PlatformConfigResult>(
      `/config/platforms/${encodeURIComponent(platformId)}`,
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
