import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowLeft, Ban, Bug, CheckCircle2, ChevronDown, ChevronRight, CircleDashed, Clock, GitBranch, HardDrive, HelpCircle,
  Loader2, MessageSquare, Plus, RefreshCw, Search, Settings2, SkipForward, User, Users, Workflow, XCircle, type LucideIcon,
} from 'lucide-react';
import {
  Button, Card, EmptyState, Field, Input, Modal, PageHeader, StatusDot, Switch, Tabs, toast,
} from '@/components/kit';
import { Select } from '@/components/ui/Select';
import {
  debugLoggingApi, debugMode, withDebugMode, type DebugContext, type DebugEvent, type DebugMode,
  type DebugObject, type DebugObjectSummary, type DebugPolicy, type DebugPolicySnapshot, type DebugRequest,
} from '@/api/debugLogging';
import { logServices } from '@/api/logging';
import { errorText } from '@/utils/errorText';
import { cn } from '@/utils/cn';
import { diagnosticBorders, diagnosticTints, diagnosticTone, eventPresentation, inputDeltaPresentation, requestHint } from './debugPresentation';

const time = (value: number) => new Date(value * 1000).toLocaleString();
const statusNames: Record<string, string> = { running: '执行中', success: '已完成', failed: '失败', timeout: '超时', cancelled: '已取消', skipped: '未执行', unknown: '结果未确认', partial: '部分完成', interrupted: '未确认中断' };
const statusIcons: Record<string, LucideIcon> = { running: Loader2, success: CheckCircle2, failed: XCircle, timeout: Clock, cancelled: Ban, skipped: SkipForward, unknown: HelpCircle, partial: CircleDashed, interrupted: HelpCircle };

const kindMeta: Record<string, { label: string; icon: typeof Users; tint: 'pink' | 'teal' | 'accent' }> = {
  group: { label: '群', icon: Users, tint: 'pink' },
  private: { label: '私聊', icon: User, tint: 'teal' },
  chat: { label: 'Chat', icon: MessageSquare, tint: 'accent' },
};

const emptyStorage = { retention_days: 7, max_total_mb: 512, max_body_mb: 8, max_request_mb: 32 };

// 各服务只保存自己产生的链路: 群与私聊来自平台 Backend, Chat 来自 Chat 服务; Control 不产生请求链路
type DebugSource = 'backend' | 'chat';
type ObjectKind = 'all' | 'group' | 'private' | 'chat';
type DebugHealth = Awaited<ReturnType<typeof debugLoggingApi.health>>;
const sourceNames: Record<DebugSource, string> = { backend: '平台 Backend', chat: 'Chat' };
const sourcesFor = (kind: ObjectKind): DebugSource[] => kind === 'chat' ? ['chat'] : kind === 'all' ? ['backend', 'chat'] : ['backend'];
const sourceUrl = (service: string) => (logServices().find((item) => item.id === service) ?? logServices().find((item) => item.id === 'backend')!).url;
const objectId = (item: DebugObjectSummary) => `${item.service}:${item.object_key}`;

/** 多来源合并: 以服务 + 对象键去重, 按最近活动排序 */
function mergeObjects(current: DebugObjectSummary[], source: DebugSource, incoming: DebugObjectSummary[]): DebugObjectSummary[] {
  const seen = new Set(current.map(objectId));
  const added = incoming.map((item) => ({ ...item, service: item.service || source })).filter((item) => !seen.has(objectId(item)));
  return [...current, ...added].sort((left, right) => right.updated_at - left.updated_at);
}

