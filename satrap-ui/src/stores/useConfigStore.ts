import { create } from 'zustand';
import { modelApi } from '@/api/model';
import { sessionApi } from '@/api/session';
import { edictumApi } from '@/api/edictum';
import { getConfigVersions, VersionedConfigRead, type ConfigReadOptions, type ConfigResource } from '@/api/configVersions';
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
  fetchModels: (type: ModelType, options?: ConfigReadOptions) => Promise<void>;
  fetchAllModels: () => Promise<void>;
  fetchSessionClasses: (options?: ConfigReadOptions) => Promise<void>;
  fetchEdictumConfigs: (options?: ConfigReadOptions) => Promise<void>;
  watchConfigs: (names: readonly WatchedConfig[]) => () => void;
  refreshWatchedConfigs: (signal?: AbortSignal) => Promise<void>;
  createModel: (type: ModelType, name: string, config: Partial<ModelConfig>) => Promise<boolean>;
  updateModel: (type: ModelType, name: string, config: Partial<ModelConfig>) => Promise<boolean>;
  deleteModel: (type: ModelType, name: string) => Promise<boolean>;
}

let sessionClassRequest = 0;
let edictumRequest = 0;
const modelRequests: Record<ModelType, number> = { llm: 0, embedding: 0, rerank: 0, asr: 0 };
export type WatchedConfig = ModelType | 'sessions' | 'edictum';
const watchers = new Map<WatchedConfig, number>();
const modelTypes: ModelType[] = ['llm', 'embedding', 'rerank', 'asr'];
const modelReads = Object.fromEntries(modelTypes.map((type) => [type,
  new VersionedConfigRead(['models'], (signal) => modelApi.list(type, signal)),
])) as Record<ModelType, VersionedConfigRead<Record<string, ModelConfig>>>;
const sessionRead = new VersionedConfigRead(['session_classes'], (signal) => sessionApi.list(signal));
const edictumRead = new VersionedConfigRead(['edictum_configs', 'plugin_catalog'], (signal) => edictumApi.list(signal));

export const useConfigStore = create<ConfigState>((set, get) => ({
  llmConfigs: {},
  embeddingConfigs: {},
  rerankConfigs: {},
  asrConfigs: {},
  sessionClasses: {},
  edictumConfigs: {},

  fetchModels: async (type: ModelType, options?: ConfigReadOptions) => {
    const request = ++modelRequests[type];
    const data = await modelReads[type].read(options);
    if (request !== modelRequests[type]) return;
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

  fetchSessionClasses: async (options) => {
    const request = ++sessionClassRequest;
    const data = await sessionRead.read(options);
    if (request === sessionClassRequest) set({ sessionClasses: data });
  },

  fetchEdictumConfigs: async (options) => {
    const request = ++edictumRequest;
    const edictumConfigs = await edictumRead.read(options);
    if (request === edictumRequest) set({ edictumConfigs });
  },

  watchConfigs: (names) => {
    const unique = [...new Set(names)];
    unique.forEach((name) => watchers.set(name, (watchers.get(name) || 0) + 1));
    return () => unique.forEach((name) => {
      const count = (watchers.get(name) || 0) - 1;
      if (count > 0) watchers.set(name, count); else watchers.delete(name);
    });
  },

  refreshWatchedConfigs: async (signal) => {
    const names = [...watchers.keys()];
    if (!names.length) return;
    const resources: ConfigResource[] = [...new Set(names.flatMap((name): ConfigResource[] =>
      name === 'sessions' ? ['session_classes'] : name === 'edictum' ? ['edictum_configs', 'plugin_catalog'] : ['models']))];
    const versions = await getConfigVersions(resources, false, signal);
    const results = await Promise.allSettled(names.map((name) => name === 'sessions'
      ? get().fetchSessionClasses({ versions, signal }) : name === 'edictum'
        ? get().fetchEdictumConfigs({ versions, signal }) : get().fetchModels(name, { versions, signal })));
    const failures = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected');
    if (failures.length) throw new Error(failures.map((result) => result.reason instanceof Error ? result.reason.message : String(result.reason)).join('; '));
  },

  createModel: async (type, name, config) => {
    try {
      await modelApi.create(type, name, config);
      modelReads[type].invalidate();
      await get().fetchModels(type);
      return true;
    } catch {
      return false;
    }
  },

  updateModel: async (type, name, config) => {
    try {
      await modelApi.update(type, name, config);
      modelReads[type].invalidate();
      await get().fetchModels(type);
      return true;
    } catch {
      return false;
    }
  },

  deleteModel: async (type, name) => {
    try {
      await modelApi.delete(type, name);
      modelReads[type].invalidate();
      await get().fetchModels(type);
      return true;
    } catch {
      return false;
    }
  },
}));
