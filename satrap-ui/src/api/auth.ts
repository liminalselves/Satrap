import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { setApiSessionState, useApiSessionStore } from '@/stores/useApiSessionStore';

let bootstrapToken: string | null | undefined;
const pendingSessions = new Map<string, Promise<void>>();
export const API_SESSION_TIMEOUT_MS = 8000;

export function requiredApiServices(pathname: string): string[] {
  if (pathname === '/logs' || pathname.startsWith('/logs/')) return [];
  if (pathname === '/chat' || pathname.startsWith('/chat/')) return [getChatApiUrl()];
  const backendPage = !['/models', '/rag'].includes(pathname.replace(/\/$/, ''));
  return [...new Set([getControlApiUrl(), ...(backendPage ? [getApiBaseUrl()] : [])])];
}

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

export async function establishApiSession(baseUrl: string, options: { background?: boolean } = {}): Promise<void> {
  const normalized = baseUrl.replace(/\/$/, '');
  const existing = pendingSessions.get(normalized);
  if (existing) return existing;

  const token = consumeBootstrapToken();
  const request = Promise.resolve().then(async () => {
    const previous = useApiSessionStore.getState().sessions[normalized];
    if (!options.background || previous?.state !== 'ready') setApiSessionState(normalized, { ...previous, state: 'checking' });
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
            const statusResponse = await Promise.race([fetch(`${normalized}/auth/password/status`, { credentials: 'include', signal: controller.signal }), deadline]);
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
      if (!options.background || previous?.state !== 'ready' || (errorKind !== 'network' && errorKind !== 'timeout')) {
        setApiSessionState(normalized, { state: 'error', error: message, errorKind, status: responseStatus });
      }
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

let lastRenewal = Date.now();
export async function renewReadyApiSessions(): Promise<void> {
  if (Date.now() - lastRenewal < 6 * 3600000) return;
  lastRenewal = Date.now();
  const urls = new Set([getApiBaseUrl(), getControlApiUrl(), getChatApiUrl()]);
  await Promise.allSettled([...urls].filter((url) => useApiSessionStore.getState().sessions[url.replace(/\/$/, '')]?.state === 'ready')
    .map((url) => establishApiSession(url, { background: true })));
}

// ---------- 访问密码 ----------

export async function getPasswordStatus(baseUrl: string): Promise<boolean> {
  const normalized = baseUrl.replace(/\/$/, '');
  const response = await fetch(`${normalized}/auth/password/status`, { credentials: 'include' });
  if (!response.ok) throw new Error(`无法读取访问密码状态: HTTP ${response.status}`);
  const data = (await response.json()) as { password_required?: boolean };
  return data.password_required === true;
}

export async function passwordLogin(baseUrl: string, password: string, remember = true): Promise<void> {
  const normalized = baseUrl.replace(/\/$/, '');
  const response = await fetch(`${normalized}/auth/password/login`, {
    method: 'POST', credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password, remember }),
  });
  if (response.status === 401 || response.status === 403) throw new Error('密码不正确');
  if (response.status === 429) throw new Error('尝试过于频繁, 请稍后重试');
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { error?: string } | null;
    throw new Error(body?.error || `登录失败: HTTP ${response.status}`);
  }
  const previous = useApiSessionStore.getState().sessions[normalized];
  setApiSessionState(normalized, { ...previous, state: 'ready', error: undefined, errorKind: undefined, status: undefined });
}

export async function logoutBrowser(): Promise<void> {
  const services = new Map([
    [getApiBaseUrl().replace(/\/$/, ''), '平台后端'],
    [getControlApiUrl().replace(/\/$/, ''), '管理服务'],
    [getChatApiUrl().replace(/\/$/, ''), '聊天服务'],
  ]);
  const sessions = useApiSessionStore.getState().sessions;
  const results = await Promise.allSettled([...services].map(async ([url, name]) => {
    let response: Response;
    try {
      response = await fetch(`${url}/auth/logout`, { method: 'POST', credentials: 'include', signal: AbortSignal.timeout(API_SESSION_TIMEOUT_MS) });
    } catch (error) {
      const unavailable = error instanceof TypeError || (error instanceof DOMException && ['AbortError', 'TimeoutError'].includes(error.name));
      if (unavailable && sessions[url]?.state !== 'ready') return false;
      // 本轮未建立登录的离线服务不阻断注销, 已登录服务仍须确认撤销结果
      throw new Error(`${name}无法连接, 注销未确认`);
    }
    if (!response.ok && response.status !== 401) throw new Error(`${name}注销失败: HTTP ${response.status}`);
    return true;
  }));
  const failures = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected');
  if (failures.length) throw new Error(`部分服务注销未完成: ${failures.map((result) => result.reason instanceof Error ? result.reason.message : '未知错误').join('; ')}`);
  if (!results.some((result) => result.status === 'fulfilled' && result.value)) throw new Error('无法连接任何服务, 注销未确认, 请恢复服务连接后重试');
  window.location.reload();
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
