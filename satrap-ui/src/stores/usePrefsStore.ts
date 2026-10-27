import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export type MotionPreference = 'full' | 'reduced';

interface PrefsState {
  motion: MotionPreference;
  sidebarCollapsed: boolean;
  setMotion: (motion: MotionPreference) => void;
  toggleSidebar: () => void;
}

/** 仅保存在当前浏览器的界面偏好 */
export const usePrefsStore = create<PrefsState>()(persist((set) => ({
  motion: 'full',
  sidebarCollapsed: false,
  setMotion: (motion) => set({ motion }),
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
}), { name: 'satrap.ui.prefs' }));
