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

function alignLoopbackHost(serviceUrl: string, pageOrigin: string): string {
  try {
    const service = new URL(serviceUrl);
    const page = new URL(pageOrigin);
    const loopback = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);
    if (loopback.has(service.hostname) && loopback.has(page.hostname)) {
      service.hostname = page.hostname;
    }
    return service.origin;
  } catch {
    return serviceUrl;
  }
}

export function resolveRuntimeConfig(
  discovered: Partial<RuntimeServiceConfig> | null,
  pageOrigin: string,
  overrides: RuntimeOverrides = {},
): RuntimeServiceConfig {
  const controlApi = discovered
    ? discovered.control_api || pageOrigin || DEFAULT_RUNTIME_CONFIG.control_api
    : DEFAULT_RUNTIME_CONFIG.control_api;
  return {
    backend_api: alignLoopbackHost(
      overrides.backend || discovered?.backend_api || DEFAULT_RUNTIME_CONFIG.backend_api,
      pageOrigin,
    ),
    control_api: alignLoopbackHost(overrides.control || controlApi, pageOrigin),
    chat_api: alignLoopbackHost(
      overrides.chat || discovered?.chat_api || DEFAULT_RUNTIME_CONFIG.chat_api,
      pageOrigin,
    ),
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

export const THINKING_FIELD_OPTIONS = [
  {
    value: 'thinking.type',
    label: 'thinking.type',
    description: '使用 enabled/disabled 控制思考开关',
  },
  {
    value: 'reasoning_effort',
    label: 'reasoning_effort',
    description: '传递当前选择的思考强度',
  },
  {
    value: 'enable_thinking',
    label: 'enable_thinking',
    description: '使用 true/false 控制思考开关',
  },
  {
    value: 'thinking_level',
    label: 'thinking_level',
    description: '传递当前选择的思考强度',
  },
] as const;

export const THINKING_LEVEL_OPTIONS = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
  { value: 'xhigh', label: '高+' },
  { value: 'max', label: '超高' },
  { value: 'ultra', label: '最高' },
] as const;

export const DEFAULT_THINKING_LEVELS = ['low', 'medium', 'high'] as const;

const THINKING_LEVEL_FIELDS = new Set(['thinking_level', 'reasoning_effort']);

export function getThinkingOptions(config?: {
  thinking_fields?: string[];
  thinking_levels?: string[];
}): { value: string; label: string }[] {
  const fields = config?.thinking_fields ?? [];
  const offOption = { value: 'off', label: '关闭' };
  if (fields.some((field) => THINKING_LEVEL_FIELDS.has(field))) {
    const enabled = config?.thinking_levels ?? [...DEFAULT_THINKING_LEVELS];
    return [offOption, ...THINKING_LEVEL_OPTIONS.filter((option) => enabled.includes(option.value))];
  }
  if (fields.length > 0) {
    return [offOption, { value: 'high', label: '开启' }];
  }
  return [offOption];
}

export const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const;

export const PLATFORM_TYPES = ['misskey', 'onebot'] as const;

export type ModelType = (typeof MODEL_TYPES)[number]['value'];
export type LogLevel = (typeof LOG_LEVELS)[number];
export type PlatformType = (typeof PLATFORM_TYPES)[number];
