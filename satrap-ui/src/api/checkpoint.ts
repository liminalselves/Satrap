import { apiClient } from './client';
import type { Checkpoint } from './types';

export const checkpointApi = {
  // 列出检查点
  list: (platformId: string, conversationId: string) =>
    apiClient.get<{
      conversation_id: string;
      checkpoints: Checkpoint[];
      branches: Checkpoint[];
    }>(`/api/checkpoints?platform_id=${encodeURIComponent(platformId)}&conversation=${encodeURIComponent(conversationId)}`),

  // 查看血缘
  traceLineage: (platformId: string, checkpointId: string) =>
    apiClient.get<{ checkpoint_id: string; lineage: Checkpoint[] }>(
      `/api/checkpoint/lineage?platform_id=${encodeURIComponent(platformId)}&checkpoint_id=${encodeURIComponent(checkpointId)}`
    ),

  // 列出变更记录
  listMutations: (platformId: string, conversationId: string) =>
    apiClient.get<{ conversation_id: string; mutations: Checkpoint[] }>(
      `/api/checkpoint/audit?platform_id=${encodeURIComponent(platformId)}&conversation=${encodeURIComponent(conversationId)}`
    ),

  // 创建检查点
  create: (platformId: string, conversationId: string, name?: string, description?: string) =>
    apiClient.post<{ checkpoint_id: string; ok: boolean }>('/api/checkpoint/create', {
      conversation: conversationId,
      platform_id: platformId,
      name,
      description,
    }),

  // 回滚
  rollback: (platformId: string, conversationId: string, checkpointId: string) =>
    apiClient.post<{ checkpoint_id: string; ok: boolean }>('/api/checkpoint/rollback', {
      conversation: conversationId,
      platform_id: platformId,
      checkpoint_id: checkpointId,
    }),

  // 重试
  retry: (platformId: string, conversationId: string, checkpointId: string) =>
    apiClient.post<{ checkpoint_id: string; ok: boolean }>('/api/checkpoint/retry', {
      conversation: conversationId,
      platform_id: platformId,
      checkpoint_id: checkpointId,
    }),

  // Fork 分支
  fork: (platformId: string, conversationId: string, branchName: string, checkpointId?: string) =>
    apiClient.post<{ conversation_id: string; ok: boolean }>('/api/checkpoint/fork', {
      conversation: conversationId,
      platform_id: platformId,
      branch_name: branchName,
      checkpoint_id: checkpointId,
    }),
};
