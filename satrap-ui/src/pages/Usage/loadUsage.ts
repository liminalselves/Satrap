import type {
  ModelMetrics, Overview, SessionMetrics, StatsWindowQuery,
} from '@/api/contracts';
import { statsApi } from '@/api/contracts';

export interface UsageSourceData {
  overview?: Overview;
  models?: ModelMetrics[];
  sessions?: SessionMetrics[];
}

export interface UsageLoadResult {
  status: 'success' | 'partial' | 'failure' | 'aborted';
  data: UsageSourceData;
  errors: string[];
}

export type UsageApi = Pick<typeof statsApi, 'overview' | 'models' | 'sessions'>;

const SOURCE_LABELS = ['概览', '模型', '会话'] as const;

function requireObject(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label}响应格式无效`);
  }
  return value as Record<string, unknown>;
}

function parseOverview(value: unknown): Overview {
  return requireObject(value, '概览') as unknown as Overview;
}

function parseModels(value: unknown): ModelMetrics[] {
  const record = requireObject(value, '模型');
  if (!Array.isArray(record.models)) throw new Error('模型响应缺少 models 数组');
  return record.models as ModelMetrics[];
}

function parseSessions(value: unknown): SessionMetrics[] {
  const record = requireObject(value, '会话');
  if (!Array.isArray(record.sessions)) throw new Error('会话响应缺少 sessions 数组');
  return record.sessions as SessionMetrics[];
}

function reasonText(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

async function read<T>(request: Promise<unknown>, parse: (value: unknown) => T): Promise<T> {
  return parse(await request);
}

/**
 * 并行读取用量数据并分别保留成功来源
 *
 * 单个来源失败或响应格式错误时保留其它来源, 全部失败才返回 failure;
 * 格式错误不会被当成空数组, 组件据此保留上次缓存并提示重试
 */
export async function loadUsage(
  query: StatsWindowQuery,
  signal: AbortSignal | undefined,
  api: UsageApi = statsApi,
): Promise<UsageLoadResult> {
  const settled = await Promise.allSettled([
    read(api.overview(query, signal), parseOverview),
    read(api.models({ ...query, limit: 100 }, signal), parseModels),
    read(api.sessions({ ...query, limit: 100 }, signal), parseSessions),
  ]);
  if (signal?.aborted) return { status: 'aborted', data: {}, errors: [] };

  const errors: string[] = [];
  const data: UsageSourceData = {};
  settled.forEach((result, index) => {
    if (result.status === 'rejected') {
      errors.push(`${SOURCE_LABELS[index]}: ${reasonText(result.reason)}`);
    }
  });

  const [overview, models, sessions] = settled;
  if (overview.status === 'fulfilled') data.overview = overview.value;
  if (models.status === 'fulfilled') data.models = models.value;
  if (sessions.status === 'fulfilled') data.sessions = sessions.value;
  const succeeded = settled.filter((result) => result.status === 'fulfilled').length;
  if (succeeded === 0) return { status: 'failure', data, errors };
  return { status: succeeded === settled.length ? 'success' : 'partial', data, errors };
}

/** 轮询只在新错误签名出现时通知, 成功或同一错误重复出现不再 toast */
export function nextUsageNotifySignature(
  current: string,
  result: UsageLoadResult,
): { signature: string; notify: boolean } {
  if (result.status === 'aborted') return { signature: current, notify: false };
  if (result.status === 'success') return { signature: '', notify: false };
  const signature = result.errors.join('\n');
  return { signature, notify: signature !== current && errorsVisible(result) };
}

function errorsVisible(result: UsageLoadResult): boolean {
  return result.status === 'partial' || result.status === 'failure';
}
