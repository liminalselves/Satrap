import { apiClient } from './client';
import type { UserInfo } from './types';

export const userApi = {
  // 列出用户
  list: (limit = 200) => apiClient.get<{ users: UserInfo[]; count: number }>(`/api/users?limit=${limit}`),

  // 获取用户详情
  get: (userId: string) => apiClient.get<{ ok: boolean; user?: UserInfo; error?: string }>(`/api/users?user_id=${userId}`),

  // 创建用户
  create: (userId: string, platform?: string, nickname?: string) =>
    apiClient.post<{ ok: boolean; user: UserInfo; created: boolean }>('/api/user/create', {
      user_id: userId,
      platform,
      nickname,
    }),

  // 更新用户
  update: (userId: string, data: { nickname?: string; platform?: string }) =>
    apiClient.post<{ ok: boolean; user: UserInfo }>('/api/user/update', {
      user_id: userId,
      ...data,
    }),

  // 删除用户
  delete: (userId: string) =>
    apiClient.post<{ ok: boolean }>('/api/user/delete', { user_id: userId }),

  // 绑定会话
  bindSession: (userId: string, sessionId: string) =>
    apiClient.post<{ ok: boolean; session_ids: string[] }>('/api/user/bind', {
      user_id: userId,
      session_id: sessionId,
    }),

  // 解绑会话
  unbindSession: (userId: string, sessionId: string) =>
    apiClient.post<{ ok: boolean; session_ids: string[] }>('/api/user/unbind', {
      user_id: userId,
      session_id: sessionId,
    }),

  // 获取用户会话列表
  getSessions: (userId: string) =>
    apiClient.get<{ user_id: string; session_ids: string[]; count: number }>(
      `/api/user/sessions?user_id=${userId}`
    ),
};