/** 上下文负责浏览索引, 对象负责持久开关, 正文只在用户展开时读取 */
export function DebugLogs() {
  const [objectKind, setObjectKind] = useState<ObjectKind>('all');
  const sources = sourcesFor(objectKind);
  const sourcesKey = sources.join(',');
  const [refresh, setRefresh] = useState(0);
  const [settings, setSettings] = useState<DebugPolicySnapshot>();
  const [storageDraft, setStorageDraft] = useState(emptyStorage);
  const [settingsError, setSettingsError] = useState('');
  const [saving, setSaving] = useState(false);
  const [storageOpen, setStorageOpen] = useState(false);
  const [objects, setObjects] = useState<DebugObjectSummary[]>([]);
  const [objectKey, setObjectKey] = useState<string>();
  const [cursors, setCursors] = useState<Partial<Record<DebugSource, string | null>>>({});
  const [context, setContext] = useState<DebugContext>();
  const [sourceErrors, setSourceErrors] = useState<Partial<Record<DebugSource, string>>>({});
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState('');
  const [platformFilter, setPlatformFilter] = useState('');
  const [kindFilter, setKindFilter] = useState('');
  const [recordingFilter, setRecordingFilter] = useState('');
  const recordingRevision = recordingFilter ? settings?.revision : '';
  const contextQuery = {
    ...(filter ? { q: filter } : {}), ...(platformFilter ? { platform_id: platformFilter } : {}),
    ...(kindFilter ? { context_kind: kindFilter } : {}), ...(recordingFilter ? { recording: recordingFilter } : {}),
  };
  const [healths, setHealths] = useState<Partial<Record<DebugSource, DebugHealth>>>({});
  const epoch = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    void debugLoggingApi.settings(controller.signal).then((result) => {
      if (!controller.signal.aborted) {
        setSettings(result);
        setSettingsError('');
        setStorageDraft({
          retention_days: result.policy.retention_days,
          max_total_mb: result.policy.max_total_mb,
          max_body_mb: result.policy.max_body_mb,
          max_request_mb: result.policy.max_request_mb,
        });
      }
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setSettingsError(errorText(reason)); });
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController();
    setHealths({});
    for (const source of sourcesKey.split(',') as DebugSource[]) {
      void debugLoggingApi.health(sourceUrl(source), controller.signal).then((result) => {
        if (!controller.signal.aborted) setHealths((current) => ({ ...current, [source]: result }));
      }).catch(() => undefined);
    }
    return () => controller.abort();
  }, [sourcesKey, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    const current = ++epoch.current;
    setObjects([]);
    setObjectKey(undefined);
    setContext(undefined);
    setCursors({});
    setSourceErrors({});
    setLoading(true);
    const timer = setTimeout(() => void Promise.allSettled((sourcesKey.split(',') as DebugSource[]).map(async (source) => {
      try {
        const page = await debugLoggingApi.objects(sourceUrl(source), contextQuery, controller.signal);
        if (controller.signal.aborted || current !== epoch.current) return;
        setObjects((items) => mergeObjects(items, source, page.objects));
        setCursors((values) => ({ ...values, [source]: page.has_more ? page.next_cursor : null }));
      } catch (reason) {
        if (!controller.signal.aborted && current === epoch.current) setSourceErrors((values) => ({ ...values, [source]: errorText(reason) }));
      }
    })).finally(() => { if (current === epoch.current) setLoading(false); }), 250);
    return () => { clearTimeout(timer); controller.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- contextQuery 由下列筛选值派生
  }, [sourcesKey, refresh, filter, platformFilter, kindFilter, recordingFilter, recordingRevision]);

  const pendingSources = sources.filter((source) => cursors[source]);
  async function moreObjects() {
    if (!pendingSources.length || loading) return;
    const current = epoch.current;
    setLoading(true);
    await Promise.allSettled(pendingSources.map(async (source) => {
      try {
        const page = await debugLoggingApi.objects(sourceUrl(source), { ...contextQuery, cursor: cursors[source]! });
        if (current !== epoch.current) return;
        setObjects((items) => mergeObjects(items, source, page.objects));
        setCursors((values) => ({ ...values, [source]: page.has_more ? page.next_cursor : null }));
        setSourceErrors((values) => ({ ...values, [source]: undefined }));
      } catch (reason) { if (current === epoch.current) setSourceErrors((values) => ({ ...values, [source]: errorText(reason) })); }
    }));
    if (current === epoch.current) setLoading(false);
  }

  async function save(next: DebugPolicy): Promise<boolean> {
    if (!settings || saving) return false;
    setSaving(true);
    try {
      setSettings(await debugLoggingApi.save(next, settings.revision));
      setSettingsError('');
      return true;
    } catch (reason) {
      setSettingsError(`${errorText(reason)}; 请刷新设置后重试`);
      return false;
    } finally {
      setSaving(false);
    }
  }

  // 存储状态按当前来源汇总, 故障信息注明来源
  const healthList = sources.flatMap((source) => healths[source] ? [{ source, value: healths[source]! }] : []);
  const finite = (value: number | undefined) => (typeof value === 'number' && Number.isFinite(value) ? value : 0);
  const health = healthList.length ? {
    used_bytes: healthList.reduce((sum, item) => sum + finite(item.value.used_bytes), 0),
    wal_bytes: healthList.reduce((sum, item) => sum + finite(item.value.wal_bytes), 0),
    pending_events: healthList.reduce((sum, item) => sum + finite(item.value.pending_events), 0),
    write_error: healthList.filter((item) => item.value.write_error).map((item) => `${sourceNames[item.source]}: ${item.value.write_error}`).join('; '),
    maintenance_error: healthList.filter((item) => item.value.maintenance_error).map((item) => `${sourceNames[item.source]}: ${item.value.maintenance_error}`).join('; '),
    quota_blocked: healthList.some((item) => item.value.quota_blocked),
    dropped_events: healthList.reduce((sum, item) => sum + finite(item.value.dropped_events), 0),
    dropped_requests: healthList.reduce((sum, item) => sum + finite(item.value.dropped_requests), 0),
  } : undefined;
  const failedSources = sources.filter((source) => sourceErrors[source]);
  const allFailed = failedSources.length === sources.length;
  const storageFault = health && (health.write_error || health.maintenance_error);
  const storageWarning = health && (health.quota_blocked || !!health.dropped_events || !!health.dropped_requests);
  const contextMode = settings && context ? debugMode(settings.policy, context.object) : undefined;
  const contextRecording = contextMode === 'on' || (contextMode === 'inherit' && settings?.policy.default_enabled);
  const visibleObjects = objects.filter((item) => objectKind === 'all' || item.object.conversation_kind === objectKind);
  const selectedObject = context ? objects.find((item) => objectId(item) === objectKey) : undefined;
  const contextBase = sourceUrl(context?.service || selectedObject?.service || sources[0]);
  const usedMb = health ? health.used_bytes / 1e6 : null;

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow={(
          <Link to="/logs" className="inline-flex items-center gap-1 text-xs text-fg-3 transition hover:text-fg">
            <ArrowLeft className="h-3 w-3" />运行日志
          </Link>
        )}
        title="高级 Debug"
        description="按群、私聊或 Chat 归类, 查看每次请求的完整执行链路"
        actions={<>
          <Button icon={<RefreshCw className="h-4 w-4" />} onClick={() => setRefresh((value) => value + 1)}>刷新</Button>
          <Button icon={<Settings2 className="h-4 w-4" />} onClick={() => setStorageOpen(true)} disabled={!settings}>存储设置</Button>
        </>}
      />

      {/* 存储状态条 */}
      <div className="animate-rise flex flex-wrap items-center gap-x-5 gap-y-2 rounded-2xl border border-line bg-glass px-4 py-2.5 text-xs text-fg-3 backdrop-blur-md" style={{ animationDelay: '40ms' }}>
        <span className="flex items-center gap-2">
          <StatusDot tone={storageFault ? 'red' : storageWarning ? 'orange' : health ? 'green' : 'neutral'} live={!!health && !storageFault} />
          {storageFault ? '存储故障' : storageWarning ? '存储受限' : health ? '存储正常' : '读取中'}
        </span>
        <span className="num">默认记录 <span className={settings?.policy.default_enabled ? 'text-green' : 'text-fg-2'}>{settings ? (settings.policy.default_enabled ? '开启' : '关闭') : '—'}</span></span>
        <span className="num">保留 <span className="text-fg-2">{settings ? `${settings.policy.retention_days} 天` : '—'}</span></span>
        <span className="flex items-center gap-2">
          <span className="num">占用 <span className="font-mono text-fg-2">{usedMb === null ? '—' : `${usedMb.toFixed(1)} / ${settings?.policy.max_total_mb ?? '—'} MB`}</span></span>
          {settings && usedMb !== null && (
            <span className="h-1.5 w-24 overflow-hidden rounded-full bg-glass-hover">
              <span className={cn('block h-full rounded-full bg-tint', storageFault ? 'tint-red' : usedMb / Math.max(1, settings.policy.max_total_mb) > 0.85 ? 'tint-orange' : 'tint-green')}
                style={{ width: `${Math.min(100, (usedMb / Math.max(1, settings.policy.max_total_mb)) * 100)}%` }} />
            </span>
          )}
        </span>
        <span className="num">WAL <span className="font-mono text-fg-2">{health ? `${(health.wal_bytes / 1e6).toFixed(2)} MB` : '—'}</span></span>
        <span className="num">待写入 <span className="font-mono text-fg-2">{health ? health.pending_events : '—'}</span></span>
        <span className="ml-auto flex items-center gap-3" aria-label="数据来源">
          {sources.map((source) => (
            <span key={source} className="flex items-center gap-1.5">
              <StatusDot tone={sourceErrors[source] ? 'red' : healths[source] ? 'green' : 'neutral'} />{sourceNames[source]}
            </span>
          ))}
        </span>
      </div>

      {settingsError && <p role="alert" className="surface tint-red rounded-xl px-4 py-2.5 text-sm text-error break-words">{settingsError}</p>}
      {storageFault && health && (
        <Card role="alert" tint="red" className={cn('p-3 text-sm text-error', diagnosticBorders.error)}>
          高级日志存储故障: {health.write_error || health.maintenance_error}
        </Card>
      )}
      {storageWarning && health && (
        <p role="status" className="surface tint-orange rounded-xl px-4 py-2.5 text-sm text-warning">
          {health.quota_blocked ? '容量不足, 新请求的高级采集已暂停; 自动清理后恢复。' : ''} 本进程未记录事件 {health.dropped_events ?? 0} 条, 未采集请求 {health.dropped_requests ?? 0} 个。
        </p>
      )}

      {settings && <AvailableContexts settings={settings} saving={saving} save={save} />}

      <div className="grid items-start gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
        {/* 左: 对象 → 上下文 */}
        <Card padding="none" className="flex max-h-[calc(100vh-260px)] flex-col animate-rise lg:sticky lg:top-0" style={{ animationDelay: '80ms' } as CSSProperties}>
          <div className="space-y-2.5 p-3 pb-2">
            <Input
              leading={<Search className="h-4 w-4" />}
              placeholder="搜索群号、用户或名称"
              aria-label="搜索上下文或对象"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            />
            <Tabs
              size="sm"
              label="对象类型"
              value={objectKind}
              onChange={setObjectKind}
              className="w-full [&>button]:flex-1 [&>button]:justify-center"
              items={[
                { value: 'all', label: '全部' }, { value: 'group', label: '群' },
                { value: 'private', label: '私聊' }, { value: 'chat', label: 'Chat' },
              ]}
            />
            <div className="grid gap-2 sm:grid-cols-2">
              <Input aria-label="平台实例 ID" placeholder="平台实例 ID" value={platformFilter} onChange={(event) => setPlatformFilter(event.target.value)} />
              <Select
                value={kindFilter}
                options={[{ value: '', label: '全部上下文' }, { value: 'main', label: '主上下文' }, { value: 'workflow', label: '子工作流' }]}
                onChange={(event) => setKindFilter(event.target.value)}
                className="w-full"
              />
            </div>
            <Select
              value={recordingFilter}
              options={[{ value: '', label: '全部开关状态' }, { value: 'on', label: '当前开启' }, { value: 'off', label: '当前关闭' }]}
              onChange={(event) => setRecordingFilter(event.target.value)}
              className="w-full"
            />
          </div>
          <div className="scroll min-h-0 flex-1 space-y-0.5 px-2 pb-3">
            {!loading && !visibleObjects.length && !allFailed && (
              <p className="px-3 py-8 text-center text-[13px] text-fg-3">没有匹配的对象</p>
            )}
            {loading && (
              <div className="space-y-1.5 px-1" role="status">
                {[0, 1, 2].map((index) => <span key={index} className="skeleton block h-14 rounded-xl" />)}
                <span className="sr-only">正在加载…</span>
              </div>
            )}
            {visibleObjects.map((item) => (
              <ObjectItem
                key={objectId(item)}
                item={item}
                open={objectKey === objectId(item)}
                selectedCtx={context?.object_key === item.object_key ? context.context_id : undefined}
                recording={settings ? (debugMode(settings.policy, item.object) === 'on'
                  || (debugMode(settings.policy, item.object) === 'inherit' && settings.policy.default_enabled)) : false}
                onToggle={() => { setObjectKey(objectKey === objectId(item) ? undefined : objectId(item)); setContext(undefined); }}
                onSelectCtx={(value) => setContext({ ...value, service: value.service || item.service })}
                base={sourceUrl(item.service)}
                query={contextQuery}
                refresh={refresh}
              />
            ))}
            {pendingSources.length > 0 && <Button size="sm" variant="ghost" className="w-full" disabled={loading} onClick={() => void moreObjects()}>加载更多对象</Button>}
          </div>
          <div className="border-t border-line px-4 py-2.5 text-[11px] text-fg-4">
            在群或私聊里发送 <code className="rounded bg-glass-hover px-1 font-mono text-fg-3">/debug on</code> 也可以开启记录
          </div>
        </Card>

        {/* 右: 上下文头 + 请求列表 */}
        <div className="min-w-0 space-y-4">
          {failedSources.map((source) => (
            <p key={source} role={allFailed ? 'alert' : 'status'} className="surface tint-orange break-words rounded-xl px-4 py-2.5 text-sm text-warning">
              {sourceNames[source]} 暂不可用, {source === 'backend' ? '群与私聊' : 'Chat'}记录无法读取: {sourceErrors[source]}
            </p>
          ))}
          {context && settings ? (
            <div className="animate-rise space-y-4" key={`${context.object_key}:${context.context_id}`}>
              <ContextHeader
                context={context}
                object={selectedObject}
                recording={!!contextRecording}
                settings={settings}
                saving={saving}
                save={save}
              />
              <Requests key={`${contextBase}:${context.object_key}:${context.context_id}:${refresh}`} base={contextBase} context={context} />
            </div>
          ) : (
            <Card padding="none">
              <EmptyState
                tint="orange"
                icon={<GitBranch className="h-6 w-6" />}
                title="选择一个会话"
                description="展开左侧的群或人, 再选择具体会话查看请求链路"
              />
            </Card>
          )}
        </div>
      </div>

      {settings && (
        <StorageModal
          open={storageOpen}
          onClose={() => setStorageOpen(false)}
          policy={settings.policy}
          draft={storageDraft}
          saving={saving}
          error={settingsError}
          usedMb={usedMb}
          onDraft={setStorageDraft}
          onSave={save}
        />
      )}
    </div>
  );
}

