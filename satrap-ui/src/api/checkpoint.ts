import { apiClient } from './client';
import type { Checkpoint } from './types';

export const checkpointApi = {
  // 列出检查点
  list: (conversationId: string) =>
    apiClient.get<{
      conversation_id: string;
      checkpoints: Checkpoint[];
      branches: Checkpoint[];
    }>(`/api/checkpoints?conversation=${conversationId}`),

  // 列出分支
  listBranches: (conversationId: string) =>
    apiClient.get<{ conversation_id: string; branches: Checkpoint[] }>(
      `/api/checkpoint/branches?conversation=${conversationId}`
    ),

  // 查看血缘
  traceLineage: (checkpointId: string) =>
    apiClient.get<{ checkpoint_id: string; lineage: Checkpoint[] }>(
      `/api/checkpoint/lineage?checkpoint_id=${checkpointId}`
    ),

  // 列出变更记录
  listMutations: (conversationId: string) =>
    apiClient.get<{ conversation_id: string; mutations: Checkpoint[] }>(
      `/api/checkpoint/audit?conversation=${conversationId}`
    ),

  // 创建检查点
  create: (conversationId: string, name?: string, description?: string) =>
    apiClient.post<{ checkpoint_id: string; ok: boolean }>('/api/checkpoint/create', {
      conversation: conversationId,
      name,
      description,
    }),

  // 回滚
  rollback: (conversationId: string, checkpointId: string) =>
    apiClient.post<{ checkpoint_id: string; ok: boolean }>('/api/checkpoint/rollback', {
      conversation: conversationId,
      checkpoint_id: checkpointId,
    }),

  // 重试
  retry: (conversationId: string, checkpointId: string) =>
    apiClient.post<{ checkpoint_id: string; ok: boolean }>('/api/checkpoint/retry', {
      conversation: conversationId,
      checkpoint_id: checkpointId,
    }),

  // Fork 分支
  fork: (conversationId: string, branchName: string, checkpointId?: string) =>
    apiClient.post<{ conversation_id: string; ok: boolean }>('/api/checkpoint/fork', {
      conversation: conversationId,
      branch_name: branchName,
      checkpoint_id: checkpointId,
    }),
};
