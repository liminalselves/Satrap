export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:19870';

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
