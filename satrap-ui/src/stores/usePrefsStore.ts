import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type MotionPreference = 'full' | 'reduced';

interface PrefsState {
  motion: MotionPreference;
  sidebarCollapsed: boolean;
  /** 亚克力 (backdrop-filter) 效果; null 表示按设备自动决定 */
  glass: boolean | null;
  /** 动态背景色; null 表示按设备自动决定 */
  ambient: boolean | null;
  setMotion: (motion: MotionPreference) => void;
  toggleSidebar: () => void;
  setGlass: (value: boolean | null) => void;
  setAmbient: (value: boolean | null) => void;
}

/** 触屏、窄屏或核心/内存较少的设备视为低性能, 默认关闭亚克力与动态背景 */
export function isLowPowerDevice(): boolean {
  if (typeof window === 'undefined') return false;
  const nav = navigator as Navigator & { deviceMemory?: number };
  const coarse = window.matchMedia?.('(pointer: coarse)').matches ?? false;
  const narrow = window.innerWidth < 768;
  const fewCores = (nav.hardwareConcurrency ?? 8) <= 4;
  const lowMemory = (nav.deviceMemory ?? 8) <= 4;
  return coarse || narrow || fewCores || lowMemory;
}

/** 仅保存在当前浏览器的界面偏好 */
export const usePrefsStore = create<PrefsState>()(persist((set) => ({
  motion: 'full',
  sidebarCollapsed: false,
  glass: null,
  ambient: null,
  setMotion: (motion) => set({ motion }),
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
  setGlass: (glass) => set({ glass }),
  setAmbient: (ambient) => set({ ambient }),
}), { name: 'satrap.ui.prefs' }));

/** 实际生效的视觉效果: 用户未设置时取设备默认值 */
export function useVisualEffects(): { glass: boolean; ambient: boolean; lowPower: boolean } {
  const glass = usePrefsStore((state) => state.glass);
  const ambient = usePrefsStore((state) => state.ambient);
  const lowPower = isLowPowerDevice();
  return { glass: glass ?? !lowPower, ambient: ambient ?? !lowPower, lowPower };
}
