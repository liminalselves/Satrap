import { Play, Square, RefreshCw, RotateCcw } from 'lucide-react';
import { useBackendStore } from '@/stores/useBackendStore';
import type { BackendOperation } from '@/stores/useBackendStore';
import { Button } from '@/components/ui/Button';
import { toast } from '@/components/ui/Toast';

export function backendStateLabel(state: ReturnType<typeof useBackendStore.getState>): string {
  if (state.operation) return { start: '后端启动中', stop: '后端停止中', restart: '后端重启中' }[state.operation];
  return { running: '后端运行中', stopped: '后端未运行', unknown: '后端状态未知' }[state.runState];
}

export function BackendControls() {
  const state = useBackendStore();
  const busy = state.operation !== null;
  const execute = async (action: BackendOperation) => {
    const result = await state.controlBackend(action);
    toast(result.ok ? 'success' : 'error', result.ok
      ? { start: '后端已启动', stop: '后端已停止', restart: '后端已重启' }[action]
      : result.error || '操作未成功');
  };
  return (
    <div className="flex flex-wrap items-center gap-2" aria-busy={busy}>
      <Button variant="default" onClick={() => void state.refreshAll()} disabled={busy || state.loading}>
        <RefreshCw className="mr-2 h-4 w-4" />刷新状态
      </Button>
      {state.isRunning && <>
        <Button variant="default" disabled={busy} onClick={async () => {
          const ok = await state.reloadConfig();
          toast(ok ? 'success' : 'warning', ok ? '配置已重载' : useBackendStore.getState().lastError || '重载未成功');
        }}><RotateCcw className="mr-2 h-4 w-4" />重载配置</Button>
        <Button variant="default" disabled={busy || !state.controlStatus} onClick={() => void execute('restart')}>
          <RefreshCw className="mr-2 h-4 w-4" />重启后端
        </Button>
      </>}
      <Button
        variant={state.isRunning ? 'danger' : 'primary'}
        disabled={busy || state.runState === 'unknown' || (!state.isRunning && !state.controlStatus)}
        onClick={() => void execute(state.isRunning ? 'stop' : 'start')}
      >
        {state.isRunning ? <Square className="mr-2 h-4 w-4" /> : <Play className="mr-2 h-4 w-4" />}
        {busy ? backendStateLabel(state) : state.isRunning ? '停止后端' : '启动后端'}
      </Button>
    </div>
  );
}
