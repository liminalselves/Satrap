import { useEffect } from 'react';
import { SatrapWebSocket } from '@/api/websocket';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfigStore } from '@/stores/useConfigStore';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { toast } from '@/components/ui/Toast';

export function useBackendStatusSync() {
  useEffect(() => {
    const socket = new SatrapWebSocket('/ws/status');
    let active = true;
    let configError = '';
    const statusPoll = startVisiblePoll(() => useBackendStore.getState().refreshAll(), {
      interval: () => useBackendStore.getState().statusConnected ? 30000 : 5000,
    });
    const configPoll = startVisiblePoll(async (signal) => {
      await useConfigStore.getState().refreshWatchedConfigs(signal);
      configError = '';
    }, { interval: 30000, onError: (error) => {
      const message = error instanceof Error ? error.message : '无法检查配置变化';
      if (message !== configError) toast('error', '配置刷新失败: ' + message);
      configError = message;
    } });
    socket.connect({
      onStatus: (data) => { if (active) useBackendStore.getState().setHealth(data); },
      onConnect: () => {
        if (!active) return;
        useBackendStore.getState().setStatusConnected(true);
        statusPoll.request();
      },
      onDisconnect: () => {
        if (!active) return;
        useBackendStore.getState().setStatusConnected(false);
        statusPoll.request();
      },
    });
    return () => {
      active = false;
      statusPoll.stop();
      configPoll.stop();
      socket.disconnect();
      useBackendStore.getState().setStatusConnected(false);
    };
  }, []);
}
