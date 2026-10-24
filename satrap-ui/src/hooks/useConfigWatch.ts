import { useEffect } from 'react';
import { useConfigStore, type WatchedConfig } from '@/stores/useConfigStore';
import { isPasswordRequiredError } from '@/api/auth';
import { toast } from '@/components/ui/Toast';

export function useConfigWatch(names: readonly WatchedConfig[]) {
  const key = [...new Set(names)].sort().join(',');
  useEffect(() => {
    const names = key.split(',').filter(Boolean) as WatchedConfig[];
    const store = useConfigStore.getState();
    const unwatch = store.watchConfigs(names);
    const controller = new AbortController();
    Promise.all(names.map((name) => name === 'sessions' ? store.fetchSessionClasses({ signal: controller.signal })
      : name === 'edictum' ? store.fetchEdictumConfigs({ signal: controller.signal }) : store.fetchModels(name, { signal: controller.signal })))
      .catch((error: unknown) => {
        // 密码门是全屏鉴权出口, 鉴权失败不走"读取失败"提示
        if (controller.signal.aborted || isPasswordRequiredError(error)) return;
        toast('error', '读取配置失败: ' + (error instanceof Error ? error.message : String(error)));
      });
    return () => { unwatch(); controller.abort(); };
  }, [key]);
}
