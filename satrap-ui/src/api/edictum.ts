import { controlApi } from './control';
import { backendApi } from './backend';
import type { EdictumSessionConfig } from './types';

export const edictumApi = {
  listTypes: () => controlApi.listEdictumTypes(),

  listPlugins: () => controlApi.listEdictumPlugins(),

  list: () => controlApi.listEdictumSessions(),

  get: (name: string) => controlApi.getEdictumSession(name),

  create: (data: {
    name: string;
    edictum_type: string;
    enabled?: boolean;
    description?: string;
    model_name?: string;
    params?: Record<string, unknown>;
    plugins?: EdictumSessionConfig['plugins'];
  }) => controlApi.createEdictumSession(data),

  update: (name: string, data: {
    name?: string;
    edictum_type?: string;
    enabled?: boolean;
    description?: string;
    model_name?: string;
    params?: Record<string, unknown>;
    plugins?: EdictumSessionConfig['plugins'];
  }) => controlApi.updateEdictumSession(name, data),

  enable: (name: string) => controlApi.setEdictumSessionEnabled(name, true),

  disable: (name: string) => controlApi.setEdictumSessionEnabled(name, false),

  remove: (name: string) => controlApi.deleteEdictumSession(name),

  applyRuntimeChanges: () => backendApi.reloadConfig(),

  previewRuntimeChanges: (
    configName: string,
    plugins?: EdictumSessionConfig['plugins'],
  ) => backendApi.previewEdictumPlugins({
    config_name: configName,
    ...(plugins === undefined ? {} : { plugins }),
  }),

  retryRuntimeChanges: (data: {
    config_name?: string;
    session_refs?: Array<{ platform_id: string; session_id: string }>;
  }) => backendApi.reconcileEdictumPlugins(data),

  previewFullRuntimeChanges: (
    configName: string,
    config: Record<string, unknown>,
  ) => backendApi.previewEdictumRuntime({
    config_name: configName,
    config,
  }),

  applyFullRuntimeChanges: (configName?: string) => backendApi.reconcileEdictumRuntime({
    config_name: configName,
  }),
};
