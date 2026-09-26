import { controlApi } from './control';
import type { ModelConfig, ModelType } from './types';


export const modelApi = {
  // 列出模型配置
  list: (type: ModelType) =>
    controlApi.listModels(type),

  // 创建模型配置
  create: (type: ModelType, name: string, config: Partial<ModelConfig>) =>
    controlApi.createModel(type, name, config),

  // 更新模型配置
  update: (type: ModelType, name: string, config: Partial<ModelConfig>) =>
    controlApi.updateModel(type, name, config),

  // 删除模型配置
  delete: (type: ModelType, name: string) =>
    controlApi.deleteModel(type, name),
};
