import { useEffect } from 'react';
import { SatrapWebSocket } from '@/api/websocket';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfigStore } from '@/stores/useConfigStore';

export function useBackendStatusSync() {
  useEffect(() => {
    const socket = new SatrapWebSocket('/ws/status');
    let active = true;
    let polling = false;
    const refresh = async () => {
      if (!active || polling) return;
      polling = true;
      await Promise.allSettled([
        useBackendStore.getState().refreshAll(),
        useConfigStore.getState().fetchSessionClasses(),
        useConfigStore.getState().fetchEdictumConfigs(),
      ]);
      polling = false;
    };
    socket.connect({
      onStatus: (data) => { if (active) useBackendStore.getState().setHealth(data); },
      onConnect: () => {
        if (!active) return;
        useBackendStore.getState().setStatusConnected(true);
        void refresh();
      },
      onDisconnect: () => {
        if (!active) return;
        useBackendStore.getState().setStatusConnected(false);
        void refresh();
      },
    });
    void refresh();
    const interval = setInterval(() => void refresh(), 5000);
    const onVisibility = () => { if (!document.hidden) void refresh(); };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      active = false;
      clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibility);
      socket.disconnect();
      useBackendStore.getState().setStatusConnected(false);
    };
  }, []);
}
