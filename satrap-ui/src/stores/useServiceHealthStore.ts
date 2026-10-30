import { create } from 'zustand';
import { approvalsApi, healthApi, type Service, type ServiceHealth } from '@/api/contracts';
import { errorText } from '@/utils/errorText';

export interface ServiceSample {
  health: ServiceHealth | null;
  /** 浏览器到该服务的往返耗时 */
  rtt_ms: number | null;
  error: string | null;
  checked_at: number | null;
  checking: boolean;
}

interface ServiceHealthState {
  services: Record<Service, ServiceSample>;
  /** Control 到 Backend 的探测往返耗时 */
  backendProbeMs: number | null;
  pendingApprovals: number | null;
  refresh: (service?: Service) => Promise<void>;
  refreshApprovals: () => Promise<void>;
}

const empty = (): ServiceSample => ({ health: null, rtt_ms: null, error: null, checked_at: null, checking: false });
const ALL: Service[] = ['control', 'backend', 'chat'];

export const useServiceHealthStore = create<ServiceHealthState>((set, get) => ({
  services: { control: empty(), backend: empty(), chat: empty() },
  backendProbeMs: null,
  pendingApprovals: null,
  refresh: async (only) => {
    const targets = only ? [only] : ALL;
    const patch = (service: Service, value: Partial<ServiceSample>) =>
      set((state) => ({ services: { ...state.services, [service]: { ...state.services[service], ...value } } }));
    targets.forEach((service) => patch(service, { checking: true }));
    await Promise.all(targets.map(async (service) => {
      try {
        const sample = await healthApi.sample(service);
        patch(service, { health: sample.health, rtt_ms: Math.round(sample.rtt_ms), error: null, checked_at: Date.now(), checking: false });
      } catch (error) {
        patch(service, { health: null, rtt_ms: null, error: errorText(error, '无法连接'), checked_at: Date.now(), checking: false });
      }
    }));
    if (targets.includes('backend') && get().services.control.health) {
      try {
        const status = await healthApi.probe();
        set({ backendProbeMs: status.probe?.latency_ms ?? null });
      } catch {
        set({ backendProbeMs: null });
      }
    }
  },
  refreshApprovals: async () => {
    try {
      const summary = await approvalsApi.summary();
      set({ pendingApprovals: summary.pending_count });
    } catch {
      set({ pendingApprovals: null });
    }
  },
}));

let consumers = 0;
let timer: number | undefined;

const tick = () => {
  if (document.hidden) return;
  const { refresh, refreshApprovals } = useServiceHealthStore.getState();
  void refresh();
  void refreshApprovals();
};

/** 顶栏共用轮询: 页面可见时每 30 秒刷新一次, 多个组件挂载只启动一套 */
export function startServiceHealthPolling(): () => void {
  consumers += 1;
  if (consumers === 1) {
    tick();
    timer = window.setInterval(tick, 30000);
    document.addEventListener('visibilitychange', tick);
  }
  return () => {
    consumers -= 1;
    if (consumers === 0) {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    }
  };
}
