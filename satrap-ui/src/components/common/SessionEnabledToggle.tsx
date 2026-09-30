import { useEffect, useRef, useState } from 'react';
import { sessionApi } from '@/api/session';
import { edictumApi } from '@/api/edictum';
import { useBackendStore } from '@/stores/useBackendStore';
import { Button } from '@/components/ui/Button';
import { toast } from '@/components/ui/Toast';

interface SessionEnabledToggleProps {
  provider: 'session_class' | 'edictum';
  name: string;
  enabled: boolean;
  onChanged: () => void | Promise<void>;
}

export function SessionEnabledToggle({ provider, name, enabled, onChanged }: SessionEnabledToggleProps) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [retry, setRetry] = useState(false);
  const [displayEnabled, setDisplayEnabled] = useState(enabled);
  useEffect(() => { setDisplayEnabled(enabled); }, [enabled]);
  const pending = useRef(false);
  const operation = useBackendStore((state) => state.operation);
  const apply = async (save: boolean) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setNote('');
    setRetry(false);
    let saved = !save;
    try {
      if (save) {
        const api = provider === 'edictum' ? edictumApi : sessionApi;
        const result = await (displayEnabled ? api.disable(name) : api.enable(name));
        if (!result.ok) throw new Error(result.error || '配置保存失败');
        saved = true;
        setDisplayEnabled(!displayEnabled);
      }
      const state = useBackendStore.getState();
      if (state.runState === 'unknown') {
        setNote('已保存, 运行状态未知');
        toast('warning', `${name} 配置已保存, 暂无法确认应用状态`);
        setRetry(true);
      } else if (!state.isRunning) {
        setNote('已保存, 后端启动后生效');
        toast('success', `${name} 配置已保存, 后端启动后生效`);
      } else {
        const ok = await state.reloadConfig();
        setNote(ok ? '已保存并应用' : '已保存, 应用未成功');
        setRetry(!ok);
        toast(ok ? 'success' : 'warning', ok ? `${name} 配置已保存并应用`
          : `${name} 配置已保存: ${useBackendStore.getState().lastError || '应用未成功'}`);
      }
    } catch (error) {
      setNote(saved ? '已保存, 应用未成功' : '保存失败');
      setRetry(saved);
      toast('error', error instanceof Error ? error.message : '状态更新失败');
    } finally {
      try { if (saved) await onChanged(); }
      catch { toast('warning', `${name} 配置已保存, 列表刷新失败, 请稍后刷新`); }
      finally { pending.current = false; setBusy(false); }
    }
  };
  return (
    <div className="space-y-1">
      <Button role="switch" aria-checked={displayEnabled} aria-label={`会话配置 ${name}`}
        size="sm" variant={displayEnabled ? 'primary' : 'default'} disabled={busy || operation !== null}
        onClick={() => void apply(true)}>
        {busy ? '处理中...' : displayEnabled ? '已启用' : '已禁用'}
      </Button>
      {note && <p className="text-xs text-text-secondary" role="status">{note}</p>}
      {retry && <Button size="sm" variant="ghost" disabled={busy || operation !== null}
        onClick={() => void apply(false)}>重试应用</Button>}
    </div>
  );
}
