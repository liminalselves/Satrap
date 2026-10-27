// 插件管理页使用的扩展契约: 目录健康批量读取与宿主执行总闸
import { controlClient } from './control';
import { VersionedConfigRead } from './configVersions';
import { pluginHealthApi, type PluginHealth, type SourceState } from './contracts';
import type { ManagedPlugin, PluginRuntimeResult } from './types';

/** 清单可选元数据, 旧服务不返回时保持 undefined */
export interface PluginManifestExtras {
  display_name?: string;
  icon?: string;
  license?: string;
  homepage?: string;
  repository?: string;
}

/** 目录条目, 健康数据与宿主总闸由新接口补齐 */
export interface CatalogPlugin extends ManagedPlugin, PluginManifestExtras {
  health?: PluginHealth;
  execution_enabled?: boolean;
  host_revision?: number;
}

export interface PluginExecutionState {
  execution_enabled: boolean;
  revision: number;
  updated_at: number | null;
  execution_updated_at: number | null;
}

export interface PluginExecutionSaveResult extends PluginExecutionState {
  saved?: boolean;
  runtime?: PluginRuntimeResult[];
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);

function isCatalogPlugin(value: unknown): value is CatalogPlugin {
  if (!isObject(value) || typeof value.name !== 'string' || !isObject(value.config_schema) || !isObject(value.capabilities)) return false;
  if (value.health !== undefined && (!isObject(value.health) || typeof value.health.state !== 'string')) return false;
  if (value.execution_enabled !== undefined && typeof value.execution_enabled !== 'boolean') return false;
  if (value.host_revision !== undefined && (typeof value.host_revision !== 'number' || !Number.isFinite(value.host_revision))) return false;
  return true;
}

/**
 * 读取目录并批量附带健康资料
 * 单一请求 `/config/plugins?include=health`, 避免为每个插件单独探测运行时
 */
export const pluginCatalogHealthRead = new VersionedConfigRead<CatalogPlugin[]>(
  ['plugin_catalog', 'edictum_configs', 'chat_plugins'],
  async (signal) => {
    const payload = await pluginHealthApi.list<ManagedPlugin>(signal);
    const plugins = payload?.plugins;
    if (!Array.isArray(plugins) || plugins.some((plugin) => !isCatalogPlugin(plugin))) {
      throw new Error('插件目录响应格式无效');
    }
    return plugins as CatalogPlugin[];
  },
);

/** 单插件宿主执行总闸, 与插件作者参数分开保存 */
export const pluginExecutionApi = {
  get: async (name: string, signal?: AbortSignal): Promise<PluginExecutionState> => {
    const { data } = await controlClient.get<PluginExecutionState>(`/config/plugins/${encodeURIComponent(name)}/execution`, { signal });
    if (!isObject(data) || typeof data.execution_enabled !== 'boolean' || typeof data.revision !== 'number') {
      throw new Error('插件宿主状态响应格式无效');
    }
    return data;
  },
  save: async (name: string, enabled: boolean, expected_revision: number): Promise<PluginExecutionSaveResult> => {
    const { data } = await controlClient.put<PluginExecutionSaveResult>(`/config/plugins/${encodeURIComponent(name)}/execution`, {
      execution_enabled: enabled,
      expected_revision,
    });
    if (!isObject(data) || typeof data.execution_enabled !== 'boolean' || typeof data.revision !== 'number') {
      throw new Error('插件宿主状态响应格式无效');
    }
    return data;
  },
};

/** 汇总目录健康覆盖, 缺失来源如实标记为未知 */
export function pluginHealthCoverage(plugins: readonly CatalogPlugin[]) {
  const sources = new Map<string, SourceState>();
  let incomplete = false;
  let known = false;
  for (const plugin of plugins) {
    const coverage = plugin.health?.coverage;
    if (!coverage) continue;
    known = true;
    if (!coverage.complete) incomplete = true;
    for (const source of coverage.sources) {
      if (source?.service) sources.set(source.service, source.state);
    }
  }
  return { sources: [...sources.entries()].map(([service, state]) => ({ service, state })), incomplete, known };
}
