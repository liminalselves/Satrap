import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { setApiSessionState, useApiSessionStore } from '@/stores/useApiSessionStore';

let bootstrapToken: string | null | undefined;
const pendingSessions = new Map<string, Promise<void>>();
export const API_SESSION_TIMEOUT_MS = 8000;

export class PasswordRequiredError extends Error {
  constructor() {
    super('需要访问密码');
    this.name = 'PasswordRequiredError';
  }
}

export function isPasswordRequiredError(error: unknown): boolean {
  return error instanceof PasswordRequiredError;
}

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
    const previous = useApiSessionStore.getState().sessions[normalized];
    setApiSessionState(normalized, { ...previous, state: 'checking' });
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let expired = false;
    let responseStatus: number | undefined;
    let passwordRequired = false;
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
      responseStatus = response.status;
      if (!response.ok) {
        if (responseStatus === 401 || responseStatus === 403) {
          try {
            const statusResponse = await fetch(`${normalized}/auth/password/status`, { credentials: 'include' });
            if (statusResponse.ok) {
              const data = (await statusResponse.json()) as { password_required?: boolean };
              if (data.password_required === true) {
                passwordRequired = true;
                setApiSessionState(normalized, { state: 'password_required' });
                throw new PasswordRequiredError();
              }
            }
          } catch (error) {
            if (isPasswordRequiredError(error)) throw error;
            // 状态探测失败按普通认证错误处理
          }
        }
        throw new Error(`无法建立 API 会话: HTTP ${response.status}`);
      }
      setApiSessionState(normalized, { state: 'ready' });
    } catch (error) {
      if (passwordRequired) throw error;
      const errorKind = expired ? 'timeout' : responseStatus !== undefined ? 'http' : error instanceof TypeError ? 'network' : 'unknown';
      const message = expired ? '连接请求超过 8 秒, 请确认服务运行后重试'
        : errorKind === 'network' ? '无法连接服务, 请检查服务是否已启动及连接地址'
        : error instanceof Error ? error.message : '无法建立 API 会话';
      setApiSessionState(normalized, { state: 'error', error: message, errorKind, status: responseStatus });
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
  const state = useApiSessionStore.getState().sessions[normalized]?.state;
  if (state === 'ready') return Promise.resolve();
  if (state === 'password_required') return Promise.reject(new PasswordRequiredError());
  return establishApiSession(normalized);
}

export async function establishApiSessions(): Promise<void> {
  const urls = new Set([getApiBaseUrl(), getControlApiUrl(), getChatApiUrl()]);
  await Promise.allSettled([...urls].map((url) => establishApiSession(url)));
}

// ---------- 访问密码 ----------

export async function getPasswordStatus(baseUrl: string): Promise<boolean> {
  const normalized = baseUrl.replace(/\/$/, '');
  const response = await fetch(`${normalized}/auth/password/status`, { credentials: 'include' });
  if (!response.ok) throw new Error(`无法读取访问密码状态: HTTP ${response.status}`);
  const data = (await response.json()) as { password_required?: boolean };
  return data.password_required === true;
}

export async function passwordLogin(baseUrl: string, password: string): Promise<void> {
  const normalized = baseUrl.replace(/\/$/, '');
  const response = await fetch(`${normalized}/auth/password/login`, {
    method: 'POST', credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (response.status === 401 || response.status === 403) throw new Error('密码不正确');
  if (response.status === 429) throw new Error('尝试过于频繁, 请稍后重试');
  if (!response.ok) throw new Error(`登录失败: HTTP ${response.status}`);
  const previous = useApiSessionStore.getState().sessions[normalized];
  setApiSessionState(normalized, { ...previous, state: 'ready', error: undefined, errorKind: undefined, status: undefined });
}

export async function setDashboardPassword(baseUrl: string, currentPassword: string | null, newPassword: string | null): Promise<boolean> {
  const normalized = baseUrl.replace(/\/$/, '');
  const response = await fetch(`${normalized}/auth/password`, {
    method: 'PUT', credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (response.status === 403) throw new Error('当前密码不正确');
  if (!response.ok) {
    const data = await response.json().catch(() => null) as { error?: string } | null;
    throw new Error(data?.error || `保存失败: HTTP ${response.status}`);
  }
  const data = (await response.json()) as { password_required?: boolean };
  return data.password_required === true;
}