function ObjectItem({ item, open, selectedCtx, recording, onToggle, onSelectCtx, base, query, refresh }: {
  item: DebugObjectSummary;
  open: boolean;
  selectedCtx?: string;
  recording: boolean;
  onToggle: () => void;
  onSelectCtx: (context: DebugContext) => void;
  base: string;
  query: Record<string, string>;
  refresh: number;
}) {
  const kind = item.object.conversation_kind;
  const meta = kindMeta[kind] ?? kindMeta.chat;
  const Icon = meta.icon;
  return (
    <div className={`tint-${meta.tint}`} data-debug-object={item.object_key}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className={cn(
          'group flex w-full items-center gap-3 rounded-xl px-2.5 py-2.5 text-left transition-colors',
          open ? 'bg-[rgb(var(--tint)/0.1)]' : 'hover:bg-glass-hover',
        )}
      >
        <span className="relative grid h-9 w-9 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.15)] text-tint">
          <Icon className="h-4 w-4" />
          <span className="absolute -bottom-0.5 -right-0.5 rounded-full bg-[var(--bg-base)] p-[2px]">
            <StatusDot tone={recording ? 'green' : 'neutral'} live={recording} className="!h-2 !w-2" />
          </span>
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="truncate text-[13.5px] font-medium text-fg">{meta.label} · {item.object.target_id}</span>
            {item.error_count > 0 && <span className="tint-red num rounded-full bg-[rgb(var(--tint)/0.16)] px-1.5 text-[10.5px] leading-[17px] text-tint">{item.error_count}</span>}
          </span>
          <span className="block truncate font-mono text-[11px] text-fg-4">{item.object.platform_id} · 账号 {item.object.self_id}</span>
          <span className="num block text-[10.5px] text-fg-4">
            会话 {item.context_count} · 请求 {item.request_count} · 异常 {item.error_count} · {time(item.updated_at)}
          </span>
        </span>
        <span className="flex flex-none flex-col items-end">
          <span className="num text-[10.5px] text-fg-4">{item.service}</span>
          <ChevronRight className={cn('mt-0.5 h-3.5 w-3.5 text-fg-4 transition-transform duration-300', open && 'rotate-90')} />
        </span>
      </button>
      {open && (
        <ObjectContexts
          key={`${item.object_key}:${refresh}`}
          base={base}
          objectKey={item.object_key}
          query={query}
          selected={selectedCtx}
          select={onSelectCtx}
        />
      )}
    </div>
  );
}

