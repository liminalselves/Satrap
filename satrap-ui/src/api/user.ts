import { apiClient } from './client';
import type { UserInfo } from './types';

export const userApi = {
  // 列出用户
  list: (platformId: string, limit = 200) => apiClient.get<{ users: UserInfo[]; count: number }>(
    `/api/users?platform_id=${encodeURIComponent(platformId)}&limit=${limit}`,
  ),

  // 获取用户详情
  get: (platformId: string, userId: string) => apiClient.get<{ ok: boolean; user?: UserInfo; error?: string }>(
    `/api/users?platform_id=${encodeURIComponent(platformId)}&user_id=${encodeURIComponent(userId)}`,
  ),

  // 创建用户
  create: (platformId: string, userId: string, platform?: string, nickname?: string) =>
    apiClient.post<{ ok: boolean; user: UserInfo; created: boolean }>('/api/user/create', {
      user_id: userId,
      platform_id: platformId,
      platform,
      nickname,
    }),

  // 更新用户
  update: (platformId: string, userId: string, data: { nickname?: string; platform?: string }) =>
    apiClient.post<{ ok: boolean; user: UserInfo }>('/api/user/update', {
      user_id: userId,
      platform_id: platformId,
      ...data,
    }),

  // 删除用户
  delete: (platformId: string, userId: string) =>
    apiClient.post<{ ok: boolean }>('/api/user/delete', { platform_id: platformId, user_id: userId }),

  // 绑定会话
  bindSession: (platformId: string, userId: string, sessionId: string) =>
    apiClient.post<{ ok: boolean; session_ids: string[] }>('/api/user/bind', {
      user_id: userId,
      platform_id: platformId,
      session_id: sessionId,
    }),

  // 解绑会话
  unbindSession: (platformId: string, userId: string, sessionId: string) =>
    apiClient.post<{ ok: boolean; session_ids: string[] }>('/api/user/unbind', {
      user_id: userId,
      platform_id: platformId,
      session_id: sessionId,
    }),

};
