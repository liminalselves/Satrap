import { create } from 'zustand';
import { backendApi } from '@/api/backend';
import type { BackendHealth } from '@/api/types';

interface BackendState {
  health: BackendHealth | null;
  loading: boolean;
  error: string | null;
  refreshHealth: () => Promise<void>;
  reloadConfig: () => Promise<boolean>;
  shutdown: () => Promise<boolean>;
  setHealth: (health: BackendHealth) => void;
}

export const useBackendStore = create<BackendState>((set, get) => ({
  health: null,
  loading: false,
  error: null,

  refreshHealth: async () => {
    set({ loading: true, error: null });
    try {
      const health = await backendApi.health();
      set({ health, loading: false });
    } catch (e) {
      set({
        health: { running: false, error: e instanceof Error ? e.message : 'Unknown error' },
        loading: false,
        error: e instanceof Error ? e.message : 'Unknown error',
      });
    }
  },

  reloadConfig: async () => {
    try {
      await backendApi.reloadConfig();
      await get().refreshHealth();
      return true;
    } catch {
      return false;
    }
  },

  shutdown: async () => {
    try {
      await backendApi.shutdown();
      await get().refreshHealth();
      return true;
    } catch {
      return false;
    }
  },

  setHealth: (health: BackendHealth) => {
    set({ health });
  },
}));
