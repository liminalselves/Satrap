import { chatApi } from './chat';
import { controlApi } from './control';
import type { EdictumPluginConfigField } from './types';
import type { ModelOptions } from '@/components/common/PluginConfigFields';

export interface SessionPluginSettings {
  ok: boolean;
  schema: Record<string, EdictumPluginConfigField>;
  config: Record<string, unknown>;
  inherited: Record<string, unknown>;
  overrides: Record<string, unknown>;
  sources: Record<string, string>;
  inherited_sources?: Record<string, string>;
  revision: number;
  model_options: ModelOptions;
  runtime?: { status?: string; ok?: boolean };
}

export interface PluginSettingsContext { platformId: string; sessionId: string }

export const pluginSettingsApi = {
  get: (context: PluginSettingsContext, plugin: string) => context.platformId === 'chat'
    ? chatApi.getSessionPluginConfig(context.sessionId, plugin)
    : controlApi.getSessionPluginConfig(context.platformId, context.sessionId, plugin),
  save: (context: PluginSettingsContext, plugin: string, overrides: Record<string, unknown>, revision: number) => context.platformId === 'chat'
    ? chatApi.saveSessionPluginConfig(context.sessionId, plugin, overrides, revision)
    : controlApi.saveSessionPluginConfig(context.platformId, context.sessionId, plugin, overrides, revision),
};
