import { create } from 'zustand';
import { backendApi } from '@/api/backend';
import { controlApi } from '@/api/control';
import type { BackendStatus, ControlResult } from '@/api/control';
import type { BackendHealth } from '@/api/types';

export type BackendOperation = 'start' | 'stop' | 'restart';
export type BackendRunState = 'running' | 'stopped' | 'unknown';

interface BackendState {
  health: BackendHealth | null;
  healthAvailable: boolean;
  controlStatus: BackendStatus | null;
  loading: boolean;
  isRunning: boolean;
  runState: BackendRunState;
  operation: BackendOperation | null;
  statusConnected: boolean;
  lastError: string | null;
  refreshHealth: () => Promise<void>;
  refreshControlStatus: () => Promise<void>;
  refreshAll: () => Promise<void>;
  reloadConfig: () => Promise<boolean>;
  controlBackend: (operation: BackendOperation) => Promise<ControlResult>;
  shutdown: () => Promise<boolean>;
  setHealth: (health: BackendHealth) => void;
  setStatusConnected: (connected: boolean) => void;
}

function runtimeState(control: BackendStatus | null, health: BackendHealth | null, available: boolean) {
  const running = control?.running ?? (available ? health?.running : undefined);
  return {
    isRunning: running === true,
    runState: running === undefined ? 'unknown' as const : running ? 'running' as const : 'stopped' as const,
  };
}

export const useBackendStore = create<BackendState>((set, get) => {
  let healthRequest = 0;
  let controlRequest = 0;
  // 请求代次在新快照和启停操作时递增, 旧响应不能恢复已停止的状态

  return {
    health: null,
    healthAvailable: false,
    controlStatus: null,
    loading: false,
    isRunning: false,
    runState: 'unknown',
    operation: null,
    statusConnected: false,
    lastError: null,

    refreshHealth: async () => {
      const request = ++healthRequest;
      set({ loading: true });
      try {
        const health = await backendApi.health();
        if (request !== healthRequest) return;
        set({ health, healthAvailable: true, loading: false, ...runtimeState(get().controlStatus, health, true) });
      } catch (error) {
        if (request !== healthRequest) return;
        const health = { running: false, error: error instanceof Error ? error.message : '无法读取后端状态' };
        set({ health, healthAvailable: false, loading: false, ...runtimeState(get().controlStatus, health, false) });
      }
    },

    refreshControlStatus: async () => {
      const request = ++controlRequest;
      try {
        const controlStatus = await controlApi.status();
        if (request !== controlRequest) return;
        if (!controlStatus.running) ++healthRequest;
        const previousHealth = get().health;
        const health = !controlStatus.running && previousHealth
          ? { ...previousHealth, running: false, adapters: {} }
          : previousHealth;
        set({ controlStatus, health, ...(!controlStatus.running ? { loading: false } : {}),
          ...runtimeState(controlStatus, health, get().healthAvailable) });
      } catch {
        if (request !== controlRequest) return;
        set({ controlStatus: null, ...runtimeState(null, get().health, get().healthAvailable) });
      }
    },

    refreshAll: async () => {
      await Promise.all([get().refreshHealth(), get().refreshControlStatus()]);
    },

    reloadConfig: async () => {
      try {
        const result = await backendApi.reloadConfig();
        const failures = [
          ...(result.platforms || []).filter((item) => item.status !== 'applied').map((item) => item.error || `${item.id}: 待应用`),
          ...(result.edictum_sessions || []).filter((item) => !item.ok || item.restart_required).map((item) => item.error || `${item.session_id}: 待应用`),
        ];
        const ok = result.ok && failures.length === 0;
        set({ lastError: ok ? null : failures.join('; ') || '配置重载未成功' });
        await get().refreshAll();
        return ok;
      } catch (error) {
        set({ lastError: error instanceof Error ? error.message : '配置重载失败' });
        return false;
      }
    },

    controlBackend: async (operation) => {
      if (get().operation) return { ok: false, error: '后端正在执行启停操作' };
      const previous = get();
      ++healthRequest;
      ++controlRequest;
      set({ operation, loading: false, lastError: null });
      try {
        let result: ControlResult;
        try {
          result = await controlApi[operation]();
        } catch (error) {
          if (operation !== 'stop' || previous.controlStatus?.managed || !previous.healthAvailable) throw error;
          result = await backendApi.shutdown();
        }
        if (!result.ok) throw new Error(result.error || '后端控制请求失败');
        ++healthRequest;
        ++controlRequest;
        set({ health: null, healthAvailable: false, controlStatus: null, loading: false, isRunning: false, runState: 'unknown' });
        const deadline = Date.now() + 30000;
        do {
          await get().refreshAll();
          const current = get();
          const ready = operation === 'stop'
            ? current.runState === 'stopped'
            : current.isRunning && current.healthAvailable && current.health?.running
              && (operation !== 'restart' || !previous.health?.runtime_id || current.health.runtime_id !== previous.health.runtime_id);
          if (ready) return { ok: true };
          await new Promise((resolve) => setTimeout(resolve, 500));
        } while (Date.now() < deadline);
        throw new Error('请求已发送, 但未在 30 秒内确认后端状态, 请刷新后检查');
      } catch (error) {
        const message = error instanceof Error ? error.message : '无法连接控制服务';
        set({ lastError: message });
        return { ok: false, error: message };
      } finally {
        set({ operation: null });
      }
    },

    shutdown: async () => (await get().controlBackend('stop')).ok,

    setHealth: (patch) => {
      ++healthRequest;
      if (get().controlStatus?.running === false && patch.running) return;
      const previous = get().health;
      const health = {
        ...(previous?.runtime_id && patch.runtime_id && previous.runtime_id !== patch.runtime_id ? {} : previous),
        ...patch,
      };
      set({ health, healthAvailable: true, loading: false, ...runtimeState(get().controlStatus, health, true) });
    },

    setStatusConnected: (statusConnected) => set({ statusConnected }),
  };
});
