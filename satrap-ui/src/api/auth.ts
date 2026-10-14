import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { setApiSessionState, useApiSessionStore } from '@/stores/useApiSessionStore';

let bootstrapToken: string | null | undefined;
const pendingSessions = new Map<string, Promise<void>>();
export const API_SESSION_TIMEOUT_MS = 8000;

function consumeBootstrapToken(): string | null {
  if (bootstrapToken !== undefined) return bootstrapToken;
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  bootstrapToken = fragment.get('token');
  if (bootstrapToken) {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  }
  return bootstrapToken;
}

export async function establishApiSession(baseUrl: string): Promise<void> {
  const normalized = baseUrl.replace(/\/$/, '');
  const existing = pendingSessions.get(normalized);
  if (existing) return existing;

  const token = consumeBootstrapToken();
  const request = Promise.resolve().then(async () => {
    setApiSessionState(normalized, { state: 'checking', error: useApiSessionStore.getState().sessions[normalized]?.error });
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let expired = false;
    try {
      const deadline = new Promise<Response>((_resolve, reject) => {
        timer = setTimeout(() => {
          expired = true;
          controller.abort();
          reject(new Error('认证超时'));
        }, API_SESSION_TIMEOUT_MS);
      });
      const response = await Promise.race([fetch(`${normalized}/auth/session`, {
        method: 'POST', credentials: 'include', signal: controller.signal,
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      }), deadline]);
      if (!response.ok) throw new Error(`无法建立 API 会话: HTTP ${response.status}`);
      setApiSessionState(normalized, { state: 'ready' });
    } catch (error) {
      const message = expired ? '认证请求超过 8 秒, 请确认服务运行后重试' : error instanceof Error ? error.message : '无法建立 API 会话';
      setApiSessionState(normalized, { state: 'error', error: message });
      throw new Error(message);
    } finally {
      clearTimeout(timer);
    }
  });
  pendingSessions.set(normalized, request);
  try {
    await request;
  } finally {
    if (pendingSessions.get(normalized) === request) pendingSessions.delete(normalized);
  }
}

export function ensureApiSession(baseUrl: string): Promise<void> {
  const normalized = baseUrl.replace(/\/$/, '');
  const pending = pendingSessions.get(normalized);
  if (pending) return pending;
  if (useApiSessionStore.getState().sessions[normalized]?.state === 'ready') return Promise.resolve();
  return establishApiSession(normalized);
}

export async function establishApiSessions(): Promise<void> {
  const urls = new Set([getApiBaseUrl(), getControlApiUrl(), getChatApiUrl()]);
  await Promise.allSettled([...urls].map((url) => establishApiSession(url)));
}