function ContextHeader({ context, object, recording, settings, saving, save }: {
  context: DebugContext;
  object?: DebugObjectSummary;
  recording: boolean;
  settings: DebugPolicySnapshot;
  saving: boolean;
  save: (policy: DebugPolicy) => Promise<boolean>;
}) {
  const kind = context.object.conversation_kind;
  const meta = kindMeta[kind] ?? kindMeta.chat;
  return (
    <Card padding="lg" tint={meta.tint as 'pink'} className="animate-rise" style={{ animationDelay: '120ms' } as CSSProperties}>
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-xs text-fg-3">
            <span className="text-tint">{meta.label} · {object?.object_key ?? context.object.target_id}</span>
            <ChevronRight className="h-3 w-3" />
            <span className="break-all font-mono">{context.object.platform_id} / {context.object.self_id} / {context.object.target_id}</span>
          </div>
          <h2 className="mt-1 flex items-center gap-2 break-all text-lg font-semibold text-fg">
            {context.context_kind === 'workflow' && <Workflow className="h-4 w-4 flex-none text-fg-3" />}
            {context.title || context.context_id || '未绑定上下文'}
          </h2>
          <p className="num mt-0.5 break-all font-mono text-[11px] text-fg-4">{context.context_id}</p>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <span className="flex items-center gap-1.5 text-[11px] text-fg-3">
            <StatusDot tone={recording ? 'green' : 'neutral'} live={recording} />
            {recording ? '正在记录' : '未记录'}
          </span>
          <ModeSwitch policy={settings.policy} object={context.object} saving={saving} save={save} />
        </div>
      </div>
      <div className="mt-4 grid grid-cols-3 gap-2">
        {[
          { k: '请求', v: String(context.request_count) },
          { k: '异常', v: String(context.error_count), red: context.error_count > 0 },
          { k: '最近活动', v: time(context.updated_at) },
        ].map((stat) => (
          <div key={stat.k} className="min-w-0 rounded-xl border border-line bg-glass px-3 py-2">
            <div className="text-[11px] text-fg-4">{stat.k}</div>
            <div className={cn('num mt-0.5 truncate text-base font-semibold', stat.red ? 'text-red' : 'text-fg')}>{stat.v}</div>
          </div>
        ))}
      </div>
      <p className="mt-3 text-[11px] text-fg-4">对象记录开关属于对象, /new 后继续生效; 列表中的不同上下文共用此设置。</p>
    </Card>
  );
}

