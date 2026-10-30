import { ApiError } from './client';
import { ensureApiSession, establishApiSession } from './auth';

type Query = Record<string, string | number | boolean | null | undefined>;

export interface ServiceRequest {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  query?: Query;
  body?: unknown;
  raw?: Blob;
  signal?: AbortSignal;
  /** 后台轮询不弹出登录框 */
  background?: boolean;
}

export function queryString(query: Query = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : '';
}

/**
 * 访问任一服务的新契约接口, 各服务沿用自己的会话; 错误保留 {error, code}
 */
export async function serviceFetch<T>(baseUrl: string, path: string, options: ServiceRequest = {}): Promise<T> {
  const { method = 'GET', query, body, raw, signal, background } = options;
  await ensureApiSession(baseUrl);
  signal?.throwIfAborted();
  const send = () => fetch(`${baseUrl}${path}${queryString(query)}`, {
    method,
    credentials: 'include',
    headers: raw ? { 'Content-Type': 'application/octet-stream' } : body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: raw ?? (body === undefined ? undefined : JSON.stringify(body)),
    signal,
  });
  let response = await send();
  // 二进制上传体可重复读取; 非 GET 请求认证失败时请求未被处理, 重放安全
  if (response.status === 401) {
    await establishApiSession(baseUrl, background ? { background: true } : undefined);
    signal?.throwIfAborted();
    response = await send();
  }
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!response.ok) {
    const payload = (data && typeof data === 'object' ? data : {}) as { error?: unknown; code?: unknown; reason?: unknown };
    const message = typeof payload.error === 'string' && payload.error ? payload.error : `请求失败 (HTTP ${response.status})`;
    const code = typeof payload.code === 'string' ? payload.code : typeof payload.reason === 'string' ? payload.reason : undefined;
    throw new ApiError(message, response.status, code);
  }
  return data as T;
}
