import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, Save, Trash2 } from 'lucide-react';
import { loggingApi, loggingError, type LogPolicySnapshot } from '@/api/logging';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { toast } from '@/components/ui/Toast';
import { formatTime } from '@/utils/format';

export function LogRetentionPanel({ active }: { active: boolean }) {
  const [snapshot, setSnapshot] = useState<LogPolicySnapshot | null>(null);
  const [base, setBase] = useState<LogPolicySnapshot | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [days, setDays] = useState('30');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const dirty = base !== null && (enabled !== base.policy.enabled || days !== String(base.policy.retention_days));
  const dirtyRef = useRef(dirty);
  const requestId = useRef(0);
  const busyRef = useRef(false);
  dirtyRef.current = dirty;
  busyRef.current = saving || cleaning;
  const validDays = /^\d+$/.test(days) && Number(days) >= 1 && Number(days) <= 3650;

  const apply = useCallback((value: LogPolicySnapshot) => {
    setBase(value);
    setEnabled(value.policy.enabled);
    setDays(String(value.policy.retention_days));
    dirtyRef.current = false;
  }, []);

  const load = useCallback(async () => {
    if (busyRef.current) return;
    const id = ++requestId.current;
    setLoading(true);
    try {
      const value = await loggingApi.read();
      if (id !== requestId.current) return;
      setSnapshot(value);
      if (!dirtyRef.current) apply(value);
      setLoadError('');
    } catch (cause) {
      if (id === requestId.current) setLoadError(loggingError(cause));
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [apply]);

  useEffect(() => {
    if (!active) return;
    void load();
    const timer = window.setInterval(() => { void load(); }, 10000);
    return () => { window.clearInterval(timer); };
  }, [active, load]);

  useEffect(() => () => { requestId.current++; }, []);

  const save = async () => {
    if (!base || !validDays) return;
    requestId.current++;
    setLoading(false);
    setSaving(true);
    busyRef.current = true;
    setError('');
    try {
      const value = await loggingApi.save({ enabled, retention_days: Number(days) }, base.revision);
      setSnapshot(value);
      apply(value);
      toast('success', '日志策略已保存, 无需重启后端');
    } catch (cause) {
      setError(loggingError(cause));
    } finally {
      setSaving(false);
      busyRef.current = false;
    }
  };

  const cleanup = async () => {
    if (!base || dirty) return;
    requestId.current++;
    setLoading(false);
    setCleaning(true);
    busyRef.current = true;
    setError('');
    try {
      const value = await loggingApi.cleanup(base.revision);
      setSnapshot((current) => current ? { ...current, last_cleanup: value.result, maintenance_error: null } : current);
      toast(value.ok ? 'success' : 'warning', value.ok ? `清理完成, 删除 ${value.result.deleted.length} 个过期日志` : `清理完成, ${value.result.errors.length} 项失败`);
    } catch (cause) {
      setError(loggingError(cause));
    } finally {
      setCleaning(false);
      busyRef.current = false;
    }
  };

  const busy = loading || saving || cleaning;
  const result = snapshot?.last_cleanup;
  return (
    <section aria-label="日志保留设置" className="space-y-5 pt-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-medium text-text-primary">日志保留</h2>
          <p className="text-sm text-text-secondary mt-1">按日期保留文件日志, 启动时检查并每小时清理一次。实时日志展示不受影响。</p>
        </div>
        <Button variant="ghost" onClick={() => void load()} disabled={busy}>
          <RefreshCw className="h-4 w-4 mr-2" />刷新日志状态
        </Button>
      </div>
      {error && <div role="alert" className="rounded-sm border border-error/30 bg-error/10 p-3 text-sm text-error">{error}</div>}
      {loadError && <div role="alert" className="rounded-sm border border-error/30 bg-error/10 p-3 text-sm text-error">读取日志状态失败: {loadError}</div>}
      <div className="grid gap-5 sm:grid-cols-2">
        <div>
          <label className="flex items-center gap-2 text-sm text-text-primary">
            <input type="checkbox" aria-label="自动清理过期日志" checked={enabled} onChange={(event) => { dirtyRef.current = true; setEnabled(event.target.checked); }} disabled={!base || saving || cleaning} />
            自动清理过期日志
          </label>
          <p className="mt-2 text-sm text-text-tertiary">关闭后仍可手动清理, 文件继续按日期轮转。</p>
        </div>
        <div>
          <label htmlFor="log-retention-days" className="block mb-1 text-sm text-text-secondary">保留天数</label>
          <Input id="log-retention-days" type="number" min={1} max={3650} step={1} value={days} onChange={(event) => { dirtyRef.current = true; setDays(event.target.value); }} disabled={!base || saving || cleaning} aria-invalid={!validDays} aria-describedby="log-retention-help" />
          <p id="log-retention-help" className={`mt-1 text-sm ${validDays ? 'text-text-tertiary' : 'text-error'}`}>
            {validDays ? '1 至 3650 天, 包含当天。30 天表示保留今天及前 29 天。' : '请输入 1 至 3650 的整数'}
          </p>
        </div>
      </div>
      <div className="rounded-sm bg-glass p-3 text-sm space-y-2 break-all">
        <p><span className="text-text-tertiary">日志目录: </span>{snapshot?.directory || '尚未读取'}</p>
        <p><span className="text-text-tertiary">配置文件: </span>{snapshot?.config_path || '尚未读取'}</p>
      </div>
      {dirty && <p className="text-sm text-warning">有未保存的草稿, 刷新状态会保留草稿。请先保存再执行清理。</p>}
      {base && snapshot && base.revision !== snapshot.revision && <p className="text-sm text-warning">已保存策略在其它位置发生变化, 请重新加载并核对草稿。</p>}
      <div className="flex flex-wrap gap-3">
        <Button variant="primary" onClick={() => void save()} disabled={!base || !validDays || !dirty || busy}>
          <Save className="h-4 w-4 mr-2" />{saving ? '保存中…' : '保存日志策略'}
        </Button>
        <Button onClick={() => void cleanup()} disabled={!base || dirty || busy}>
          <Trash2 className="h-4 w-4 mr-2" />{cleaning ? '清理中…' : '立即清理过期日志'}
        </Button>
        {dirty && snapshot && <Button variant="ghost" onClick={() => { apply(snapshot); setError(''); }} disabled={busy}>放弃草稿并加载已保存策略</Button>}
      </div>
      <p className="text-sm text-text-tertiary">保存无需重启, 运行中的服务通常在 10 秒内读取新策略。正在使用的过期文件会暂时保留。</p>
      {snapshot?.status_error && <p role="alert" className="text-sm text-error">清理状态读取失败: {snapshot.status_error}</p>}
      {snapshot?.maintenance_error && <p role="alert" className="text-sm text-error">最近维护失败 ({formatTime(snapshot.maintenance_error.created_at)}): {snapshot.maintenance_error.error}</p>}
      <div className="border-t border-border-glass pt-4 space-y-3">
        <h3 className="font-medium text-text-primary">最近清理结果</h3>
        {result ? <>
          <p className="text-sm text-text-secondary">{formatTime(result.created_at)} · 当次保留 {result.retention_days} 天 · 删除 {result.deleted.length} 项 · 跳过 {result.skipped.length} 项 · 失败 {result.errors.length} 项</p>
          <p className="text-sm text-text-tertiary">清理日期早于 {result.cutoff} 的日志文件</p>
          {result.deleted.length > 0 && <details className="text-sm text-text-secondary"><summary>已删除文件</summary><ul className="mt-2 space-y-1 break-all">{result.deleted.map((file) => <li key={file}>{file}</li>)}</ul></details>}
          {result.skipped.length > 0 && <details className="text-sm text-text-secondary"><summary>跳过的文件及原因</summary><ul className="mt-2 space-y-1 break-all">{result.skipped.map((item) => <li key={item.file}>{item.file}: {item.reason}</li>)}</ul></details>}
          {result.errors.length > 0 && <ul className="text-sm text-error space-y-1 break-all">{result.errors.map((item) => <li key={item.file}>{item.file}: {item.reason}</li>)}</ul>}
        </> : <p className="text-sm text-text-tertiary">尚无清理记录</p>}
      </div>
    </section>
  );
}
