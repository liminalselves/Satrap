import { errorText } from '@/utils/errorText';
import { controlClient } from './control';

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
