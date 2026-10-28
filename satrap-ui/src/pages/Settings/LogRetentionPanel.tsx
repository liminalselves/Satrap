import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Archive, Clock, FolderOpen, RefreshCw, Save, ScrollText, Trash2 } from 'lucide-react';
import { cn } from '@/utils/cn';
import { loggingApi, loggingError, type LogPolicySnapshot } from '@/api/logging';
import { Badge, Button, Field, Input, Skeleton, toast } from '@/components/kit';
import { formatTime } from '@/utils/format';
import { VersionedConfigRead } from '@/api/configVersions';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { Banner, InlineSwitchRow, SectionCard } from './shared';
import { ACTIVATION_REFRESH_MS } from './useActivationLoad';

interface Props {
  i: number;
  active: boolean;
  onDirty: (dirty: boolean) => void;
  register: (actions: { save: () => Promise<boolean>; undo: () => void }) => void;
}

export function LogRetentionPanel({ i, active, onDirty, register }: Props) {
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
  const statusRead = useMemo(() => new VersionedConfigRead([], (signal) => loggingApi.read(signal)), []);
  dirtyRef.current = dirty;
  busyRef.current = saving || cleaning;
  const validDays = /^\d+$/.test(days) && Number(days) >= 1 && Number(days) <= 3650;

  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  const apply = useCallback((value: LogPolicySnapshot) => {
    setBase(value);
    setEnabled(value.policy.enabled);
    setDays(String(value.policy.retention_days));
    dirtyRef.current = false;
  }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (busyRef.current || !active) return true;
    const id = ++requestId.current;
    setLoading(true);
    try {
      const value = await statusRead.read({ signal });
      if (id !== requestId.current || signal?.aborted) return true;
      // 旧版或异常响应不具备策略结构时保持当前状态, 避免滚动切换分节时覆盖草稿
      if (!value || typeof value !== 'object' || !value.policy
        || typeof value.policy.enabled !== 'boolean' || typeof value.policy.retention_days !== 'number'
        || typeof value.revision !== 'string') {
        setLoadError('日志状态响应格式无效');
        return false;
      }
      setSnapshot(value);
      if (!dirtyRef.current) apply(value);
      setLoadError('');
      return true;
    } catch (cause) {
      if (id === requestId.current && !signal?.aborted) setLoadError(loggingError(cause));
      return false;
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [active, apply, statusRead]);

  // 滚动经过分节会反复激活; 刚读取过就不立即重读, 由轮询按间隔刷新
  const loadedAt = useRef(0);
  useEffect(() => {
    if (!active) { setLoading(false); return; }
    const poll = startVisiblePoll(async (signal) => {
      if (!await load(signal)) throw new Error('日志状态读取失败');
      loadedAt.current = Date.now();
    }, { interval: 10000, immediate: Date.now() - loadedAt.current > ACTIVATION_REFRESH_MS });
    return () => { requestId.current++; poll.stop(); };
  }, [active, load]);

  useEffect(() => () => { requestId.current++; }, []);

  const save = async (): Promise<boolean> => {
    if (!base || !validDays) return false;
    requestId.current++;
    setLoading(false);
    setSaving(true);
    busyRef.current = true;
    setError('');
    try {
      const value = await loggingApi.save({ enabled, retention_days: Number(days) }, base.revision);
      setSnapshot(value);
      apply(value);
      toast.success('日志策略已保存, 无需重启后端');
      return true;
    } catch (cause) {
      setError(loggingError(cause));
      return false;
    } finally {
      setSaving(false);
      busyRef.current = false;
    }
  };

  const undo = useCallback(() => {
    if (!base) return;
    setEnabled(base.policy.enabled);
    setDays(String(base.policy.retention_days));
    setError('');
    toast.success('已放弃日志保留的草稿');
  }, [base]);

  useEffect(() => { register({ save, undo }); });

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
      if (value.ok) toast.success(`清理完成, 删除 ${value.result.deleted.length} 个过期日志`);
      else toast.warning(`清理完成, ${value.result.errors.length} 项失败`);
    } catch (cause) {
      setError(loggingError(cause));
    } finally {
      setCleaning(false);
      busyRef.current = false;
    }
  };

  const busy = loading || saving || cleaning;
  const result = snapshot?.last_cleanup;
  const summary = result ? { deleted: result.deleted.length, skipped: result.skipped.length, errors: result.errors.length } : null;

  return (
    <section aria-label="日志保留设置">
      <SectionCard id="logs" i={i} tint="orange" icon={<ScrollText className="h-4 w-4" />}
        title="日志保留" description="按日期保留文件日志, 启动时检查并每小时清理一次。实时日志展示不受影响"
        actions={<>
          <Badge tone="green" dot live>每 10 秒自动刷新</Badge>
          <Button variant="ghost" size="sm" icon={<RefreshCw className={cn('h-4 w-4', loading && 'animate-spin-slow')} />}
            onClick={() => void load()} disabled={busy}>刷新日志状态</Button>
        </>}>

        {loading && !snapshot ? (
          <div className="space-y-3">
            <Skeleton className="h-10 w-2/3" />
            <Skeleton className="h-10 w-1/2" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : (
          <>
            {error && <Banner tone="error" className="mb-4">{error}</Banner>}
            {loadError && <Banner tone="error" className="mb-4">读取日志状态失败: {loadError}</Banner>}

            <div className="grid gap-5 sm:grid-cols-2">
              <div className="rounded-xl border border-line bg-glass px-4">
                <InlineSwitchRow label="自动清理过期日志" hint="关闭后仍可手动清理, 文件继续按日期轮转。">
                  <label className="flex items-center gap-2 text-sm text-fg-2">
                    <input type="checkbox" aria-label="自动清理过期日志" checked={enabled}
                      className="h-4 w-4 accent-[var(--fg-accent)]"
                      onChange={(event) => { dirtyRef.current = true; setEnabled(event.target.checked); }}
                      disabled={!base || saving || cleaning} />
                    启用
                  </label>
                </InlineSwitchRow>
              </div>
              <Field label="保留天数" error={validDays ? undefined : '请输入 1 至 3650 的整数'}
                hint={validDays ? '30 天表示保留今天及前 29 天。包含当天。' : undefined}>
                <Input type="number" min={1} max={3650} step={1} value={days} disabled={!base || saving || cleaning}
                  aria-invalid={!validDays} onChange={(event) => { dirtyRef.current = true; setDays(event.target.value); }} />
              </Field>
            </div>

            <div className="mt-4 grid gap-2 rounded-xl border border-line bg-glass px-3.5 py-3 text-[12px] text-fg-3">
              <span className="flex min-w-0 items-center gap-2"><FolderOpen className="h-3.5 w-3.5 flex-none" />日志目录 <code className="break-all font-mono text-fg-2">{snapshot?.directory || '尚未读取'}</code></span>
              <span className="flex min-w-0 items-center gap-2"><ScrollText className="h-3.5 w-3.5 flex-none" />配置文件 <code className="break-all font-mono text-fg-2">{snapshot?.config_path || '尚未读取'}</code></span>
              <span className="flex items-center gap-2"><Clock className="h-3.5 w-3.5" />策略修订 <code className="font-mono text-fg-2">{base?.revision || snapshot?.revision || '—'}</code></span>
            </div>

            {dirty && (
              <Banner tone="warning" className="mt-4" compact
                actions={<Button size="sm" variant="ghost" onClick={undo} disabled={busy}>放弃草稿</Button>}>
                有未保存的草稿。刷新状态会保留草稿, 请先保存再执行清理。
              </Banner>
            )}
            {base && snapshot && base.revision !== snapshot.revision && (
              <Banner tone="warning" title="已保存策略在其它位置发生变化" className="mt-4"
                actions={<Button size="sm" variant="ghost" onClick={() => { apply(snapshot); toast.success('已载入磁盘上的已保存策略'); }} disabled={busy}>载入已保存策略</Button>}>
                已保存策略在其它位置发生变化, 请重新加载并核对草稿。
              </Banner>
            )}

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Button variant="primary" icon={<Save className="h-4 w-4" />} loading={saving} disabled={!base || !validDays || !dirty || busy}
                onClick={() => void save()}>保存日志策略</Button>
              <Button icon={<Trash2 className="h-4 w-4" />} loading={cleaning} disabled={!base || dirty || invalidDays(days) || busy}
                onClick={() => void cleanup()}>立即清理过期日志</Button>
              {dirty && <Button variant="ghost" onClick={() => { if (snapshot) { apply(snapshot); setError(''); } else undo(); }} disabled={busy}>放弃草稿并加载已保存策略</Button>}
              <span className="ml-auto text-[11.5px] text-fg-4">正在使用的过期文件会暂时保留</span>
            </div>

            {snapshot?.status_error && <p role="alert" className="mt-3 text-sm text-red">清理状态读取失败: {snapshot.status_error}</p>}
            {snapshot?.maintenance_error && (
              <p role="alert" className="mt-3 text-sm text-red">最近维护失败 ({formatTime(snapshot.maintenance_error.created_at)}): {snapshot.maintenance_error.error}</p>
            )}

            <div className="mt-5 border-t border-line pt-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <h3 className="flex items-center gap-2 text-sm font-medium text-fg"><Archive className="h-4 w-4 text-fg-3" />最近清理结果</h3>
                {summary && <Badge tone={summary.errors > 0 ? 'orange' : 'green'} dot>{summary.errors > 0 ? '部分失败' : '清理完成'}</Badge>}
              </div>

              {result ? (
                <div className="space-y-3">
                  <p className="text-[13px] text-fg-2">
                    {formatTime(result.created_at)} · 当次保留 <span className="num">{result.retention_days}</span> 天 ·
                    删除 <span className="num text-green">{result.deleted.length}</span> 项 ·
                    跳过 <span className="num text-orange">{result.skipped.length}</span> 项 ·
                    失败 <span className={cn('num', result.errors.length ? 'text-red' : 'text-fg-2')}>{result.errors.length}</span> 项
                  </p>
                  <p className="text-[12px] text-fg-4">清理日期早于 <span className="font-mono">{result.cutoff}</span> 的日志文件</p>

                  {result.deleted.length > 0 && (
                    <details className="group rounded-xl border border-line bg-glass px-3.5 py-2.5">
                      <summary className="cursor-pointer list-none text-[13px] font-medium text-fg-2 marker:hidden">已删除文件</summary>
                      <p className="num mt-2 text-[11.5px] text-fg-4">共 {result.deleted.length} 个文件</p>
                      <ul className="mt-1 space-y-1 break-all font-mono text-[11.5px] text-fg-3">
                        {result.deleted.map((file) => <li key={file}>{file}</li>)}
                      </ul>
                    </details>
                  )}

                  {result.skipped.length > 0 && (
                    <details className="group rounded-xl border border-[rgb(var(--rgb-orange)/0.25)] bg-[rgb(var(--rgb-orange)/0.06)] px-3.5 py-2.5">
                      <summary className="cursor-pointer list-none text-[13px] font-medium text-fg-2 marker:hidden">跳过的文件及原因</summary>
                      <p className="num mt-2 text-[11.5px] text-orange">共 {result.skipped.length} 个文件</p>
                      <ul className="mt-1 space-y-1.5 text-[12px] text-fg-3">
                        {result.skipped.map((item) => (
                          <li key={item.file} className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-2">
                            <span className="break-all font-mono text-[11.5px] text-fg-2">{item.file}: </span>
                            <span className="text-orange">{item.reason}</span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}

                  {result.errors.length > 0 && (
                    <div className="rounded-xl border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.07)] px-3.5 py-2.5">
                      <p className="text-[13px] font-medium text-red">失败 {result.errors.length} 项</p>
                      <ul className="mt-2 space-y-1.5 text-[12px]">
                        {result.errors.map((item) => (
                          <li key={item.file} className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-2">
                            <span className="break-all font-mono text-[11.5px] text-fg-2">{item.file}: </span>
                            <span className="text-red">{item.reason}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <p className="text-[11.5px] text-fg-4">保存无需重启, 运行中的服务通常在 10 秒内读取新策略。正在使用的过期文件会在下次清理时重试。</p>
                </div>
              ) : (
                <p className="text-sm text-fg-4">尚无清理记录。保存策略后可以立即执行一次清理。</p>
              )}
            </div>
          </>
        )}
      </SectionCard>
    </section>
  );
}

function invalidDays(value: string): boolean {
  return !/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 3650;
}
