import { LOG_LEVELS } from '@/utils/constants';

/** 日志正文接口与预览一致使用五档级别, 未知级别仍保留原样展示 */
export type LogLevel = (typeof LOG_LEVELS)[number];
export type LogSource = 'control' | 'backend' | 'chat';
export type ConnectionState = 'connected' | 'waiting' | 'auth' | 'offline';

export const LEVELS: LogLevel[] = [...LOG_LEVELS];

export interface LevelMeta {
  label: LogLevel;
  tint: 'purple' | 'green' | 'orange' | 'red';
  /** 终端固定配色, 不随主题变化 */
  terminal: string;
  bar: string;
}

export const LEVEL_META: Record<LogLevel, LevelMeta> = {
  DEBUG: { label: 'DEBUG', tint: 'purple', terminal: 'text-[#c4a7e7]', bar: 'bg-[#a78bfa]' },
  INFO: { label: 'INFO', tint: 'green', terminal: 'text-[#7ee787]', bar: 'bg-[#3fb950]' },
  WARNING: { label: 'WARNING', tint: 'orange', terminal: 'text-[#ffb454]', bar: 'bg-[#f0883e]' },
  ERROR: { label: 'ERROR', tint: 'red', terminal: 'text-[#ff7b72]', bar: 'bg-[#f85149]' },
  CRITICAL: { label: 'CRITICAL', tint: 'red', terminal: 'font-semibold text-[#ff6b81]', bar: 'bg-[#e5484d]' },
};

export interface SourceMeta {
  id: LogSource;
  name: string;
  short: string;
  /** 分组色, 只用于图标磁片 */
  tint: 'accent' | 'orange' | 'teal';
}

export const SOURCE_META: Record<LogSource, SourceMeta> = {
  control: { id: 'control', name: 'Control', short: 'CTL', tint: 'accent' },
  backend: { id: 'backend', name: '平台 Backend', short: 'BE', tint: 'orange' },
  chat: { id: 'chat', name: 'Chat', short: 'CHAT', tint: 'teal' },
};

export const SOURCES: SourceMeta[] = [SOURCE_META.control, SOURCE_META.backend, SOURCE_META.chat];

export function sourceMeta(id: string | undefined): SourceMeta {
  return SOURCE_META[id as LogSource] ?? SOURCE_META.control;
}

export function isLogLevel(value: string): value is LogLevel {
  return (LEVELS as string[]).includes(value);
}

export function levelMeta(level: string): LevelMeta {
  return isLogLevel(level) ? LEVEL_META[level] : LEVEL_META.INFO;
}

const pad = (value: number, length = 2) => String(value).padStart(length, '0');

/** 秒级 Unix 时间; 缺失时返回占位符, 不把未知时间当成 0 */
export function formatTime(timestamp: number | undefined, withMs = true): string {
  if (typeof timestamp !== 'number' || !Number.isFinite(timestamp)) return '—';
  const date = new Date(timestamp * 1000);
  const base = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  return withMs ? `${base}.${pad(date.getMilliseconds(), 3)}` : base;
}

/** 把生产状态文案映射为预览的连接状态 */
export function connectionState(status: string | undefined): ConnectionState {
  if (!status) return 'waiting';
  if (status === '已连接') return 'connected';
  if (status === '需要认证') return 'auth';
  if (status.startsWith('离线')) return 'offline';
  return 'waiting';
}

export function statusTone(state: ConnectionState): 'green' | 'accent' | 'orange' | 'red' {
  if (state === 'connected') return 'green';
  if (state === 'auth') return 'orange';
  if (state === 'offline') return 'red';
  return 'accent';
}

export const STATUS_TEXT_CLASS: Record<ConnectionState, string> = {
  connected: 'text-green',
  waiting: 'text-accent',
  auth: 'text-orange',
  offline: 'text-red',
};

/** 生产日志正文带有 `[服务名] ` 前缀, 预览把来源放进独立列 */
export function splitSourcePrefix(content: string, source: string | undefined): { prefix: string; body: string } {
  if (!source) return { prefix: '', body: content };
  const prefix = `[${sourceMeta(source).name}] `;
  return content.startsWith(prefix) ? { prefix, body: content.slice(prefix.length) } : { prefix: '', body: content };
}

export interface LogBucket {
  start: number;
  end: number;
  total: number;
  counts: Record<LogLevel, number>;
  /** 桶内是否有可信统计覆盖 */
  covered: boolean;
  /** 覆盖不完整: 部分来源在该桶没有数据 */
  partial: boolean;
}

export function emptyLevelCounts(): Record<LogLevel, number> {
  return { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 };
}
