import { apiClient } from './client';
import type { BackendHealth } from './types';

export const backendApi = {
  // 获取后端健康状态
  health: () => apiClient.get<BackendHealth>('/api/health'),

  // 重载配置
  reloadConfig: () => apiClient.post<{ ok: boolean }>('/api/config/reload'),

  // 关闭后端
  shutdown: () => apiClient.post<{ ok: boolean }>('/api/shutdown'),
};
