import { controlApi } from './control';
import { VersionedConfigRead, readConfigSnapshot } from './configVersions';
import type { GlobalPluginConfig, ManagedPlugin } from './types';

const isObject = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
export const pluginCatalogRead = new VersionedConfigRead(['plugin_catalog', 'edictum_configs', 'chat_plugins'], async (signal) => {
  const plugins = await controlApi.listPlugins(signal);
  if (!Array.isArray(plugins) || plugins.some((plugin) => !isObject(plugin) || typeof plugin.name !== 'string' || !isObject(plugin.config_schema) || !isObject(plugin.capabilities))) throw new Error('插件目录响应格式无效');
  return plugins;
}, undefined, (signal) => readConfigSnapshot<ManagedPlugin[]>('plugin_catalog', signal));
export const pluginModelRead = new VersionedConfigRead(['models'], async (signal) => {
  const result = await controlApi.pluginModelOptions(signal);
  if (!isObject(result?.options) || Object.values(result.options).some((items) => !Array.isArray(items))) throw new Error('模型选项响应格式无效');
  return result;
});
export const pluginKnowledgeRead = new VersionedConfigRead([], (signal) => controlApi.ragList('local', '', '', signal));
const globalReads = new Map<string, VersionedConfigRead<GlobalPluginConfig>>();

export function globalPluginRead(name: string) {
  let resource = globalReads.get(name);
  if (!resource) {
    resource = new VersionedConfigRead([], async (signal) => {
      const result = await controlApi.getGlobalPluginConfig(name, signal);
      if (!result?.ok || !isObject(result.schema) || !isObject(result.config) || !isObject(result.overrides) || typeof result.revision !== 'string') throw new Error('插件参数响应格式无效');
      return result;
    });
    globalReads.set(name, resource);
    if (globalReads.size > 128) globalReads.delete(globalReads.keys().next().value as string);
  }
  return resource;
}
