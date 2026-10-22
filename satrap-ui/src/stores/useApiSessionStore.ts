import { create } from 'zustand';

export interface ApiSessionState {
  state: 'checking' | 'ready' | 'error' | 'password_required';
  error?: string;
  errorKind?: 'network' | 'timeout' | 'http' | 'unknown';
  status?: number;
}
export const useApiSessionStore = create<{ sessions: Record<string, ApiSessionState> }>(() => ({ sessions: {} }));

export function setApiSessionState(url: string, state: ApiSessionState) {
  useApiSessionStore.setState((previous) => ({ sessions: { ...previous.sessions, [url]: state } }));
}
