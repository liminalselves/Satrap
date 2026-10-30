import { errorText } from '@/utils/errorText';
import { controlClient } from './control';
import { ensureApiSession, establishApiSession } from './auth';
import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';

export type LogSource = 'control' | 'backend' | 'chat';
export interface LogRecord {
  sequence: number;
  timestamp: number;
  pid: number;
  content: string;
  level: string;
  truncated: boolean;
}
export interface LogSnapshot {
  service: string;
  stream_id: string;
  entries: LogRecord[];
  next_cursor: number;
  has_more: boolean;
  reset: boolean;
}
export const logServices = () => [
  { id: 'control' as const, name: 'Control', url: getControlApiUrl() },
  { id: 'backend' as const, name: '平台 Backend', url: getApiBaseUrl() },
  { id: 'chat' as const, name: 'Chat', url: getChatApiUrl() },
];

export async function readLogSource(baseUrl: string, query: { limit: number; after?: number; stream_id?: string }, signal: AbortSignal): Promise<LogSnapshot> {
  await ensureApiSession(baseUrl);
  signal.throwIfAborted();
  const parameters = new URLSearchParams({ limit: String(query.limit),
    ...(query.after !== undefined ? { after: String(query.after) } : {}),
    ...(query.stream_id ? { stream_id: query.stream_id } : {}),
  });
  const fetchLogs = () => fetch(`${baseUrl}/api/logs?${parameters}`, { credentials: 'include', signal });
  let response = await fetchLogs();
  if (response.status === 401 || response.status === 403) {
    await establishApiSession(baseUrl, { background: true });
    signal.throwIfAborted();
    response = await fetchLogs();
  }
  if (!response.ok) throw new Error(`日志读取失败: HTTP ${response.status}`);
  const result = await response.json() as LogSnapshot;
  if (!Array.isArray(result.entries) || typeof result.stream_id !== 'string' || !Number.isInteger(result.next_cursor)) {
    throw new Error('日志响应格式无效');
  }
  return result;
}

export interface LogPolicy {
  enabled: boolean;
  retention_days: number;
}

export interface LogCleanupResult {
  created_at: number;
  retention_days: number;
  cutoff: string;
  policy_revision: string | null;
  deleted: string[];
  skipped: { file: string; reason: string }[];
  errors: { file: string; reason: string }[];
}

export interface LogPolicySnapshot {
  ok: boolean;
  policy: LogPolicy;
  revision: string;
  directory: string;
  config_path: string;
  last_cleanup: LogCleanupResult | null;
  maintenance_error: { created_at: number; error: string } | null;
  status_error: string | null;
}

export function loggingError(error: unknown): string {
  return errorText(error, '日志管理请求失败');
}

export const loggingApi = {
  read: async (signal?: AbortSignal): Promise<LogPolicySnapshot> => (await controlClient.get<LogPolicySnapshot>('/config/logging', { signal })).data,
  save: async (policy: LogPolicy, revision: string): Promise<LogPolicySnapshot> => (await controlClient.put<LogPolicySnapshot>('/config/logging', { policy, expected_revision: revision })).data,
  cleanup: async (revision: string): Promise<{ ok: boolean; result: LogCleanupResult }> => (await controlClient.post<{ ok: boolean; result: LogCleanupResult }>('/config/logging/cleanup', { expected_revision: revision }, { timeout: 60000 })).data,
};
