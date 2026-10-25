import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader } from '@/components/common';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { debugLoggingApi, debugMode, withDebugMode, type DebugContext, type DebugEvent, type DebugMode, type DebugPolicySnapshot, type DebugRequest } from '@/api/debugLogging';
import { logServices, type LogSource } from '@/api/logging';
import { errorText } from '@/utils/errorText';

const time = (value: number) => new Date(value * 1000).toLocaleString();
const statusNames: Record<string, string> = { running: '执行中', success: '已完成', failed: '失败', timeout: '超时', cancelled: '已取消', skipped: '未执行', unknown: '结果未确认', partial: '部分完成', interrupted: '未确认中断' };

/** 上下文负责浏览索引, 对象负责持久开关, 正文只在用户展开时读取 */
export function DebugLogs() {
  const [source, setSource] = useState<LogSource>('backend');
  const base = logServices().find((service) => service.id === source)!.url;
  const [refresh, setRefresh] = useState(0);
  const [settings, setSettings] = useState<DebugPolicySnapshot>();
  const [storageDraft, setStorageDraft] = useState({ retention_days: 7, max_total_mb: 512, max_body_mb: 8, max_request_mb: 32 });
  const [settingsError, setSettingsError] = useState('');
  const [saving, setSaving] = useState(false);
  const [contexts, setContexts] = useState<DebugContext[]>([]);
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
    setContexts([]); setContext(undefined); setCursor(undefined); setError(''); setLoading(true);
    const timer = setTimeout(() => void debugLoggingApi.contexts(base, contextQuery, controller.signal).then((page) => {
      if (controller.signal.aborted || current !== epoch.current) return;
      setContexts(page.contexts); setCursor(page.has_more ? page.next_cursor : null);
    }).catch((reason: unknown) => { if (!controller.signal.aborted && current === epoch.current) setError(errorText(reason)); })
      .finally(() => { if (current === epoch.current) setLoading(false); }), 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [base, refresh, filter, platformFilter, kindFilter, recordingFilter, recordingRevision]);

  async function moreContexts() {
    if (!cursor || loading) return;
    const current = epoch.current;
    setLoading(true);
    try {
      const page = await debugLoggingApi.contexts(base, { ...contextQuery, cursor });
      if (current !== epoch.current) return;
      setContexts((items) => [...items, ...page.contexts]); setCursor(page.has_more ? page.next_cursor : null); setError('');
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

  return <div className="space-y-5">
    <PageHeader title="高级 Debug" description="按会话上下文查看输入、模型、工具及发送链路; 同一群的所有上下文共用对象开关"
      actions={<Link className="text-accent" to="/logs">返回运行日志</Link>} />
    <Card className="p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-medium">整体默认与存储</h2>
        <Button size="sm" onClick={() => setRefresh((value) => value + 1)}>刷新</Button>
      </div>
      {settingsError && <p role="alert" className="text-error break-words">{settingsError}</p>}
      {settings && <>
        <label className="flex gap-2 items-center"><input type="checkbox" checked={settings.policy.default_enabled} disabled={saving}
          onChange={(event) => void save({ ...settings.policy, default_enabled: event.target.checked })} />默认记录未设置覆盖的对象</label>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {([['retention_days', '保留天数'], ['max_total_mb', '总存储目标 (MB)'], ['max_body_mb', '单份正文上限 (MB)'], ['max_request_mb', '单请求正文上限 (MB)']] as const).map(([key, label]) =>
            <label key={key} className="space-y-1 text-sm">{label}<Input type="number" min="1" disabled={saving} value={storageDraft[key]}
              onChange={(event) => setStorageDraft({ ...storageDraft, [key]: Number(event.target.value) })} /></label>)}
        </div>
        <Button size="sm" disabled={saving} onClick={() => void save({ ...settings.policy, ...storageDraft })}>保存存储设置</Button>
        <p className="text-sm text-text-secondary">初始默认关闭, 保留 7 天, 总目标 512 MB。修改从下一次请求生效; 超预算或写入失败会标记记录不完整。关闭不会删除已有记录。</p>
      </>}
    </Card>
    {settings && <AvailableContexts settings={settings} saving={saving} save={save} />}
    <Card className="p-4 flex flex-wrap gap-3 items-center">
      <Select value={source} options={logServices().map((service) => ({ value: service.id, label: service.name }))}
        onChange={(event) => setSource(event.target.value as LogSource)} className="w-48" />
      <Input className="max-w-xs" aria-label="搜索上下文或对象" placeholder="搜索上下文、群号或对象" value={filter} onChange={(event) => setFilter(event.target.value)} />
      <Input className="max-w-40" aria-label="平台实例 ID" placeholder="平台实例 ID" value={platformFilter} onChange={(event) => setPlatformFilter(event.target.value)} />
      <Select className="w-40" value={kindFilter} options={[{ value: '', label: '全部上下文' }, { value: 'main', label: '主上下文' }, { value: 'workflow', label: '子工作流' }]} onChange={(event) => setKindFilter(event.target.value)} />
      <Select className="w-40" value={recordingFilter} options={[{ value: '', label: '全部开关状态' }, { value: 'on', label: '当前开启' }, { value: 'off', label: '当前关闭' }]} onChange={(event) => setRecordingFilter(event.target.value)} />
      <span className="text-sm text-text-secondary">各来源独立认证和加载</span>
      {health && <span className="text-sm text-text-secondary">库 {(health.database_bytes / 1e6).toFixed(2)} MB · WAL {(health.wal_bytes / 1e6).toFixed(2)} MB · 有效页 {(health.used_bytes / 1e6).toFixed(2)} MB · 待写 {health.pending_events}</span>}
    </Card>
    {error && <p role="alert" className="text-error">{error}</p>}
    {health && (health.write_error || health.maintenance_error) && <p role="alert" className="text-warning">高级日志记录可能不完整: {health.write_error || health.maintenance_error}</p>}
    {health && (health.quota_blocked || !!health.dropped_events || !!health.dropped_requests) && <p role="status" className="text-warning">
      {health.quota_blocked ? '容量不足, 新请求的高级采集已暂停; 自动清理后恢复。' : ''} 本进程未记录事件 {health.dropped_events ?? 0} 条, 未采集请求 {health.dropped_requests ?? 0} 个。
    </p>}
    <div className="grid lg:grid-cols-[minmax(240px,1fr)_minmax(0,2fr)] gap-4">
      <Card className="p-3 space-y-2 self-start max-h-[75vh] overflow-auto">
        <h2 className="font-medium p-2">会话上下文</h2>
        {contexts.map((item) =>
          <button type="button" key={`${item.object_key}:${item.context_id}`} onClick={() => setContext(item)}
            className={`w-full p-3 text-left rounded-lg border break-all ${context === item ? 'border-accent bg-glass-hover' : 'border-glass-border'}`}>
            <strong>{item.title || item.context_id || '未绑定上下文'}</strong>
            <p className="text-sm text-text-secondary">{item.object.platform_id} · {item.object.conversation_kind} · {item.object.target_id}</p>
            {item.context_kind === 'workflow' && <span className="text-xs">子工作流上下文</span>}
            <p className="text-xs text-text-secondary">请求 {item.request_count} · 异常 {item.error_count} · {time(item.updated_at)}</p>
          </button>)}
        {!loading && !contexts.length && !error && <p className="p-2 text-text-secondary">暂无记录。可在对应对象使用 /debug on 开启; 新请求产生后刷新。</p>}
        {loading && <p role="status">正在加载…</p>}
        {cursor && <Button size="sm" disabled={loading} onClick={() => void moreContexts()}>加载更多上下文</Button>}
      </Card>
      {context ? <div className="space-y-4 min-w-0">
        <Card className="p-4 space-y-3">
          <h2 className="font-medium break-all">{context.title || context.context_id || '未绑定上下文'}</h2>
          <p className="text-sm break-all">对象: {context.object.platform_id} / {context.object.self_id} / {context.object.conversation_kind} / {context.object.target_id}</p>
          {settings && <div className="flex flex-wrap items-center gap-3">
            <span>对象记录开关</span>
            <Select value={debugMode(settings.policy, context.object)} disabled={saving} className="w-40"
              options={[{ value: 'inherit', label: `继承默认 (${settings.policy.default_enabled ? '开启' : '关闭'})` }, { value: 'on', label: '强制开启' }, { value: 'off', label: '强制关闭' }]}
              onChange={(event) => void save(withDebugMode(settings.policy, context.object, event.target.value as DebugMode))} />
          </div>}
          <p className="text-sm text-text-secondary">该开关属于对象, /new 后继续生效; 列表中的不同上下文共用此设置。</p>
        </Card>
        <Requests key={`${base}:${context.object_key}:${context.context_id}:${refresh}`} base={base} context={context} />
      </div> : <Card className="p-6 self-start text-text-secondary">选择一个上下文查看请求链路</Card>}
    </div>
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
  return <Card className="p-4 space-y-3">
    <button className="text-accent" aria-expanded={open} onClick={() => setOpen(!open)}>为已有上下文开启记录 {open ? '▴' : '▾'}</button>
    {open && <>
      <p className="text-sm text-text-secondary">从已保存的路由选择对象, 无需先产生日志或恢复会话。</p>
      <Select className="w-48" value={platform} options={platforms.map((id) => ({ value: id, label: id }))} onChange={(event) => setPlatform(event.target.value)} />
      {error && <p role="status" className="text-warning">{error}</p>}
      <div className="max-h-60 overflow-auto space-y-2">{items.map((item) => <button key={`${item.object_key}:${item.context_id}`} className="block text-left text-sm break-all text-accent" onClick={() => setSelected(item)}>
        {item.context_id} · {item.object.conversation_kind} · {item.object.target_id}
      </button>)}</div>
      {busy && <p role="status">正在读取已有上下文…</p>}
      {!busy && !items.length && !error && <p className="text-text-secondary">该平台暂无可确认归属的上下文</p>}
      {cursor && <Button size="sm" disabled={busy} onClick={() => void more()}>加载更多已有上下文</Button>}
      {selected && <div className="flex flex-wrap items-center gap-3 border-t border-glass-border pt-3">
        <span className="break-all">{selected.object.platform_id} / {selected.object.self_id} / {selected.object.conversation_kind} / {selected.object.target_id}</span>
        <Select className="w-48" disabled={saving} value={debugMode(settings.policy, selected.object)}
          options={[{ value: 'inherit', label: `继承默认 (${settings.policy.default_enabled ? '开启' : '关闭'})` }, { value: 'on', label: '强制开启' }, { value: 'off', label: '强制关闭' }]}
          onChange={(event) => void save(withDebugMode(settings.policy, selected.object, event.target.value as DebugMode))} />
      </div>}
    </>}
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
    <div className="flex flex-wrap gap-2 items-end">
      <Select className="w-36" value={status} options={[{ value: '', label: '全部结果' }, ...Object.entries(statusNames).map(([value, label]) => ({ value, label }))]} onChange={(event) => setStatus(event.target.value)} />
      <label className="text-sm">开始时间<Input type="datetime-local" value={from} onChange={(event) => setFrom(event.target.value)} /></label>
      <label className="text-sm">结束时间<Input type="datetime-local" value={to} onChange={(event) => setTo(event.target.value)} /></label>
    </div>
    {error && <p role="alert" className="text-error">{error}</p>}
    {!items.length && !error && <p className="text-text-secondary">此上下文暂无请求记录</p>}
    {items.map((item) => <Card key={item.request_id} className="p-4 space-y-3">
      <button className="w-full text-left break-all" onClick={() => setSelected(selected === item.request_id ? undefined : item.request_id)} aria-expanded={selected === item.request_id}>
        <strong>{statusNames[item.status] || item.status}</strong>{!!item.incomplete && <span className="ml-2 text-warning">记录不完整</span>}
        <p className="text-sm">{time(item.started_at)} · {item.request_id}</p>
        {item.parent_request_id && <p className="text-xs text-text-secondary">来源请求: {item.parent_request_id}</p>}
      </button>
      {selected === item.request_id && <Timeline base={base} request={item} onState={updateRequest} />}
    </Card>)}
    {cursor && <Button disabled={busy} onClick={() => void more()}>加载更多请求</Button>}
  </div>;
}

function Timeline({ base, request, depth = 0, onState }: { base: string; request: DebugRequest; depth?: number; onState?: (summary: DebugRequest) => void }) {
  const [events, setEvents] = useState<DebugEvent[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [body, setBody] = useState<{ key: string; text: string }>();
  const mounted = useRef(true);
  const controller = useRef<AbortController>();
  useEffect(() => {
    mounted.current = true;
    const abort = new AbortController(); controller.current = abort;
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
    try {
      const page = await debugLoggingApi.events(base, { request_id: request.request_id, after: String(events[events.length - 1]?.sequence ?? -1) }, controller.current?.signal);
      if (mounted.current) { setEvents((items) => [...items, ...page.events]); setHasMore(page.has_more); setError(''); }
      if (mounted.current && !page.has_more && request.status === 'running') {
        const summary = await debugLoggingApi.request(base, request.request_id, controller.current?.signal);
        if (mounted.current) onState?.(summary);
      }
    } catch (reason) { if (mounted.current) setError(errorText(reason)); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function readBody(event: DebugEvent, slot: string) {
    const key = `${event.event_id}:${slot}`;
    if (body?.key === key) { setBody(undefined); return; }
    setBusy(true);
    try {
      const value = await debugLoggingApi.body(base, { request_id: request.request_id, event_id: event.event_id, slot }, controller.current?.signal);
      if (mounted.current) { setBody({ key, text: JSON.stringify(value, null, 2) }); setError(''); }
    } catch (reason) { if (mounted.current) setError(errorText(reason)); }
    finally { if (mounted.current) setBusy(false); }
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
    <Button size="sm" disabled={busy} onClick={() => void exportRequest()}>导出本次链路</Button>
    {error && <p role="alert" className="text-error">{error}</p>}
    {events.map((event) => <div key={event.event_id} className="border-l-2 border-accent pl-3 space-y-2">
      <p className="break-all text-sm"><strong>#{event.sequence} {event.category} · {event.phase}</strong> {event.status} · {time(event.recorded_at)}</p>
      {!!event.parent_span_id && <p className="text-xs break-all text-text-secondary">父步骤: {event.parent_span_id}</p>}
      <pre className="text-xs whitespace-pre-wrap break-all max-h-48 overflow-auto">{JSON.stringify(event.metadata, null, 2)}</pre>
      <div className="flex flex-wrap gap-2">{event.body_slots.map((slot) => <Button key={slot} size="sm" disabled={busy} onClick={() => void readBody(event, slot)}>查看 {slot}</Button>)}</div>
      {body && body.key.startsWith(`${event.event_id}:`) && <div>
        <pre className="text-xs whitespace-pre-wrap break-all max-h-96 overflow-auto bg-glass-hover rounded p-3">{body.text.slice(0, 50000)}</pre>
        {body.text.length > 50000 && <p className="text-sm text-text-secondary">页面仅显示前 50000 字符, 完整正文可通过链路导出查看</p>}
      </div>}
    </div>)}
    {(hasMore || request.status === 'running') && <Button size="sm" disabled={busy} onClick={() => void more()}>{hasMore ? '加载更多事件' : '刷新后续事件'}</Button>}
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
  return <div className="border border-glass-border rounded-lg p-3 space-y-2">
    <button className="text-accent text-sm break-all" disabled={depth >= 8} aria-expanded={open} onClick={() => setOpen(!open)}>
      {open ? '收起' : '展开'}来源链路: {requestId}
    </button>
    {error && <p role="alert" className="text-warning">来源链路不可用或已清理: {error}</p>}
    {open && request && <>
      <p className="text-sm">{statusNames[request.status] || request.status} · {time(request.started_at)} · {request.context_id}</p>
      <Timeline base={base} request={request} depth={depth + 1} onState={setRequest} />
    </>}
  </div>;
}