/** 对象开关: 保留生产 Select 的语义与选项文案, 便于键盘与旧脚本操作 */
function ModeSwitch({ policy, object, saving, save }: {
  policy: DebugPolicy; object: DebugObject; saving: boolean; save: (policy: DebugPolicy) => Promise<boolean>;
}) {
  return (
    <Select
      className="w-44"
      value={debugMode(policy, object)}
      disabled={saving}
      options={[
        { value: 'inherit', label: `继承默认 (${policy.default_enabled ? '开启' : '关闭'})` },
        { value: 'on', label: '强制开启' },
        { value: 'off', label: '强制关闭' },
      ]}
      onChange={(event) => void save(withDebugMode(policy, object, event.target.value as DebugMode))}
    />
  );
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
    const abort = new AbortController();
    controller.current = abort;
    setItems([]);
    setCursor(null);
    setError('');
    setBusy(true);
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
  return (
    <div className="ml-[29px] space-y-0.5 border-l border-line py-1 pl-2 animate-rise">
      {items.map((item) => {
        const active = selected === item.context_id;
        return (
          <button
            key={item.context_id}
            type="button"
            data-debug-context={item.context_id}
            onClick={() => select(item)}
            aria-current={active ? 'true' : undefined}
            className={cn(
              'relative flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors',
              active ? 'bg-[rgb(var(--tint)/0.14)] text-fg' : 'text-fg-2 hover:bg-glass-hover hover:text-fg',
            )}
          >
            {active && <span aria-hidden className="absolute -left-[9px] top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-full bg-tint" />}
            {item.context_kind === 'workflow' && <Workflow className="h-3.5 w-3.5 flex-none text-fg-3" />}
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px]">{item.title || item.context_id || '未绑定上下文'}</span>
              <span className="num block text-[10.5px] text-fg-4">请求 {item.request_count} · 异常 {item.error_count} · {time(item.updated_at)}</span>
            </span>
            <span className="num flex-none text-[11px] text-fg-4">{item.request_count}</span>
          </button>
        );
      })}
      {error && <p role="alert" className="px-2 py-1 text-sm text-error">{error}</p>}
      {busy && (
        <p role="status" className="flex items-center gap-1.5 px-2.5 py-1 text-xs text-fg-4">
          <Loader2 className="h-3 w-3 animate-spin" />正在加载会话…
        </p>
      )}
      {cursor && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void more()}>加载更多会话</Button>}
    </div>
  );
}

