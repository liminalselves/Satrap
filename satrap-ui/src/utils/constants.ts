export interface RuntimeServiceConfig {
  backend_api: string;
  control_api: string;
  chat_api: string;
}

interface RuntimeOverrides {
  backend?: string;
  control?: string;
  chat?: string;
}

const DEFAULT_RUNTIME_CONFIG: RuntimeServiceConfig = {
  backend_api: 'http://127.0.0.1:19870',
  control_api: 'http://127.0.0.1:19871',
  chat_api: 'http://127.0.0.1:19872',
};

let runtimeConfig = { ...DEFAULT_RUNTIME_CONFIG };

export function resolveRuntimeConfig(
  discovered: Partial<RuntimeServiceConfig> | null,
  pageOrigin: string,
  overrides: RuntimeOverrides = {},
): RuntimeServiceConfig {
  const controlApi = discovered
    ? discovered.control_api || pageOrigin || DEFAULT_RUNTIME_CONFIG.control_api
    : DEFAULT_RUNTIME_CONFIG.control_api;
  return {
    backend_api: overrides.backend || discovered?.backend_api || DEFAULT_RUNTIME_CONFIG.backend_api,
    control_api: overrides.control || controlApi,
    chat_api: overrides.chat || discovered?.chat_api || DEFAULT_RUNTIME_CONFIG.chat_api,
  };
}

export async function loadRuntimeConfig(): Promise<RuntimeServiceConfig> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 2000);
  let discovered: Partial<RuntimeServiceConfig> | null = null;
  try {
    const response = await fetch('/ui-config.json', {
      cache: 'no-store',
      signal: controller.signal,
    });
    if (response.ok) {
      discovered = await response.json() as Partial<RuntimeServiceConfig>;
    }
  } catch {
    discovered = null;
  } finally {
    window.clearTimeout(timeout);
  }
  runtimeConfig = resolveRuntimeConfig(discovered, window.location.origin, {
    backend: import.meta.env.VITE_API_BASE_URL,
    control: import.meta.env.VITE_CONTROL_API_URL,
    chat: import.meta.env.VITE_CHAT_API_URL,
  });
  return runtimeConfig;
}

export const getApiBaseUrl = () => runtimeConfig.backend_api;
export const getControlApiUrl = () => runtimeConfig.control_api;
export const getChatApiUrl = () => runtimeConfig.chat_api;

export const MODEL_TYPES = [
  { value: 'llm', label: 'LLM 配置' },
  { value: 'embedding', label: 'Embedding 配置' },
  { value: 'rerank', label: 'ReRank 配置' },
] as const;

export const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const;

export const PLATFORM_TYPES = ['misskey', 'onebot'] as const;

export type ModelType = (typeof MODEL_TYPES)[number]['value'];
export type LogLevel = (typeof LOG_LEVELS)[number];
export type PlatformType = (typeof PLATFORM_TYPES)[number];
