import { controlClient } from './control';
import { ensureApiSession, establishApiSession } from './auth';

export interface DebugObject {
  platform_id: string;
  self_id: string;
  conversation_kind: 'group' | 'private' | 'chat';
  target_id: string;
}
export type DebugMode = 'inherit' | 'on' | 'off';
export interface DebugPolicy {
  version: 1;
  default_enabled: boolean;
  retention_days: number;
  max_total_mb: number;
  max_body_mb: number;
  max_request_mb: number;
  objects: { object: DebugObject; mode: 'on' | 'off' }[];
}
export interface DebugPolicySnapshot { policy: DebugPolicy; revision: string }
export interface DebugContext {
  object_key: string;
  context_id: string;
  title: string;
  updated_at: number;
  request_count: number;
  error_count: number;
  object: DebugObject;
  service: string;
  context_kind?: 'main' | 'workflow';
}
export interface DebugRequest {
  request_id: string;
  context_id: string;
  parent_request_id: string;
  status: string;
  incomplete: number;
  updated_at: number;
  started_at: number;
  settings_revision: string;
}
export interface DebugEvent {
  event_id: string;
  sequence: number;
  category: string;
  phase: string;
  span_id: string;
  parent_span_id: string;
  status: string;
  recorded_at: number;
  metadata: Record<string, unknown>;
  body_slots: string[];
}
export interface DebugPage {
  has_more: boolean;
  next_cursor?: string | null;
}
export type ContextPage = DebugPage & { contexts: DebugContext[] };
export type RequestPage = DebugPage & { requests: DebugRequest[] };
export interface EventPage { events: DebugEvent[]; has_more: boolean }
export interface AvailableContextPage extends ContextPage { platforms: string[]; unconfirmed: number }

/** 独立认证所选日志服务, 不因其它服务离线而阻断查询 */
async function debugFetch(baseUrl: string, route: string, signal?: AbortSignal): Promise<Response> {
  await ensureApiSession(baseUrl);
  signal?.throwIfAborted();
  const send = () => fetch(`${baseUrl}${route}`, { credentials: 'include', signal });
  let response = await send();
  if (response.status === 401 || response.status === 403) {
    await establishApiSession(baseUrl, { background: true });
    signal?.throwIfAborted();
    response = await send();
  }
  if (!response.ok) {
    const data: unknown = await response.json().catch(() => null);
    const reason = data && typeof data === 'object' && 'error' in data && typeof data.error === 'string' ? data.error : `HTTP ${response.status}`;
    throw new Error(`高级 Debug 查询失败: ${reason}`);
  }
  return response;
}

async function readPage<T>(base: string, kind: string, key: string, query: Record<string, string>, signal?: AbortSignal): Promise<T> {
  const response = await debugFetch(base, `/api/debug/${kind}?${new URLSearchParams(query)}`, signal);
  const data: unknown = await response.json();
  if (!data || typeof data !== 'object' || !(key in data) || !Array.isArray((data as Record<string, unknown>)[key])
    || !('has_more' in data) || typeof data.has_more !== 'boolean') throw new Error('高级 Debug 分页响应格式无效');
  return data as T;
}

export const debugLoggingApi = {
  settings: async (signal?: AbortSignal): Promise<DebugPolicySnapshot> => {
    const { data } = await controlClient.get<DebugPolicySnapshot>('/config/debug-logging', { signal });
    if (!data?.policy || data.policy.version !== 1 || !Array.isArray(data.policy.objects) || typeof data.revision !== 'string') throw new Error('高级 Debug 设置响应格式无效');
    return data;
  },
  save: async (policy: DebugPolicy, revision: string): Promise<DebugPolicySnapshot> =>
    (await controlClient.put<DebugPolicySnapshot>('/config/debug-logging', { policy, expected_revision: revision })).data,
  available: async (platformId: string, cursor = '', signal?: AbortSignal): Promise<AvailableContextPage> =>
    (await controlClient.get<AvailableContextPage>('/config/debug-logging/contexts', { params: { platform_id: platformId, ...(cursor ? { cursor } : {}) }, signal })).data,
  health: async (base: string, signal?: AbortSignal): Promise<{ database_bytes: number; wal_bytes: number; used_bytes: number; pending_events: number; write_error: string; maintenance_error: string; quota_blocked?: boolean; dropped_events?: number; dropped_requests?: number }> =>
    (await debugFetch(base, '/api/debug/status', signal)).json(),
  contexts: (base: string, query: Record<string, string>, signal?: AbortSignal) => readPage<ContextPage>(base, 'contexts', 'contexts', query, signal),
  requests: (base: string, query: Record<string, string>, signal?: AbortSignal) => readPage<RequestPage>(base, 'requests', 'requests', query, signal),
  request: async (base: string, requestId: string, signal?: AbortSignal): Promise<DebugRequest> => {
    const response = await debugFetch(base, `/api/debug/request?${new URLSearchParams({ request_id: requestId })}`, signal);
    const data = await response.json() as { request?: DebugRequest };
    if (!data.request || data.request.request_id !== requestId || typeof data.request.status !== 'string') throw new Error('来源请求响应格式无效');
    return data.request;
  },
  events: (base: string, query: Record<string, string>, signal?: AbortSignal) => readPage<EventPage>(base, 'events', 'events', query, signal),
  body: async (base: string, query: Record<string, string>, signal?: AbortSignal): Promise<unknown> => {
    const response = await debugFetch(base, `/api/debug/body?${new URLSearchParams(query)}`, signal);
    const data: unknown = await response.json();
    if (!data || typeof data !== 'object' || !('body' in data)) throw new Error('高级 Debug 正文响应格式无效');
    return data.body;
  },
  export: (base: string, requestId: string, signal?: AbortSignal) => debugFetch(base, `/api/debug/export?${new URLSearchParams({ request_id: requestId })}`, signal),
};

export function sameDebugObject(left: DebugObject, right: DebugObject): boolean {
  return left.platform_id === right.platform_id && left.self_id === right.self_id
    && left.conversation_kind === right.conversation_kind && left.target_id === right.target_id;
}

export function debugMode(policy: DebugPolicy, object: DebugObject): DebugMode {
  return policy.objects.find((entry) => sameDebugObject(entry.object, object))?.mode ?? 'inherit';
}

export function withDebugMode(policy: DebugPolicy, object: DebugObject, mode: DebugMode): DebugPolicy {
  const objects = policy.objects.filter((entry) => !sameDebugObject(entry.object, object));
  if (mode !== 'inherit') objects.push({ object, mode });
  return { ...policy, objects };
}
