import { create } from 'zustand';
import { modelApi } from '@/api/model';
import { sessionApi } from '@/api/session';
import { edictumApi } from '@/api/edictum';
import type { EdictumSessionConfig } from '@/api/types';
import type { LLMConfig, EmbeddingConfig, ReRankConfig, ASRConfig, ModelConfig, ModelType, SessionClassConfig } from '@/api/types';


interface ConfigState {
  // 模型配置
  llmConfigs: Record<string, LLMConfig>;
  embeddingConfigs: Record<string, EmbeddingConfig>;
  rerankConfigs: Record<string, ReRankConfig>;
  asrConfigs: Record<string, ASRConfig>;

  // 会话类配置
  sessionClasses: Record<string, SessionClassConfig>;
  edictumConfigs: Record<string, EdictumSessionConfig>;

  // 操作
  fetchModels: (type: ModelType) => Promise<void>;
  fetchAllModels: () => Promise<void>;
  fetchSessionClasses: () => Promise<void>;
  fetchEdictumConfigs: () => Promise<void>;
  createModel: (type: ModelType, name: string, config: Partial<ModelConfig>) => Promise<boolean>;
  updateModel: (type: ModelType, name: string, config: Partial<ModelConfig>) => Promise<boolean>;
  deleteModel: (type: ModelType, name: string) => Promise<boolean>;
}

let sessionClassRequest = 0;
let edictumRequest = 0;

export const useConfigStore = create<ConfigState>((set, get) => ({
  llmConfigs: {},
  embeddingConfigs: {},
  rerankConfigs: {},
  asrConfigs: {},
  sessionClasses: {},
  edictumConfigs: {},

  fetchModels: async (type: ModelType) => {
    const data = await modelApi.list(type);
    if (type === 'llm') set({ llmConfigs: data as Record<string, LLMConfig> });
    else if (type === 'embedding') set({ embeddingConfigs: data as Record<string, EmbeddingConfig> });
    else if (type === 'asr') set({ asrConfigs: data as Record<string, ASRConfig> });
    else set({ rerankConfigs: data as Record<string, ReRankConfig> });
  },

  fetchAllModels: async () => {
    await Promise.all([
      get().fetchModels('llm'),
      get().fetchModels('embedding'),
      get().fetchModels('rerank'),
      get().fetchModels('asr'),
    ]);
  },

  fetchSessionClasses: async () => {
    const request = ++sessionClassRequest;
    const data = await sessionApi.list();
    if (request === sessionClassRequest) set({ sessionClasses: data });
  },

  fetchEdictumConfigs: async () => {
    const request = ++edictumRequest;
    const edictumConfigs = await edictumApi.list();
    if (request === edictumRequest) set({ edictumConfigs });
  },

  createModel: async (type, name, config) => {
    try {
      await modelApi.create(type, name, config);
      await get().fetchModels(type);
      return true;
    } catch {
      return false;
    }
  },

  updateModel: async (type, name, config) => {
    try {
      await modelApi.update(type, name, config);
      await get().fetchModels(type);
      return true;
    } catch {
      return false;
    }
  },

  deleteModel: async (type, name) => {
    try {
      await modelApi.delete(type, name);
      await get().fetchModels(type);
      return true;
    } catch {
      return false;
    }
  },
}));
