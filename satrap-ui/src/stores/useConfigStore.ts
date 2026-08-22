import { create } from 'zustand';
import { modelApi } from '@/api/model';
import { sessionApi } from '@/api/session';
import type { LLMConfig, EmbeddingConfig, ReRankConfig, SessionClassConfig } from '@/api/types';

type ModelType = 'llm' | 'embedding' | 'rerank';

interface ConfigState {
  // 模型配置
  llmConfigs: Record<string, LLMConfig>;
  embeddingConfigs: Record<string, EmbeddingConfig>;
  rerankConfigs: Record<string, ReRankConfig>;
  modelLoading: boolean;

  // 会话类配置
  sessionClasses: Record<string, SessionClassConfig>;
  sessionLoading: boolean;

  // 操作
  fetchModels: (type: ModelType) => Promise<void>;
  fetchAllModels: () => Promise<void>;
  fetchSessionClasses: () => Promise<void>;
  createModel: (type: ModelType, name: string, config: Partial<LLMConfig | EmbeddingConfig | ReRankConfig>) => Promise<boolean>;
  updateModel: (type: ModelType, name: string, config: Partial<LLMConfig | EmbeddingConfig | ReRankConfig>) => Promise<boolean>;
  deleteModel: (type: ModelType, name: string) => Promise<boolean>;
}

export const useConfigStore = create<ConfigState>((set, get) => ({
  llmConfigs: {},
  embeddingConfigs: {},
  rerankConfigs: {},
  modelLoading: false,
  sessionClasses: {},
  sessionLoading: false,

  fetchModels: async (type: ModelType) => {
    set({ modelLoading: true });
    try {
      const data = await modelApi.list(type);
      if (type === 'llm') set({ llmConfigs: data as Record<string, LLMConfig> });
      else if (type === 'embedding') set({ embeddingConfigs: data as Record<string, EmbeddingConfig> });
      else set({ rerankConfigs: data as Record<string, ReRankConfig> });
    } finally {
      set({ modelLoading: false });
    }
  },

  fetchAllModels: async () => {
    await Promise.all([
      get().fetchModels('llm'),
      get().fetchModels('embedding'),
      get().fetchModels('rerank'),
    ]);
  },

  fetchSessionClasses: async () => {
    set({ sessionLoading: true });
    try {
      const data = await sessionApi.list();
      set({ sessionClasses: data });
    } finally {
      set({ sessionLoading: false });
    }
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
