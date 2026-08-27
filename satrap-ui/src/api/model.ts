import { controlApi } from './control';
import type { LLMConfig, EmbeddingConfig, ReRankConfig } from './types';

type ModelType = 'llm' | 'embedding' | 'rerank';

export const modelApi = {
  // 列出模型配置
  list: (type: ModelType) =>
    controlApi.listModels(type),

  // 创建模型配置
  create: (type: ModelType, name: string, config: Partial<LLMConfig | EmbeddingConfig | ReRankConfig>) =>
    controlApi.createModel(type, name, config),

  // 更新模型配置
  update: (type: ModelType, name: string, config: Partial<LLMConfig | EmbeddingConfig | ReRankConfig>) =>
    controlApi.updateModel(type, name, config),

  // 删除模型配置
  delete: (type: ModelType, name: string) =>
    controlApi.deleteModel(type, name),
};
