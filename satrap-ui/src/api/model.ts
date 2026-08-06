import { apiClient } from './client';
import type { LLMConfig, EmbeddingConfig, ReRankConfig } from './types';

type ModelType = 'llm' | 'embedding' | 'rerank';

export const modelApi = {
  // 列出模型配置
  list: (type: ModelType) =>
    apiClient.get<Record<string, LLMConfig | EmbeddingConfig | ReRankConfig>>(
      `/api/config/models?type=${type}`
    ),

  // 创建模型配置
  create: (type: ModelType, name: string, config: Partial<LLMConfig | EmbeddingConfig | ReRankConfig>) =>
    apiClient.post<{ ok: boolean }>(`/api/config/models/${type}/${name}`, config),

  // 更新模型配置
  update: (type: ModelType, name: string, config: Partial<LLMConfig | EmbeddingConfig | ReRankConfig>) =>
    apiClient.patch<{ ok: boolean }>(`/api/config/models/${type}/${name}`, config),

  // 删除模型配置
  delete: (type: ModelType, name: string) =>
    apiClient.delete<{ ok: boolean }>(`/api/config/models/${type}/${name}`),
};
