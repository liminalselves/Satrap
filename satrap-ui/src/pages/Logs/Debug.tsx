import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowLeft, Ban, Bug, CheckCircle2, ChevronDown, ChevronRight, CircleDashed, Clock, Database, GitBranch, HelpCircle, Loader2,
  MessageSquare, Plus, RefreshCw, Search, Settings2, SkipForward, User, Users, Workflow, XCircle, type LucideIcon,
} from 'lucide-react';
import { PageHeader } from '@/components/common';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { debugLoggingApi, debugMode, withDebugMode, type DebugContext, type DebugEvent, type DebugMode, type DebugObjectSummary, type DebugPolicySnapshot, type DebugRequest } from '@/api/debugLogging';
import { logServices, type LogSource } from '@/api/logging';
import { errorText } from '@/utils/errorText';
import { diagnosticBorders, diagnosticTints, diagnosticTone, eventPresentation, inputDeltaPresentation, requestHint } from './debugPresentation';

const time = (value: number) => new Date(value * 1000).toLocaleString();
const statusNames: Record<string, string> = { running: '执行中', success: '已完成', failed: '失败', timeout: '超时', cancelled: '已取消', skipped: '未执行', unknown: '结果未确认', partial: '部分完成', interrupted: '未确认中断' };
const statusIcons: Record<string, LucideIcon> = { running: Loader2, success: CheckCircle2, failed: XCircle, timeout: Clock, cancelled: Ban, skipped: SkipForward, unknown: HelpCircle, partial: CircleDashed, interrupted: HelpCircle };

