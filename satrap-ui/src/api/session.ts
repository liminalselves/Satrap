import { apiClient } from './client';
import type { DiscoveredSessionClass, RuntimeSession, SessionClassConfig } from './types';

export const sessionApi = {
  // 列出会话类配置
  list: () => apiClient.get<Record<string, SessionClassConfig>>('/api/config/session-classes'),

  // 获取单个会话类配置
  get: (name: string) => apiClient.get<SessionClassConfig>(`/api/config/session-classes/${encodeURIComponent(name)}`),

  // 注册会话类
  register: (data: {
    name: string;
    class_path: string;
    description?: string;
    context_key?: string;
    model_key?: string;
    params?: Record<string, unknown>;
  }) => apiClient.post<{ ok: boolean }>('/api/config/session-classes', data),

  // 启用会话类
  enable: (name: string) =>
    apiClient.post<{ ok: boolean }>(`/api/config/session-classes/${encodeURIComponent(name)}/enable`),

  // 禁用会话类
  disable: (name: string) =>
    apiClient.post<{ ok: boolean }>(`/api/config/session-classes/${encodeURIComponent(name)}/disable`),

  // 更新会话类参数
  updateParams: (name: string, params: Record<string, unknown>) =>
    apiClient.put<{ ok: boolean }>(`/api/config/session-classes/${encodeURIComponent(name)}`, { params }),

  // 更新会话类配置
  update: (name: string, data: {
    params?: Record<string, unknown>;
    description?: string;
    context_key?: string;
    model_key?: string;
  }) => apiClient.put<{ ok: boolean; config: SessionClassConfig }>(
    `/api/config/session-classes/${encodeURIComponent(name)}`,
    data,
  ),

  // 注销会话类
  unregister: (name: string) =>
    apiClient.delete<{ ok: boolean }>(`/api/config/session-classes/${encodeURIComponent(name)}`),

  // 扫描会话类
  discover: (path?: string) => apiClient.get<{
    paths: string[];
    results: DiscoveredSessionClass[];
  }>(`/api/session/discovery${path ? `?path=${encodeURIComponent(path)}` : ''}`),

  // 创建配置中的扫描目录
  createScanDirectory: (path?: string) => apiClient.post<{ ok: boolean; path: string }>(
    '/api/session/discovery/directories',
    { path },
  ),

  // 列出活跃会话
  listRuntime: () => apiClient.get<{ sessions: RuntimeSession[] }>('/api/sessions'),

  // 创建运行时会话配置
  createRuntime: (data: {
    class_name: string;
    session_id?: string;
    adapter_id?: string;
    llm_name?: string;
    params?: Record<string, unknown>;
  }) => apiClient.post<{ ok: boolean; session: RuntimeSession }>('/api/sessions', data),
};
