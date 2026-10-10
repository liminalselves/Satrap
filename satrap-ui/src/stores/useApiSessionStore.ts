import { create } from 'zustand';

export interface ApiSessionState { state: 'checking' | 'ready' | 'error'; error?: string }
export const useApiSessionStore = create<{ sessions: Record<string, ApiSessionState> }>(() => ({ sessions: {} }));

export function setApiSessionState(url: string, state: ApiSessionState) {
  useApiSessionStore.setState((previous) => ({ sessions: { ...previous.sessions, [url]: state } }));
}
