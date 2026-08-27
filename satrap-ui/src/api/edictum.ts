import { controlApi } from './control';
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
};
