import { getApiBaseUrl, getChatApiUrl, getControlApiUrl } from '@/utils/constants';

let bootstrapToken: string | null | undefined;
const pendingSessions = new Map<string, Promise<void>>();

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

  const request = (async () => {
    const token = consumeBootstrapToken();
    const response = await fetch(`${normalized}/auth/session`, {
      method: 'POST',
      credentials: 'include',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    if (!response.ok) {
      throw new Error(`无法建立 API 会话: HTTP ${response.status}`);
    }
  })();
  pendingSessions.set(normalized, request);
  try {
    await request;
  } finally {
    pendingSessions.delete(normalized);
  }
}

export async function establishApiSessions(): Promise<void> {
  const urls = new Set([getApiBaseUrl(), getControlApiUrl(), getChatApiUrl()]);
  await Promise.allSettled([...urls].map((url) => establishApiSession(url)));
}
