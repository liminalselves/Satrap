import { create } from 'zustand';
import { backendApi } from '@/api/backend';
import { controlApi, BackendStatus } from '@/api/control';
import type { BackendHealth } from '@/api/types';

interface BackendState {
  health: BackendHealth | null;
  controlStatus: BackendStatus | null;
  loading: boolean;
  error: string | null;
  // 统一的后端运行状态: 优先使用控制服务状态, 其次是 health 状态
  isRunning: boolean;
  refreshHealth: () => Promise<void>;
  refreshControlStatus: () => Promise<void>;
  refreshAll: () => Promise<void>;
  reloadConfig: () => Promise<boolean>;
  shutdown: () => Promise<boolean>;
  setHealth: (health: BackendHealth) => void;
  setControlStatus: (status: BackendStatus | null) => void;
}

export const useBackendStore = create<BackendState>((set, get) => ({
  health: null,
  controlStatus: null,
  loading: false,
  error: null,
  isRunning: false,

  refreshHealth: async () => {
    set({ loading: true, error: null });
    try {
      const health = await backendApi.health();
      set({ 
        health, 
        loading: false,
        isRunning: health.running || get().controlStatus?.running || false,
      });
    } catch (e) {
      const controlRunning = get().controlStatus?.running || false;
      set({
        health: { running: false, error: e instanceof Error ? e.message : 'Unknown error' },
        loading: false,
        error: e instanceof Error ? e.message : 'Unknown error',
        isRunning: controlRunning,
      });
    }
  },

  refreshControlStatus: async () => {
    try {
      const status = await controlApi.status();
      set({ 
        controlStatus: status,
        isRunning: status.running || get().health?.running || false,
      });
    } catch {
      set({ 
        controlStatus: null,
        isRunning: get().health?.running || false,
      });
    }
  },

  refreshAll: async () => {
    await Promise.all([
      get().refreshHealth(),
      get().refreshControlStatus(),
    ]);
  },

  reloadConfig: async () => {
    try {
      const result = await backendApi.reloadConfig();
      await get().refreshHealth();
      return result.ok;
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
    set({ 
      health,
      isRunning: health.running || get().controlStatus?.running || false,
    });
  },

  setControlStatus: (status: BackendStatus | null) => {
    set({ 
      controlStatus: status,
      isRunning: status?.running || get().health?.running || false,
    });
  },
}));