function AvailableContexts({ settings, saving, save }: {
  settings: DebugPolicySnapshot; saving: boolean; save: (policy: DebugPolicy) => Promise<boolean>;
}) {
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
    setBusy(true);
    setItems([]);
    setSelected(undefined);
    setCursor(null);
    setError('');
    void debugLoggingApi.available(platform, '', controller.signal).then((page) => {
      if (controller.signal.aborted) return;
      setItems(page.contexts);
      setPlatforms(page.platforms);
      setCursor(page.has_more ? page.next_cursor : null);
      if (page.unconfirmed) setError(`本页 ${page.unconfirmed} 项旧记录无法确认对象归属, 已跳过`);
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(errorText(reason)); })
      .finally(() => { if (current === generation.current) setBusy(false); });
    return () => controller.abort();
  }, [platform, open]);
  async function more() {
    if (!cursor || busy) return;
    const current = generation.current;
    setBusy(true);
    try {
      const page = await debugLoggingApi.available(platform, cursor);
      if (current !== generation.current) return;
      setItems((values) => [...values, ...page.contexts]);
      setCursor(page.has_more ? page.next_cursor : null);
    } catch (reason) { if (current === generation.current) setError(errorText(reason)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  return (
    <Card padding="none" className="overflow-hidden">
      <button
        type="button"
        className="tint-purple flex w-full items-center gap-3 px-5 py-3.5 text-left transition-colors hover:bg-glass-hover"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="grid h-8 w-8 place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint"><Plus className="h-4 w-4" /></span>
        <span className="min-w-0 flex-1">
          <span className="block font-semibold">为已有上下文开启记录</span>
          <span className="block text-xs text-fg-3">从已保存的路由选择对象, 无需先产生日志</span>
        </span>
        <ChevronDown className={cn('h-4 w-4 flex-none text-fg-3 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="animate-rise space-y-3 border-t border-line px-5 py-4">
          <p className="text-sm text-fg-2">从已保存的路由选择对象, 无需先产生日志或恢复会话。</p>
          <Select className="w-48" value={platform} options={platforms.map((id) => ({ value: id, label: id }))} onChange={(event) => setPlatform(event.target.value)} />
          {error && <p role="status" className="text-sm text-warning">{error}</p>}
          <div className="scroll grid max-h-60 gap-1.5 sm:grid-cols-2">
            {items.map((item) => {
              const active = selected?.object_key === item.object_key && selected.context_id === item.context_id;
              return (
                <button
                  key={`${item.object_key}:${item.context_id}`}
                  type="button"
                  onClick={() => setSelected(item)}
                  aria-pressed={active}
                  className={cn(
                    'break-all rounded-lg border px-3 py-2 text-left text-sm transition-colors',
                    active
                      ? 'border-[rgb(var(--rgb-purple)/0.5)] bg-[rgb(var(--rgb-purple)/0.12)] text-purple'
                      : 'border-line hover:bg-glass-hover',
                  )}
                >
                  {item.context_id} · {item.object.conversation_kind} · {item.object.target_id}
                </button>
              );
            })}
          </div>
          {busy && (
            <p role="status" className="flex items-center gap-1.5 text-sm text-fg-3">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />正在读取已有上下文…
            </p>
          )}
          {!busy && !items.length && !error && <p className="text-sm text-fg-2">该平台暂无可确认归属的上下文</p>}
          {cursor && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void more()}>加载更多已有上下文</Button>}
          {selected && (
            <div className="flex flex-wrap items-center gap-3 border-t border-line pt-3">
              <span className="break-all text-sm">{selected.object.platform_id} / {selected.object.self_id} / {selected.object.conversation_kind} / {selected.object.target_id}</span>
              <ModeSwitch policy={settings.policy} object={selected.object} saving={saving} save={save} />
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function Requests({ base, context }: { base: string; context: DebugContext }) {
  const [items, setItems] = useState<DebugRequest[]>([]);
  const [cursor, setCursor] = useState<string | null>();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [mountedIds, setMountedIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const toggle = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    setMountedIds((current) => current.has(id) ? current : new Set(current).add(id));
  };
  const generation = useRef(0);
  const [status, setStatus] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const updateRequest = useCallback((summary: DebugRequest) => setItems((values) => values.map((item) => item.request_id === summary.request_id ? summary : item)), []);
  const query = {
    object_key: context.object_key, context_id: context.context_id, ...(status && status !== 'issue' ? { status } : {}),
    ...(from ? { from: String(new Date(from).getTime() / 1000) } : {}), ...(to ? { to: String(new Date(to).getTime() / 1000) } : {}),
  };
  useEffect(() => {
    const controller = new AbortController();
    mounted.current = true;
    const current = ++generation.current;
    setItems([]);
    setExpanded(new Set());
    setMountedIds(new Set());
    setCursor(null);
    setError('');
    setBusy(false);
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
  const counts = {
    all: items.length,
    issue: items.filter((item) => item.diagnostic_level === 'warning' || item.diagnostic_level === 'error').length,
    running: items.filter((item) => item.status === 'running').length,
  };
  const shown = status === 'issue'
    ? items.filter((item) => item.diagnostic_level === 'warning' || item.diagnostic_level === 'error')
    : items;
  const statusItems = [
    { value: '', label: '全部', count: counts.all },
    { value: 'issue', label: '有问题', count: counts.issue },
    { value: 'running', label: '执行中', count: counts.running },
    ...Object.entries(statusNames)
      .filter(([value]) => value !== 'running' && value !== 'success')
      .map(([value, label]) => ({ value, label })),
    { value: 'success', label: '已完成' },
  ];
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 animate-rise" style={{ animationDelay: '160ms' } as CSSProperties}>
        <Tabs size="sm" label="请求筛选" value={status} onChange={setStatus} items={statusItems} />
        <span className="ml-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-fg-4">
          <span className="sr-only">外框说明: 蓝色正常 · 黄色调用问题或待确认 · 红色系统错误 · 灰色等待、取消或旧记录未分类</span>
          {([['accent', '正常'], ['orange', '调用问题'], ['red', '系统错误'], ['neutral', '等待 / 取消']] as const).map(([tint, label]) => (
            <span key={tint} aria-hidden className="flex items-center gap-1.5">
              <span className={cn('h-2.5 w-[3px] rounded-full', tint === 'neutral' ? 'bg-[var(--text-4)]' : `tint-${tint} bg-tint`)} />{label}
            </span>
          ))}
        </span>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <p className="mr-auto self-center text-sm font-semibold text-fg">
          请求记录{items.length > 0 && <span className="num ml-2 text-xs text-fg-4">{items.length}{cursor ? '+' : ''}</span>}
        </p>
        <label className="text-[11px] text-fg-3">
          <span className="mb-1 block">开始时间</span>
          <Input type="datetime-local" value={from} onChange={(event) => setFrom(event.target.value)} />
        </label>
        <label className="text-[11px] text-fg-3">
          <span className="mb-1 block">结束时间</span>
          <Input type="datetime-local" value={to} onChange={(event) => setTo(event.target.value)} />
        </label>
      </div>
      {error && <p role="alert" className="text-sm text-error">{error}</p>}
      {!shown.length && !error && (
        <Card padding="none"><EmptyState tint="orange" icon={<Bug className="h-6 w-6" />} title="此上下文暂无请求记录" description="新请求产生后会出现在这里" /></Card>
      )}
      <div className="stagger space-y-2">
        {shown.map((item, index) => {
          const tone = diagnosticTone(item.diagnostic_level, item.status, !!item.incomplete);
          const open = expanded.has(item.request_id);
          const hint = requestHint(item);
          const StatusIcon = statusIcons[item.status] ?? CircleDashed;
          const totalMs = Math.max(0, (item.updated_at - item.started_at) * 1000);
          const percent = Math.min(100, Math.max(2, (totalMs / 30000) * 100));
          return (
            <Card
              key={item.request_id}
              data-debug-request={item.request_id}
              data-diagnostic-tone={tone}
              padding="none"
              tint={tone === 'normal' ? 'accent' : tone === 'warning' ? 'orange' : tone === 'error' ? 'red' : undefined}
              style={{ '--i': Math.min(index, 12) } as CSSProperties}
              className={cn('relative overflow-hidden !rounded-2xl', diagnosticBorders[tone])}
            >
              <button
                type="button"
                className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-left transition-colors hover:bg-glass-hover/60"
                onClick={() => toggle(item.request_id)}
                aria-label={`${statusNames[item.status] || item.status} ${hint} ${item.request_id}`}
                aria-expanded={open}
              >
                <span aria-hidden className={cn(
                  'grid h-7 w-7 flex-none place-items-center rounded-lg',
                  diagnosticTints[tone] ? 'bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]' : 'bg-glass-hover text-fg-3',
                )}>
                  <StatusIcon className={cn('h-4 w-4', item.status === 'running' && 'animate-spin')} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <strong className="text-sm text-fg">{statusNames[item.status] || item.status}</strong>
                    {hint && <span className={cn('chip', diagnosticTints[tone])}>{hint}</span>}
                    {!!item.incomplete && <span className="chip tint-orange">记录不完整</span>}
                  </span>
                  <span className="mt-0.5 block truncate font-mono text-[11.5px] text-fg-3">{item.request_id}</span>
                </span>
                <span className="hidden w-24 flex-none sm:block">
                  <span className="block h-1.5 overflow-hidden rounded-full bg-glass-hover">
                    <span className={cn(
                      'block h-full rounded-full',
                      tone === 'neutral' || tone === 'unknown' ? 'bg-[var(--text-4)]' : 'bg-[linear-gradient(90deg,rgb(var(--tint)/0.5),rgb(var(--tint)))]',
                      item.status === 'running' && 'animate-pulse',
                    )} style={{ width: `${percent}%` }} />
                  </span>
                  <span className="num mt-1 block text-right font-mono text-[10.5px] text-fg-4">
                    {totalMs >= 1000 ? `${(totalMs / 1000).toFixed(2)}s` : `${Math.round(totalMs)}ms`}
                  </span>
                </span>
                <span className="flex w-20 flex-none flex-col items-end">
                  <span className="num font-mono text-[11px] text-fg-2">{time(item.started_at)}</span>
                  <span className="num font-mono text-[10px] text-fg-4">{item.request_id.slice(0, 12)}</span>
                </span>
                <ChevronDown className={cn('h-4 w-4 flex-none text-fg-4 transition-transform duration-300', open && 'rotate-180')} />
              </button>
              {/* 首次展开后保留时间线, 再次展开不重新请求 */}
              {mountedIds.has(item.request_id) && (
                <Collapse open={open}><div className="px-4 pb-4"><Timeline base={base} request={item} onState={updateRequest} /></div></Collapse>
              )}
            </Card>
          );
        })}
      </div>
      {cursor && <Button variant="ghost" className="w-full" disabled={busy} onClick={() => void more()}>加载更多请求</Button>}
    </div>
  );
}

/** 高度过渡的折叠容器: 收起动画结束后才隐藏内容, 使收起项不参与键盘与读屏顺序 */
function Collapse({ open, children }: { open: boolean; children: ReactNode }) {
  const [shown, setShown] = useState(open);
  useEffect(() => {
    if (open) { setShown(true); return; }
    // 减少动效时不会触发 transitionend, 用定时器兜底
    const timer = window.setTimeout(() => setShown(false), 600);
    return () => window.clearTimeout(timer);
  }, [open]);
  return (
    <div
      className={cn('grid transition-[grid-template-rows,opacity] duration-500 [transition-timing-function:var(--ease-out)]', open ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0')}
      aria-hidden={!open || undefined}
      onTransitionEnd={(event) => { if (event.target === event.currentTarget && !open) setShown(false); }}
    >
      <div className={cn('min-h-0 overflow-hidden', !open && !shown && 'hidden')}>{children}</div>
    </div>
  );
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
    const abort = new AbortController();
    controller.current = abort;
    setEvents([]);
    setExpanded(new Set());
    setBody(undefined);
    setOpenBody(undefined);
    setError('');
    setBusy(false);
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
      if (next.has(eventId)) next.delete(eventId); else next.add(eventId);
      return next;
    });
    if (openBody?.startsWith(`${eventId}:`)) setOpenBody(undefined);
  }
  async function readBody(event: DebugEvent, slot: string, full = false) {
    const key = `${event.event_id}:${slot}${full ? ':full' : ''}`;
    if (body?.key === key) { setOpenBody(openBody === key ? undefined : key); return; }
    setBusy(true);
    setOpenBody(key); // 在等待前记录用户选择, 收起操作不会被迟到的响应撤销
    const signal = controller.current?.signal;
    try {
      const value = await debugLoggingApi.body(base, { request_id: request.request_id, event_id: event.event_id, slot }, signal, full);
      if (mounted.current && !signal?.aborted) {
        setBody({ key, ...(!full && event.category === 'model' && slot === 'input' ? inputDeltaPresentation(value) : { text: JSON.stringify(value, null, 2) }) });
        setError('');
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
      anchor.href = url;
      anchor.download = `debug-${request.request_id}.jsonl`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) { if (mounted.current) setError(errorText(reason)); }
    finally { if (mounted.current) setBusy(false); }
  }
  const lastRecorded = events.length ? Math.max(...events.map((event) => event.recorded_at)) : request.started_at;
  const totalSeconds = Math.max(1, lastRecorded - request.started_at);

  return (
    <div className="space-y-3 border-t border-line pt-3">
      {request.parent_request_id && <SourceRequest base={base} requestId={request.parent_request_id} depth={depth} />}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={!events.length} onClick={() => setExpanded(new Set(events.map((event) => event.event_id)))}>展开全部节点</Button>
        <Button size="sm" disabled={!expanded.size} onClick={() => { setExpanded(new Set()); setOpenBody(undefined); }}>全部收起</Button>
        <Button size="sm" variant="ghost" className="ml-auto" disabled={busy} onClick={() => void exportRequest()}>导出本次链路</Button>
      </div>
      <p className="text-xs text-fg-4">本次请求的动作按发生顺序列出。模型输入默认隐藏重复历史; 首次调用从最近一条用户消息开始, 后续调用显示相对上次输入的变更。</p>
      <details className="rounded-lg border border-line px-3 py-2 text-xs text-fg-2">
        <summary className="cursor-pointer select-none">请求信息</summary>
        <dl className="mt-2 grid gap-1 sm:grid-cols-[auto_minmax(0,1fr)] sm:gap-x-3">
          <dt className="text-fg-4">请求 ID</dt><dd className="break-all font-mono">{request.request_id}</dd>
          <dt className="text-fg-4">上下文</dt><dd className="break-all font-mono">{request.context_id}</dd>
          <dt className="text-fg-4">设置修订</dt><dd className="break-all font-mono">{request.settings_revision}</dd>
        </dl>
      </details>
      {error && <p role="alert" className="text-sm text-error">{error}</p>}
      <ol className="relative space-y-2 pl-5 before:absolute before:bottom-2 before:left-[7px] before:top-2 before:w-px before:bg-[var(--glass-border-strong)]">
        {events.map((event) => {
          const presentation = eventPresentation(event);
          const open = expanded.has(event.event_id);
          const tint = diagnosticTints[presentation.tone];
          const offset = Math.max(0, event.recorded_at - request.started_at);
          const left = Math.min(96, (offset / totalSeconds) * 100);
          return (
            <li key={event.event_id} className="relative">
              <span aria-hidden className={cn('dot absolute -left-[18px] top-[15px]', tint)} />
              <div
                data-debug-event={event.event_id}
                data-diagnostic-tone={presentation.tone}
                className={cn('rounded-xl px-3 py-2 transition-colors', tint, diagnosticBorders[presentation.tone], open ? 'bg-glass-hover/50' : '')}
              >
                <button
                  type="button"
                  className="flex w-full flex-wrap items-center gap-x-2.5 gap-y-1 text-left text-sm"
                  aria-expanded={open}
                  onClick={() => toggleEvent(event.event_id)}
                >
                  <ChevronRight aria-hidden className={cn('h-3.5 w-3.5 flex-none text-fg-3 transition-transform', open && 'rotate-90')} />
                  <span className="num text-xs text-fg-4">#{event.sequence}</span>
                  <strong className="break-all font-medium text-fg">{presentation.title}</strong>
                  {!presentation.title.endsWith(presentation.label) && <span className={cn('chip', tint)}>{presentation.label}</span>}
                  <span aria-hidden className="ml-auto hidden h-1.5 w-20 flex-none overflow-hidden rounded-full bg-glass-hover sm:block">
                    <span className={cn('block h-full rounded-full', tint ? 'bg-tint' : 'bg-[var(--text-4)]')} style={{ marginLeft: `${left}%`, width: '4%', minWidth: 3 }} />
                  </span>
                  <time className="num ml-auto text-xs text-fg-4 sm:ml-0">{time(event.recorded_at)}</time>
                </button>
                {open && (
                  <div className="animate-rise space-y-2 pl-1 pt-2 sm:pl-6">
                    {typeof event.metadata.reason === 'string' && event.metadata.reason && <p className="break-words text-sm">{event.metadata.reason}</p>}
                    <details className="text-xs">
                      <summary className="cursor-pointer select-none text-fg-4">技术信息</summary>
                      <p className="mt-1 break-all text-fg-2">事件 ID: {event.event_id} · {event.category} / {event.phase} / {event.status}</p>
                      {event.parent_span_id && <p className="break-all text-fg-2">父步骤: {event.parent_span_id}</p>}
                      <pre className="scroll mt-1 max-h-48 whitespace-pre-wrap break-all rounded-lg bg-glass-hover/60 p-2.5 font-mono">{JSON.stringify(event.metadata, null, 2)}</pre>
                    </details>
                    <div className="flex flex-wrap gap-2">
                      {event.body_slots.map((slot) => (
                        <Button
                          key={slot}
                          size="sm"
                          disabled={busy}
                          aria-expanded={openBody === `${event.event_id}:${slot}`}
                          onClick={() => void readBody(event, slot)}
                        >
                          {openBody === `${event.event_id}:${slot}` ? '收起' : '查看'} {event.category === 'model' && slot === 'input' ? '新增输入' : slot}
                        </Button>
                      ))}
                      {event.category === 'model' && event.phase === 'start' && event.body_slots.includes('input') && (
                        <Button size="sm" variant="ghost" disabled={busy} aria-expanded={openBody === `${event.event_id}:input:full`} onClick={() => void readBody(event, 'input', true)}>
                          {openBody === `${event.event_id}:input:full` ? '收起' : '查看'}完整输入
                        </Button>
                      )}
                    </div>
                    {body && openBody === body.key && body.key.startsWith(`${event.event_id}:`) && (
                      <div className="animate-rise">
                        {body.summary && <p className="chip mb-2 !whitespace-normal">{body.summary}</p>}
                        <pre className="scroll max-h-96 whitespace-pre-wrap break-all rounded-xl border border-line bg-[rgb(0_0_0/0.22)] p-3 font-mono text-xs leading-relaxed text-fg-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)]">{body.text.slice(0, 50000)}</pre>
                        {body.text.length > 50000 && <p className="mt-1 text-xs text-fg-4">页面仅显示前 50000 字符, 完整正文可通过链路导出查看</p>}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      {(hasMore || request.status === 'running') && (
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void more()}>{hasMore ? '加载更多事件' : '刷新后续事件'}</Button>
      )}
    </div>
  );
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
  return (
    <div className="space-y-2 rounded-xl border border-dashed border-[rgb(var(--rgb-teal)/0.45)] bg-[rgb(var(--rgb-teal)/0.06)] p-3 tint-teal">
      <button type="button" className="flex items-center gap-1.5 break-all text-sm text-tint disabled:opacity-50" disabled={depth >= 8} aria-expanded={open} onClick={() => setOpen(!open)}>
        <GitBranch aria-hidden className="h-3.5 w-3.5 flex-none" />{open ? '收起' : '展开'}来源链路: {requestId}
      </button>
      {error && <p role="alert" className="text-sm text-warning">来源链路不可用或已清理: {error}</p>}
      {open && request && (
        <>
          <p className="text-xs text-fg-2">{statusNames[request.status] || request.status} · {time(request.started_at)} · {request.context_id}</p>
          <Timeline base={base} request={request} depth={depth + 1} onState={setRequest} />
        </>
      )}
    </div>
  );
}

function StorageModal({ open, onClose, policy, draft, saving, error, usedMb, onDraft, onSave }: {
  open: boolean;
  onClose: () => void;
  policy: DebugPolicy;
  draft: typeof emptyStorage;
  saving: boolean;
  error: string;
  usedMb: number | null;
  onDraft: (value: typeof emptyStorage) => void;
  onSave: (policy: DebugPolicy) => Promise<boolean>;
}) {
  const totalMb = Math.max(1, draft.max_total_mb);
  const ratio = usedMb === null ? 0 : Math.min(1, usedMb / totalMb);
  return (
    <Modal
      open={open}
      onClose={onClose}
      size="lg"
      title="记录与存储"
      description="修改从下一次请求生效. 关闭记录不会删除已有数据."
      icon={<HardDrive className="h-[18px] w-[18px]" />}
      className="tint-orange"
      footer={<>
        <Button variant="ghost" onClick={onClose}>取消</Button>
        <Button
          variant="primary"
          loading={saving}
          onClick={() => void onSave({ ...policy, ...draft }).then((ok) => { if (ok) { toast.success('存储设置已保存'); onClose(); } })}
        >
          保存
        </Button>
      </>}
    >
      <div className="rounded-xl border border-line bg-glass px-4">
        <Field inline label="默认记录" hint="未单独设置的群、私聊和 Chat 是否记录">
          <Switch label="默认记录" checked={policy.default_enabled} disabled={saving} onChange={(value) => void onSave({ ...policy, default_enabled: value })} />
        </Field>
      </div>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <Field label="保留天数">
          <Input type="number" min={1} value={draft.retention_days} disabled={saving}
            onChange={(event) => onDraft({ ...draft, retention_days: Number(event.target.value) })} />
        </Field>
        <Field label="总存储目标 (MB)">
          <Input type="number" min={1} value={draft.max_total_mb} disabled={saving}
            onChange={(event) => onDraft({ ...draft, max_total_mb: Number(event.target.value) })} />
        </Field>
        <Field label="单份正文上限 (MB)">
          <Input type="number" min={1} value={draft.max_body_mb} disabled={saving}
            onChange={(event) => onDraft({ ...draft, max_body_mb: Number(event.target.value) })} />
        </Field>
        <Field label="单请求正文上限 (MB)">
          <Input type="number" min={1} value={draft.max_request_mb} disabled={saving}
            onChange={(event) => onDraft({ ...draft, max_request_mb: Number(event.target.value) })} />
        </Field>
      </div>
      {error && <p role="alert" className="mt-3 break-words text-sm text-error">{error}</p>}
      <div className="mt-5">
        <div className="mb-1.5 flex justify-between text-xs text-fg-3">
          <span>当前占用</span>
          <span className="num font-mono">{usedMb === null ? '—' : usedMb.toFixed(1)} / {totalMb} MB</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-glass-hover">
          <div className={cn('h-full rounded-full bg-[linear-gradient(90deg,rgb(var(--tint)/0.6),rgb(var(--tint)))]', ratio > 0.85 ? 'tint-red' : ratio > 0.6 ? 'tint-orange' : 'tint-green')}
            style={{ width: `${ratio * 100}%`, transition: 'width 600ms var(--ease-out)' }} />
        </div>
        <p className="mt-2 text-xs text-fg-4">超出预算或写入失败时, 新记录会标记为不完整; 过期数据每小时自动清理.</p>
      </div>
    </Modal>
  );
}
