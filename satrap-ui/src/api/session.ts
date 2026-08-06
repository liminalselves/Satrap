import { apiClient } from './client';
import type { SessionClassConfig } from './types';

export const sessionApi = {
  // 列出会话类配置
  list: () => apiClient.get<Record<string, SessionClassConfig>>('/api/config/session-classes'),

  // 获取单个会话类配置
  get: (name: string) => apiClient.get<SessionClassConfig>(`/api/config/session-classes/${name}`),

  // 注册会话类
  register: (data: {
    name: string;
    class_path: string;
    description?: string;
    context_key?: string;
    model_key?: string;
  }) => apiClient.post<{ ok: boolean }>('/api/config/session-classes', data),

  // 启用会话类
  enable: (name: string) =>
    apiClient.post<{ ok: boolean }>(`/api/config/session-classes/${name}/enable`),

  // 禁用会话类
  disable: (name: string) =>
    apiClient.post<{ ok: boolean }>(`/api/config/session-classes/${name}/disable`),

  // 更新会话类参数
  updateParams: (name: string, params: Record<string, unknown>) =>
    apiClient.put<{ ok: boolean }>(`/api/config/session-classes/${name}`, { params }),

  // 注销会话类
  unregister: (name: string) =>
    apiClient.delete<{ ok: boolean }>(`/api/config/session-classes/${name}`),
};