/** 上下文负责浏览索引, 对象负责持久开关, 正文只在用户展开时读取 */
export function DebugLogs() {
  const [source, setSource] = useState<LogSource>('backend');
  const base = logServices().find((service) => service.id === source)!.url;
  const [refresh, setRefresh] = useState(0);
  const [settings, setSettings] = useState<DebugPolicySnapshot>();
  const [storageDraft, setStorageDraft] = useState({ retention_days: 7, max_total_mb: 512, max_body_mb: 8, max_request_mb: 32 });
  const [settingsError, setSettingsError] = useState('');
  const [saving, setSaving] = useState(false);
  const [objects, setObjects] = useState<DebugObjectSummary[]>([]);
  const [objectKey, setObjectKey] = useState<string>();
  const [cursor, setCursor] = useState<string | null>();
  const [context, setContext] = useState<DebugContext>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState('');
  const [platformFilter, setPlatformFilter] = useState('');
  const [kindFilter, setKindFilter] = useState('');
  const [recordingFilter, setRecordingFilter] = useState('');
  const recordingRevision = recordingFilter ? settings?.revision : '';
  const contextQuery = { ...(filter ? { q: filter } : {}), ...(platformFilter ? { platform_id: platformFilter } : {}),
    ...(kindFilter ? { context_kind: kindFilter } : {}), ...(recordingFilter ? { recording: recordingFilter } : {}) };
  const [health, setHealth] = useState<Awaited<ReturnType<typeof debugLoggingApi.health>>>();
  const epoch = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    void debugLoggingApi.settings(controller.signal).then((result) => {
      if (!controller.signal.aborted) {
        setSettings(result); setSettingsError('');
        setStorageDraft({ retention_days: result.policy.retention_days, max_total_mb: result.policy.max_total_mb,
          max_body_mb: result.policy.max_body_mb, max_request_mb: result.policy.max_request_mb });
      }
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setSettingsError(errorText(reason)); });
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController();
    setHealth(undefined);
    void debugLoggingApi.health(base, controller.signal).then((result) => {
      if (!controller.signal.aborted) setHealth(result);
    }).catch(() => undefined);
    return () => controller.abort();
  }, [base, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    const current = ++epoch.current;
    setObjects([]); setObjectKey(undefined); setContext(undefined); setCursor(undefined); setError(''); setLoading(true);
    const timer = setTimeout(() => void debugLoggingApi.objects(base, contextQuery, controller.signal).then((page) => {
      if (controller.signal.aborted || current !== epoch.current) return;
      setObjects(page.objects); setCursor(page.has_more ? page.next_cursor : null);
    }).catch((reason: unknown) => { if (!controller.signal.aborted && current === epoch.current) setError(errorText(reason)); })
      .finally(() => { if (current === epoch.current) setLoading(false); }), 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [base, refresh, filter, platformFilter, kindFilter, recordingFilter, recordingRevision]);

  async function moreObjects() {
    if (!cursor || loading) return;
    const current = epoch.current;
    setLoading(true);
    try {
      const page = await debugLoggingApi.objects(base, { ...contextQuery, cursor });
      if (current !== epoch.current) return;
      setObjects((items) => [...items, ...page.objects]); setCursor(page.has_more ? page.next_cursor : null); setError('');
    } catch (reason) { if (current === epoch.current) setError(errorText(reason)); }
    finally { if (current === epoch.current) setLoading(false); }
  }

  async function save(next: DebugPolicySnapshot['policy']) {
    if (!settings || saving) return;
    setSaving(true);
    try { setSettings(await debugLoggingApi.save(next, settings.revision)); setSettingsError(''); }
    catch (reason) { setSettingsError(`${errorText(reason)}; 请刷新设置后重试`); }
    finally { setSaving(false); }
  }

  const storageFault = health && (health.write_error || health.maintenance_error);
  const storageWarning = health && (health.quota_blocked || !!health.dropped_events || !!health.dropped_requests);
  const contextMode = settings && context ? debugMode(settings.policy, context.object) : undefined;
  const contextRecording = contextMode === 'on' || (contextMode === 'inherit' && settings?.policy.default_enabled);

  return <div className="space-y-5">
    <PageHeader title="高级 Debug" description="按群或人归类, 再选择具体会话; 链路默认只查看新增动作, 完整输入可单独核查"
      actions={<>
        <Button size="sm" variant="ghost" onClick={() => setRefresh((value) => value + 1)}><RefreshCw className="h-3.5 w-3.5 mr-1.5 inline" aria-hidden />刷新</Button>
        <Link className="chip tint-accent hover:opacity-80 transition-opacity" to="/logs"><ArrowLeft className="h-3 w-3" aria-hidden />返回运行日志</Link>
      </>} />

    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <Card className="p-5 space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <span className="tint-accent grid h-8 w-8 place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]"><Settings2 className="h-4 w-4" aria-hidden /></span>
          <div className="min-w-0">
            <h2 className="font-semibold">整体默认与存储</h2>
            <p className="text-xs text-text-tertiary">修改从下一次请求生效; 关闭不会删除已有记录</p>
          </div>
          {settings && <label className="ml-auto flex cursor-pointer items-center gap-2 text-sm">
            <input type="checkbox" className="h-4 w-4 accent-[rgb(var(--rgb-accent))]" checked={settings.policy.default_enabled} disabled={saving}
              onChange={(event) => void save({ ...settings.policy, default_enabled: event.target.checked })} />默认记录未设置覆盖的对象</label>}
        </div>
        {settingsError && <p role="alert" className="text-error break-words text-sm">{settingsError}</p>}
        {settings && <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            {([['retention_days', '保留天数'], ['max_total_mb', '总存储目标 (MB)'], ['max_body_mb', '单份正文上限 (MB)'], ['max_request_mb', '单请求正文上限 (MB)']] as const).map(([key, label]) =>
              <label key={key} className="space-y-1 text-xs text-text-secondary">{label}<Input type="number" min="1" disabled={saving} value={storageDraft[key]} className="num"
                onChange={(event) => setStorageDraft({ ...storageDraft, [key]: Number(event.target.value) })} /></label>)}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm" disabled={saving} onClick={() => void save({ ...settings.policy, ...storageDraft })}>保存存储设置</Button>
            <p className="text-xs text-text-tertiary">初始默认关闭, 保留 7 天, 总目标 512 MB。超预算或写入失败会标记记录不完整。</p>
          </div>
        </>}
      </Card>
      <Card className="p-5 space-y-3">
        <div className="flex items-center gap-3">
          <span className="tint-green grid h-8 w-8 place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]"><Database className="h-4 w-4" aria-hidden /></span>
          <div className="min-w-0">
            <h2 className="font-semibold">存储状态</h2>
            <p className="text-xs text-text-tertiary">各来源独立认证和加载</p>
          </div>
          <span className={`chip ml-auto ${storageFault ? 'tint-red' : storageWarning ? 'tint-orange' : health ? 'tint-green' : ''}`}>
            <span className={`dot ${storageFault ? 'tint-red' : storageWarning ? 'tint-orange' : health ? 'tint-green' : ''}`} data-live={health && !storageFault ? '1' : undefined} />
            {storageFault ? '故障' : storageWarning ? '受限' : health ? '正常' : '读取中'}
          </span>
        </div>
        {health ? <dl className="grid grid-cols-2 gap-2 text-xs">
          {([['库', `${(health.database_bytes / 1e6).toFixed(2)} MB`], ['WAL', `${(health.wal_bytes / 1e6).toFixed(2)} MB`],
            ['有效页', `${(health.used_bytes / 1e6).toFixed(2)} MB`], ['待写', String(health.pending_events)]] as const).map(([label, value]) =>
            <div key={label} className="rounded-lg border border-glass-border bg-glass-hover/40 px-3 py-2">
              <dt className="text-text-tertiary">{label}</dt><dd className="num mt-0.5 text-sm font-medium text-text-primary">{value}</dd>
            </div>)}
        </dl> : <div className="grid grid-cols-2 gap-2">{[0, 1, 2, 3].map((index) => <div key={index} className="skeleton h-12 rounded-lg" />)}</div>}
      </Card>
    </div>

    {settings && <AvailableContexts settings={settings} saving={saving} save={save} />}

    <Card className="p-3 flex flex-wrap gap-2 items-center">
      <Select value={source} options={logServices().map((service) => ({ value: service.id, label: service.name }))}
        onChange={(event) => setSource(event.target.value as LogSource)} className="w-44" />
      <div className="relative min-w-0 flex-1 basis-56">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" aria-hidden />
        <Input className="!pl-9" aria-label="搜索上下文或对象" placeholder="搜索上下文、群号或对象" value={filter} onChange={(event) => setFilter(event.target.value)} />
      </div>
      <Input className="w-40" aria-label="平台实例 ID" placeholder="平台实例 ID" value={platformFilter} onChange={(event) => setPlatformFilter(event.target.value)} />
      <Select className="w-36" value={kindFilter} options={[{ value: '', label: '全部上下文' }, { value: 'main', label: '主上下文' }, { value: 'workflow', label: '子工作流' }]} onChange={(event) => setKindFilter(event.target.value)} />
      <Select className="w-36" value={recordingFilter} options={[{ value: '', label: '全部开关状态' }, { value: 'on', label: '当前开启' }, { value: 'off', label: '当前关闭' }]} onChange={(event) => setRecordingFilter(event.target.value)} />
    </Card>
    {error && <p role="alert" className="text-error text-sm">{error}</p>}
    {storageFault && <Card role="alert" variant="error" className={`p-3 text-sm text-error ${diagnosticBorders.error}`}>高级日志存储故障: {health.write_error || health.maintenance_error}</Card>}
    {storageWarning && <p role="status" className="surface tint-orange rounded-xl px-4 py-2.5 text-sm text-warning">
      {health.quota_blocked ? '容量不足, 新请求的高级采集已暂停; 自动清理后恢复。' : ''} 本进程未记录事件 {health.dropped_events ?? 0} 条, 未采集请求 {health.dropped_requests ?? 0} 个。
    </p>}

    <div className="grid lg:grid-cols-[minmax(260px,1fr)_minmax(0,2.2fr)] gap-4">
      <Card className="p-2.5 space-y-1.5 self-start max-h-[75vh] overflow-auto custom-scrollbar lg:sticky lg:top-20">
        <div className="flex items-center justify-between px-2 py-1.5">
          <h2 className="text-sm font-semibold">会话上下文</h2>
          {objects.length > 0 && <span className="chip num">{objects.length}{cursor ? '+' : ''}</span>}
        </div>
        <div className="stagger space-y-1.5">
          {objects.map((item, index) => {
            const open = objectKey === item.object_key;
            const kind = item.object.conversation_kind;
            const KindIcon = kind === 'group' ? Users : kind === 'private' ? User : MessageSquare;
            const tint = kind === 'group' ? 'tint-teal' : kind === 'private' ? 'tint-purple' : 'tint-accent';
            return <div key={item.object_key} data-debug-object={item.object_key} style={{ '--i': Math.min(index, 12) } as CSSProperties}>
              <button type="button" aria-expanded={open} onClick={() => {
                setObjectKey(open ? undefined : item.object_key); setContext(undefined);
              }} className={`${tint} group w-full flex items-start gap-3 p-2.5 text-left rounded-xl border transition-colors break-all ${open ? 'border-[rgb(var(--tint)/0.45)] bg-[rgb(var(--tint)/0.10)]' : 'border-transparent hover:bg-glass-hover'}`}>
                <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]"><KindIcon className="h-4 w-4" aria-hidden /></span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <strong className="truncate text-sm">{kind === 'group' ? '群' : kind === 'private' ? '私聊' : 'Chat'} · {item.object.target_id}</strong>
                    {item.error_count > 0 && <span className="chip tint-red num !px-1.5 !py-0">{item.error_count}</span>}
                    <ChevronRight className={`ml-auto h-4 w-4 shrink-0 text-text-tertiary transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden />
                  </span>
                  <span className="block truncate text-xs text-text-tertiary">{item.object.platform_id} · 账号 {item.object.self_id}</span>
                  <span className="block text-xs text-text-tertiary num">会话 {item.context_count} · 请求 {item.request_count} · 异常 {item.error_count} · {time(item.updated_at)}</span>
                </span>
              </button>
              {open && <ObjectContexts key={`${item.object_key}:${refresh}`} base={base} objectKey={item.object_key}
                query={contextQuery} selected={context?.context_id} select={setContext} />}
            </div>;
          })}
        </div>
        {!loading && !objects.length && !error && <div className="flex flex-col items-center gap-2 px-4 py-8 text-center text-sm text-text-secondary">
          <Bug className="h-8 w-8 text-text-tertiary" aria-hidden />暂无记录。可在对应对象使用 /debug on 开启; 新请求产生后刷新。</div>}
        {loading && <p role="status" className="space-y-1.5 px-1">{[0, 1, 2].map((index) => <span key={index} className="skeleton block h-14 rounded-xl" />)}<span className="sr-only">正在加载…</span></p>}
        {cursor && <Button size="sm" variant="ghost" className="w-full" disabled={loading} onClick={() => void moreObjects()}>加载更多对象</Button>}
      </Card>
      {context ? <div className="space-y-4 min-w-0 animate-rise" key={`${context.object_key}:${context.context_id}`}>
        <Card variant="accent" className="p-5 space-y-3">
          <div className="flex flex-wrap items-start gap-3">
            <div className="min-w-0 flex-1">
              <p className="text-xs text-text-tertiary">{context.context_kind === 'workflow' ? '子工作流' : '会话上下文'}</p>
              <h2 className="mt-0.5 text-lg font-semibold break-all">{context.title || context.context_id || '未绑定上下文'}</h2>
            </div>
            <span className={`chip ${contextRecording ? 'tint-green' : ''}`}><span className={`dot ${contextRecording ? 'tint-green' : ''}`} data-live={contextRecording ? '1' : undefined} />{contextRecording ? '记录中' : '未记录'}</span>
          </div>
          <div className="flex flex-wrap gap-1.5 text-xs">
            {([['平台', context.object.platform_id], ['账号', context.object.self_id], ['类型', context.object.conversation_kind], ['目标', context.object.target_id]] as const).map(([label, value]) =>
              <span key={label} className="chip break-all"><span className="text-text-tertiary">{label}</span>{value}</span>)}
            <span className="sr-only">对象: {context.object.platform_id} / {context.object.self_id} / {context.object.conversation_kind} / {context.object.target_id}</span>
          </div>
          {settings && <div className="flex flex-wrap items-center gap-3 border-t border-glass-border pt-3">
            <span className="text-sm font-medium">对象记录开关</span>
            <Select value={debugMode(settings.policy, context.object)} disabled={saving} className="w-44"
              options={[{ value: 'inherit', label: `继承默认 (${settings.policy.default_enabled ? '开启' : '关闭'})` }, { value: 'on', label: '强制开启' }, { value: 'off', label: '强制关闭' }]}
              onChange={(event) => void save(withDebugMode(settings.policy, context.object, event.target.value as DebugMode))} />
            <p className="text-xs text-text-tertiary basis-full">该开关属于对象, /new 后继续生效; 列表中的不同上下文共用此设置。</p>
          </div>}
        </Card>
        <Requests key={`${base}:${context.object_key}:${context.context_id}:${refresh}`} base={base} context={context} />
      </div> : <Card className="p-10 self-start flex flex-col items-center gap-3 text-center text-text-secondary">
        <span className="tint-accent grid h-12 w-12 place-items-center rounded-2xl bg-[rgb(var(--tint)/0.12)] text-[var(--tint-fg)]"><GitBranch className="h-6 w-6" aria-hidden /></span>
        展开群或人, 再选择具体会话查看新增动作</Card>}
    </div>
  </div>;
}

/**
 * 按所选对象独立分页, 不先取全部上下文再在浏览器分组
 *
 * 参数:
 * - base: 当前日志服务地址
 * - objectKey: 完整对象身份的稳定键
 * - query: 与对象目录相同的筛选条件
 * - selected: 当前选中的上下文 ID
 * - select: 切换具体上下文的回调
 *
 * 返回:
 * - 对象内的会话目录及分页操作
 */
function ObjectContexts({ base, objectKey, query, selected, select }: {
  base: string; objectKey: string; query: Record<string, string>; selected?: string; select: (context: DebugContext) => void;
}) {
  const [items, setItems] = useState<DebugContext[]>([]);
  const [cursor, setCursor] = useState<string | null>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController>();
  const queryKey = JSON.stringify(query);
  useEffect(() => {
    const abort = new AbortController(); controller.current = abort;
    setItems([]); setCursor(null); setError(''); setBusy(true);
    void debugLoggingApi.contexts(base, { ...JSON.parse(queryKey) as Record<string, string>, object_key: objectKey }, abort.signal).then((page) => {
      if (!abort.signal.aborted) { setItems(page.contexts); setCursor(page.has_more ? page.next_cursor : null); }
    }).catch((reason: unknown) => { if (!abort.signal.aborted) setError(errorText(reason)); })
      .finally(() => { if (!abort.signal.aborted) setBusy(false); });
    return () => abort.abort();
  }, [base, objectKey, queryKey]);
  async function more() {
    if (!cursor || busy) return;
    const signal = controller.current?.signal;
    setBusy(true);
    try {
      const page = await debugLoggingApi.contexts(base, { ...query, object_key: objectKey, cursor }, signal);
      if (!signal?.aborted) { setItems((values) => [...values, ...page.contexts]); setCursor(page.has_more ? page.next_cursor : null); setError(''); }
    } catch (reason) { if (!signal?.aborted) setError(errorText(reason)); }
    finally { if (!signal?.aborted) setBusy(false); }
  }
  return <div className="ml-6 pl-3 border-l border-glass-border space-y-1 py-1.5 animate-rise">
    {items.map((item) => {
      const active = selected === item.context_id;
      return <button key={item.context_id} type="button" data-debug-context={item.context_id} onClick={() => select(item)}
        aria-current={active || undefined}
        className={`relative w-full text-left rounded-lg px-2.5 py-2 break-all transition-colors ${active ? 'bg-[rgb(var(--rgb-accent)/0.12)] text-[var(--fg-accent)]' : 'hover:bg-glass-hover'}`}>
        {active && <span aria-hidden className="absolute -left-[13px] top-2 bottom-2 w-[3px] rounded-full bg-[var(--fg-accent)]" />}
        <span className="flex items-center gap-1.5">
          {item.context_kind === 'workflow' && <Workflow className="h-3.5 w-3.5 shrink-0 text-[var(--fg-teal)]" aria-hidden />}
          <strong className="text-sm">{item.title || item.context_id || '未绑定上下文'}</strong>
        </span>
        {item.title && item.title !== item.context_id && <p className="text-xs text-text-tertiary">{item.context_id}</p>}
        <p className="text-xs text-text-tertiary num">{item.context_kind === 'workflow' ? '子工作流 · ' : ''}请求 {item.request_count} · 异常 {item.error_count} · {time(item.updated_at)}</p>
      </button>;
    })}
    {error && <p role="alert" className="text-error text-sm">{error}</p>}
    {busy && <p role="status" className="px-2.5 py-1 text-xs text-text-tertiary"><Loader2 className="mr-1.5 inline h-3 w-3 animate-spin" aria-hidden />正在加载会话…</p>}
    {cursor && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void more()}>加载更多会话</Button>}
  </div>;
}

function AvailableContexts({ settings, saving, save }: { settings: DebugPolicySnapshot; saving: boolean; save: (policy: DebugPolicySnapshot['policy']) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [platform, setPlatform] = useState('chat');
  const [platforms, setPlatforms] = useState(['chat']);
  const [items, setItems] = useState<DebugContext[]>([]);
  const [cursor, setCursor] = useState<string | null>();
  const [selected, setSelected] = useState<DebugContext>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController(), current = ++generation.current;
    setBusy(true); setItems([]); setSelected(undefined); setCursor(null); setError('');
    void debugLoggingApi.available(platform, '', controller.signal).then((page) => {
      if (controller.signal.aborted) return;
      setItems(page.contexts); setPlatforms(page.platforms); setCursor(page.has_more ? page.next_cursor : null);
      if (page.unconfirmed) setError(`本页 ${page.unconfirmed} 项旧记录无法确认对象归属, 已跳过`);
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(errorText(reason)); })
      .finally(() => { if (current === generation.current) setBusy(false); });
    return () => controller.abort();
  }, [platform, open]);
  async function more() {
    if (!cursor || busy) return;
    const current = generation.current; setBusy(true);
    try {
      const page = await debugLoggingApi.available(platform, cursor);
      if (current !== generation.current) return;
      setItems((values) => [...values, ...page.contexts]); setCursor(page.has_more ? page.next_cursor : null);
    } catch (reason) { if (current === generation.current) setError(errorText(reason)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  return <Card className="p-0 overflow-hidden">
    <button type="button" className="tint-purple flex w-full items-center gap-3 px-5 py-3.5 text-left transition-colors hover:bg-glass-hover" aria-expanded={open} onClick={() => setOpen(!open)}>
      <span className="grid h-8 w-8 place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]"><Plus className="h-4 w-4" aria-hidden /></span>
      <span className="min-w-0 flex-1">
        <span className="block font-semibold">为已有上下文开启记录</span>
        <span className="block text-xs text-text-tertiary">从已保存的路由选择对象, 无需先产生日志</span>
      </span>
      <ChevronDown className={`h-4 w-4 text-text-tertiary transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden />
    </button>
    {open && <div className="space-y-3 border-t border-glass-border px-5 py-4 animate-rise">
      <p className="text-sm text-text-secondary">从已保存的路由选择对象, 无需先产生日志或恢复会话。</p>
      <Select className="w-48" value={platform} options={platforms.map((id) => ({ value: id, label: id }))} onChange={(event) => setPlatform(event.target.value)} />
      {error && <p role="status" className="text-warning text-sm">{error}</p>}
      <div className="max-h-60 overflow-auto custom-scrollbar grid gap-1.5 sm:grid-cols-2">{items.map((item) => {
        const active = selected?.object_key === item.object_key && selected.context_id === item.context_id;
        return <button type="button" key={`${item.object_key}:${item.context_id}`} onClick={() => setSelected(item)} aria-pressed={active}
          className={`rounded-lg border px-3 py-2 text-left text-sm break-all transition-colors ${active ? 'border-[rgb(var(--rgb-purple)/0.5)] bg-[rgb(var(--rgb-purple)/0.12)] text-[var(--fg-purple)]' : 'border-glass-border hover:bg-glass-hover'}`}>
          {item.context_id} · {item.object.conversation_kind} · {item.object.target_id}
        </button>;
      })}</div>
      {busy && <p role="status" className="text-sm text-text-tertiary"><Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" aria-hidden />正在读取已有上下文…</p>}
      {!busy && !items.length && !error && <p className="text-sm text-text-secondary">该平台暂无可确认归属的上下文</p>}
      {cursor && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void more()}>加载更多已有上下文</Button>}
      {selected && <div className="flex flex-wrap items-center gap-3 border-t border-glass-border pt-3">
        <span className="break-all text-sm">{selected.object.platform_id} / {selected.object.self_id} / {selected.object.conversation_kind} / {selected.object.target_id}</span>
        <Select className="w-48" disabled={saving} value={debugMode(settings.policy, selected.object)}
          options={[{ value: 'inherit', label: `继承默认 (${settings.policy.default_enabled ? '开启' : '关闭'})` }, { value: 'on', label: '强制开启' }, { value: 'off', label: '强制关闭' }]}
          onChange={(event) => void save(withDebugMode(settings.policy, selected.object, event.target.value as DebugMode))} />
      </div>}
    </div>}
  </Card>;
}

function Requests({ base, context }: { base: string; context: DebugContext }) {
  const [items, setItems] = useState<DebugRequest[]>([]);
  const [cursor, setCursor] = useState<string | null>();
  const [selected, setSelected] = useState<string>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const generation = useRef(0);
  const [status, setStatus] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const updateRequest = useCallback((summary: DebugRequest) => setItems((values) => values.map((item) => item.request_id === summary.request_id ? summary : item)), []);
  const query = { object_key: context.object_key, context_id: context.context_id, ...(status ? { status } : {}),
    ...(from ? { from: String(new Date(from).getTime() / 1000) } : {}), ...(to ? { to: String(new Date(to).getTime() / 1000) } : {}) };
  useEffect(() => {
    const controller = new AbortController(); mounted.current = true;
    const current = ++generation.current;
    setItems([]); setSelected(undefined); setCursor(null); setError(''); setBusy(false);
    void debugLoggingApi.requests(base, query, controller.signal).then((page) => {
      if (!controller.signal.aborted && current === generation.current) { setItems(page.requests); setCursor(page.has_more ? page.next_cursor : null); }
    }).catch((reason: unknown) => { if (!controller.signal.aborted && current === generation.current) setError(errorText(reason)); });
    return () => { mounted.current = false; controller.abort(); };
  }, [base, context.object_key, context.context_id, status, from, to]);
  async function more() {
    if (!cursor || busy) return;
    const current = generation.current;
    setBusy(true);
    try {
      const page = await debugLoggingApi.requests(base, { ...query, cursor });
      if (mounted.current && current === generation.current) { setItems((values) => [...values, ...page.requests]); setCursor(page.has_more ? page.next_cursor : null); setError(''); }
    } catch (reason) { if (mounted.current && current === generation.current) setError(errorText(reason)); }
    finally { if (mounted.current && current === generation.current) setBusy(false); }
  }
  return <div className="space-y-3">
    <div className="flex flex-wrap items-end gap-2">
      <h3 className="mr-auto self-center text-sm font-semibold">请求记录{items.length > 0 && <span className="chip num ml-2">{items.length}{cursor ? '+' : ''}</span>}</h3>
      <Select className="w-36" value={status} options={[{ value: '', label: '全部结果' }, ...Object.entries(statusNames).map(([value, label]) => ({ value, label }))]} onChange={(event) => setStatus(event.target.value)} />
      <label className="text-xs text-text-secondary space-y-1"><span className="block">开始时间</span><Input type="datetime-local" value={from} onChange={(event) => setFrom(event.target.value)} /></label>
      <label className="text-xs text-text-secondary space-y-1"><span className="block">结束时间</span><Input type="datetime-local" value={to} onChange={(event) => setTo(event.target.value)} /></label>
    </div>
    <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-tertiary">
      <span className="sr-only">外框说明: 蓝色正常 · 黄色调用问题或待确认 · 红色系统错误 · 灰色等待、取消或旧记录未分类</span>
      {([['tint-accent', '正常'], ['tint-orange', '调用问题或待确认'], ['tint-red', '系统错误'], ['', '等待、取消或未分类']] as const).map(([tint, label]) =>
        <span key={label} aria-hidden className="inline-flex items-center gap-1.5"><span className={`dot ${tint}`} />{label}</span>)}
    </p>
    {error && <p role="alert" className="text-error text-sm">{error}</p>}
    {!items.length && !error && <Card className="p-8 text-center text-sm text-text-secondary">此上下文暂无请求记录</Card>}
    <div className="stagger space-y-2.5">
      {items.map((item, index) => {
        const tone = diagnosticTone(item.diagnostic_level, item.status, !!item.incomplete), open = selected === item.request_id, hint = requestHint(item);
        const StatusIcon = statusIcons[item.status] ?? CircleDashed;
        return <Card key={item.request_id} data-debug-request={item.request_id} data-diagnostic-tone={tone}
          style={{ '--i': Math.min(index, 12) } as CSSProperties}
          className={`${diagnosticTints[tone]} relative overflow-hidden !rounded-2xl p-0 ${diagnosticBorders[tone]}`}>
          <button type="button" className="w-full text-left flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 transition-colors hover:bg-glass-hover/60"
            onClick={() => setSelected(open ? undefined : item.request_id)}
            aria-label={`${statusNames[item.status] || item.status} ${hint} ${item.request_id}`} aria-expanded={open}>
            <span aria-hidden className={`grid h-7 w-7 shrink-0 place-items-center rounded-lg ${diagnosticTints[tone] ? 'bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]' : 'bg-glass-hover text-text-tertiary'}`}>
              <StatusIcon className={`h-4 w-4 ${item.status === 'running' ? 'animate-spin' : ''}`} />
            </span>
            <strong className="text-sm">{statusNames[item.status] || item.status}</strong>
            {hint && <span className={`chip ${diagnosticTints[tone]}`}>{hint}</span>}
            {!!item.incomplete && <span className="chip tint-orange">记录不完整</span>}
            <span className="ml-auto flex items-center gap-2 text-xs text-text-tertiary num">
              <span>{time(item.started_at)}</span><code className="hidden sm:inline">{item.request_id.slice(0, 12)}</code>
              <ChevronDown aria-hidden className={`h-4 w-4 transition-transform ${open ? 'rotate-180' : ''}`} />
            </span>
          </button>
          {open && <div className="px-4 pb-4"><Timeline base={base} request={item} onState={updateRequest} /></div>}
        </Card>;
      })}
    </div>
    {cursor && <Button variant="ghost" className="w-full" disabled={busy} onClick={() => void more()}>加载更多请求</Button>}
  </div>;
}

function Timeline({ base, request, depth = 0, onState }: { base: string; request: DebugRequest; depth?: number; onState?: (summary: DebugRequest) => void }) {
  const [events, setEvents] = useState<DebugEvent[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [body, setBody] = useState<{ key: string; text: string; summary?: string }>();
  const [openBody, setOpenBody] = useState<string>();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const mounted = useRef(true);
  const controller = useRef<AbortController>();
  useEffect(() => {
    mounted.current = true;
    const abort = new AbortController(); controller.current = abort;
    setEvents([]); setExpanded(new Set()); setBody(undefined); setOpenBody(undefined); setError(''); setBusy(false);
    void debugLoggingApi.events(base, { request_id: request.request_id }, abort.signal).then(async (page) => {
      if (!abort.signal.aborted) { setEvents(page.events); setHasMore(page.has_more); }
      if (!abort.signal.aborted && !page.has_more && request.status === 'running') {
        const summary = await debugLoggingApi.request(base, request.request_id, abort.signal);
        if (!abort.signal.aborted) onState?.(summary);
      }
    }).catch((reason: unknown) => { if (!abort.signal.aborted) setError(errorText(reason)); });
    return () => { mounted.current = false; abort.abort(); };
  }, [base, request.request_id]);
  async function more() {
    if (busy) return;
    setBusy(true);
    const signal = controller.current?.signal;
    try {
      const page = await debugLoggingApi.events(base, { request_id: request.request_id, after: String(events[events.length - 1]?.sequence ?? -1) }, signal);
      if (mounted.current && !signal?.aborted) { setEvents((items) => [...items, ...page.events]); setHasMore(page.has_more); setError(''); }
      if (mounted.current && !signal?.aborted && !page.has_more && request.status === 'running') {
        const summary = await debugLoggingApi.request(base, request.request_id, signal);
        if (mounted.current && !signal?.aborted) onState?.(summary);
      }
    } catch (reason) { if (mounted.current && !signal?.aborted) setError(errorText(reason)); }
    finally { if (mounted.current && !signal?.aborted) setBusy(false); }
  }
  function toggleEvent(eventId: string) {
    setExpanded((current) => {
      const next = new Set(current);
      next.has(eventId) ? next.delete(eventId) : next.add(eventId);
      return next;
    });
    if (openBody?.startsWith(`${eventId}:`)) setOpenBody(undefined);
  }
  async function readBody(event: DebugEvent, slot: string, full = false) {
    const key = `${event.event_id}:${slot}${full ? ':full' : ''}`;
    if (body?.key === key) { setOpenBody(openBody === key ? undefined : key); return; }
    setBusy(true);
    setOpenBody(key);   // 在等待前记录用户选择, 收起操作不会被迟到的响应撤销
    const signal = controller.current?.signal;
    try {
      const value = await debugLoggingApi.body(base, { request_id: request.request_id, event_id: event.event_id, slot }, signal, full);
      if (mounted.current && !signal?.aborted) {
        setBody({ key, ...(!full && event.category === 'model' && slot === 'input' ? inputDeltaPresentation(value) : { text: JSON.stringify(value, null, 2) }) }); setError('');
      }
    } catch (reason) { if (mounted.current && !signal?.aborted) setError(errorText(reason)); }
    finally { if (mounted.current && !signal?.aborted) setBusy(false); }
  }
  async function exportRequest() {
    setBusy(true);
    try {
      const response = await debugLoggingApi.export(base, request.request_id, controller.current?.signal);
      const blob = await response.blob();
      if (!mounted.current) return;
      const url = URL.createObjectURL(blob), anchor = document.createElement('a');
      anchor.href = url; anchor.download = `debug-${request.request_id}.jsonl`; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) { if (mounted.current) setError(errorText(reason)); }
    finally { if (mounted.current) setBusy(false); }
  }
  return <div className="space-y-3 border-t border-glass-border pt-3">
    {request.parent_request_id && <SourceRequest base={base} requestId={request.parent_request_id} depth={depth} />}
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" disabled={!events.length} onClick={() => setExpanded(new Set(events.map((event) => event.event_id)))}>展开全部节点</Button>
      <Button size="sm" disabled={!expanded.size} onClick={() => { setExpanded(new Set()); setOpenBody(undefined); }}>全部收起</Button>
      <Button size="sm" variant="ghost" className="ml-auto" disabled={busy} onClick={() => void exportRequest()}>导出本次链路</Button>
    </div>
    <p className="text-xs text-text-tertiary">本次请求的动作按发生顺序列出。模型输入默认隐藏重复历史; 首次调用从最近一条用户消息开始, 后续调用显示相对上次输入的变更。</p>
    <details className="text-xs text-text-secondary rounded-lg border border-glass-border px-3 py-2"><summary className="cursor-pointer select-none">请求信息</summary>
      <dl className="mt-2 grid gap-1 sm:grid-cols-[auto_minmax(0,1fr)] sm:gap-x-3">
        <dt className="text-text-tertiary">请求 ID</dt><dd className="break-all font-mono">{request.request_id}</dd>
        <dt className="text-text-tertiary">上下文</dt><dd className="break-all font-mono">{request.context_id}</dd>
        <dt className="text-text-tertiary">设置修订</dt><dd className="break-all font-mono">{request.settings_revision}</dd>
      </dl>
    </details>
    {error && <p role="alert" className="text-error text-sm">{error}</p>}
    <ol className="relative space-y-2 pl-6 before:absolute before:left-[9px] before:top-2 before:bottom-2 before:w-px before:bg-[var(--glass-border-strong)]">
      {events.map((event) => {
        const presentation = eventPresentation(event), open = expanded.has(event.event_id), tint = diagnosticTints[presentation.tone];
        return <li key={event.event_id} className="relative">
          <span aria-hidden className={`dot ${tint} absolute -left-[19px] top-[15px]`} />
          <div data-debug-event={event.event_id} data-diagnostic-tone={presentation.tone}
            className={`${tint} rounded-xl px-3 py-2 space-y-2 transition-colors ${open ? 'bg-glass-hover/50' : ''} ${diagnosticBorders[presentation.tone]}`}>
            <button type="button" className="w-full flex flex-wrap items-center gap-x-2.5 gap-y-1 text-left text-sm" aria-expanded={open} onClick={() => toggleEvent(event.event_id)}>
              <ChevronRight aria-hidden className={`h-3.5 w-3.5 shrink-0 text-text-tertiary transition-transform ${open ? 'rotate-90' : ''}`} />
              <span className="num text-xs text-text-tertiary">#{event.sequence}</span>
              <strong className="break-all font-medium">{presentation.title}</strong>
              {!presentation.title.endsWith(presentation.label) && <span className={`chip ${tint}`}>{presentation.label}</span>}
              <time className="text-xs text-text-tertiary ml-auto num">{time(event.recorded_at)}</time>
            </button>
            {open && <div className="space-y-2 pl-6 animate-rise">
              {typeof event.metadata.reason === 'string' && event.metadata.reason && <p className="text-sm break-words">{event.metadata.reason}</p>}
              <details className="text-xs"><summary className="cursor-pointer select-none text-text-tertiary">技术信息</summary>
                <p className="mt-1 break-all">事件 ID: {event.event_id} · {event.category} / {event.phase} / {event.status}</p>
                {event.parent_span_id && <p className="break-all">父步骤: {event.parent_span_id}</p>}
                <pre className="mt-1 whitespace-pre-wrap break-all max-h-48 overflow-auto custom-scrollbar rounded-lg bg-glass-hover/60 p-2.5 font-mono">{JSON.stringify(event.metadata, null, 2)}</pre>
              </details>
              <div className="flex flex-wrap gap-2">{event.body_slots.map((slot) => <Button key={slot} size="sm" disabled={busy}
                aria-expanded={openBody === `${event.event_id}:${slot}`} onClick={() => void readBody(event, slot)}>
                {openBody === `${event.event_id}:${slot}` ? '收起' : '查看'} {event.category === 'model' && slot === 'input' ? '新增输入' : slot}
              </Button>)}{event.category === 'model' && event.phase === 'start' && event.body_slots.includes('input') &&
                <Button size="sm" variant="ghost" disabled={busy} aria-expanded={openBody === `${event.event_id}:input:full`} onClick={() => void readBody(event, 'input', true)}>
                  {openBody === `${event.event_id}:input:full` ? '收起' : '查看'}完整输入
                </Button>}</div>
              {body && openBody === body.key && body.key.startsWith(`${event.event_id}:`) && <div className="animate-rise">
                {body.summary && <p className="chip mb-2 !whitespace-normal">{body.summary}</p>}
                <pre className="text-xs whitespace-pre-wrap break-all max-h-96 overflow-auto custom-scrollbar rounded-xl border border-glass-border bg-glass-hover/60 p-3 font-mono leading-relaxed">{body.text.slice(0, 50000)}</pre>
                {body.text.length > 50000 && <p className="text-xs text-text-tertiary mt-1">页面仅显示前 50000 字符, 完整正文可通过链路导出查看</p>}
              </div>}
            </div>}
          </div>
        </li>;
      })}
    </ol>
    {(hasMore || request.status === 'running') && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void more()}>{hasMore ? '加载更多事件' : '刷新后续事件'}</Button>}
  </div>;
}

function SourceRequest({ base, requestId, depth }: { base: string; requestId: string; depth: number }) {
  const [open, setOpen] = useState(false);
  const [request, setRequest] = useState<DebugRequest>();
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void debugLoggingApi.request(base, requestId, controller.signal).then((item) => {
      if (!controller.signal.aborted) { setRequest(item); setError(''); }
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(errorText(reason)); });
    return () => controller.abort();
  }, [base, requestId, open]);
  return <div className="tint-teal rounded-xl border border-dashed border-[rgb(var(--tint)/0.45)] bg-[rgb(var(--tint)/0.06)] p-3 space-y-2">
    <button type="button" className="flex items-center gap-1.5 text-[var(--tint-fg)] text-sm break-all disabled:opacity-50" disabled={depth >= 8} aria-expanded={open} onClick={() => setOpen(!open)}>
      <GitBranch aria-hidden className="h-3.5 w-3.5 shrink-0" />{open ? '收起' : '展开'}来源链路: {requestId}
    </button>
    {error && <p role="alert" className="text-warning text-sm">来源链路不可用或已清理: {error}</p>}
    {open && request && <>
      <p className="text-xs text-text-secondary">{statusNames[request.status] || request.status} · {time(request.started_at)} · {request.context_id}</p>
      <Timeline base={base} request={request} depth={depth + 1} onState={setRequest} />
    </>}
  </div>;
}
