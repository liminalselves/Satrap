import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, Bot, CheckCircle2, ChevronRight, MessageSquare, RefreshCw, Save, Server, ShieldAlert, Wrench,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { controlApi } from '@/api/control';
import { createPluginApplication, type PluginApplicationResult } from '@/api/pluginApplications';
import { ApplicationUnconfirmedError } from '@/api/runtimeApplication';
import { useRuntimeApplication } from '@/hooks/useRuntimeApplication';
import { RuntimeApplicationProgress } from '@/components/common/RuntimeApplicationProgress';
import { VersionedConfigRead } from '@/api/configVersions';
import { startVisiblePoll } from '@/utils/visiblePoll';
import type { ManagedPlugin, PluginLocation, PluginLocationState, PluginRuntimeResult, PluginRuntimeSnapshot, PluginUsagesResult } from '@/api/types';
import { PLUGIN_CAPABILITY_LABELS, capabilityStatus } from '@/components/common/PluginCapabilities';
import type { PluginSettingsContext } from '@/api/pluginSettings';
import { Badge, Button, Card, CardHeader, Skeleton, StatusDot } from '@/components/kit';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { PluginRuntimeStatus } from './GlobalPluginSettings';
import { pluginError } from './InstallPluginModal';
import { SessionSettingsDrawer } from './SessionSettingsDrawer';
import { TealPrimary } from './PluginBits';
import { CAPABILITY_KINDS, CAPABILITY_LABELS, CAPABILITY_TINT, pluginCapabilities, type Capabilities } from './pluginModel';

function locationState(location: PluginLocation): PluginLocationState {
  return { present: location.present, enabled: location.enabled, capabilities: location.capabilities };
}

const locationKey = (location: Pick<PluginLocation, 'kind' | 'id'>) => `${location.kind}:${location.id}`;

function capCount(caps: Record<string, Record<string, boolean>>) {
  return CAPABILITY_KINDS.reduce((total, kind) => total + Object.keys(caps[kind] ?? {}).length, 0);
}

function enabledCapCount(caps: Record<string, Record<string, boolean>>) {
  return CAPABILITY_KINDS.reduce((total, kind) => total + Object.values(caps[kind] ?? {}).filter(Boolean).length, 0);
}

function CapabilityToggles({ capabilities, values, active, disabled, onChange }: {
  capabilities: Capabilities;
  values: Record<string, Record<string, boolean>>;
  active: boolean;
  disabled: boolean;
  onChange: (kind: string, name: string, enabled: boolean) => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {CAPABILITY_KINDS.map((kind) => {
        const entries = Object.entries(values[kind] ?? {});
        if (!entries.length) return null;
        const label = CAPABILITY_LABELS[kind];
        return (
          <section key={kind} className={cn('rounded-xl border border-line bg-glass p-3', `tint-${CAPABILITY_TINT[kind]}`)}>
            <div className="mb-2 flex items-center gap-2">
              <span className="h-1.5 w-1.5 rounded-full bg-tint shadow-[0_0_8px_rgb(var(--tint))]" />
              <h4 className="text-[12.5px] font-semibold text-fg">{label}</h4>
              <span className="num ml-auto text-[10.5px] text-fg-4">{entries.filter(([, enabled]) => enabled).length}/{entries.length}</span>
            </div>
            <ul className="space-y-1.5">
              {entries.map(([name, enabled]) => (
                <li key={name}>
                  <label className={cn('flex items-start justify-between gap-2 rounded-lg bg-[rgb(var(--tint)/0.05)] px-2.5 py-2', !active && 'opacity-80')}>
                    <span className="min-w-0">
                      <code className={cn('break-all font-mono text-[11.5px] font-medium', enabled ? 'text-fg' : 'text-fg-3')}>{name}</code>
                      <span className="mt-0.5 block break-words text-[10.5px] leading-4 text-fg-3">{capabilities[kind]?.[name] || '未提供说明'}</span>
                      {!active && enabled && <span className="mt-0.5 block text-[10.5px] text-orange">插件停用中，开关会保留</span>}
                    </span>
                    <input
                      type="checkbox"
                      className="mt-0.5 h-4 w-4 flex-none accent-[rgb(var(--rgb-teal))]"
                      aria-label={`${label} ${name}`}
                      checked={enabled}
                      disabled={disabled}
                      onChange={(event) => onChange(kind, name, event.target.checked)}
                    />
                  </label>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

export function PluginUsages({ plugin, onDirty, onSaved }: { plugin: ManagedPlugin; onDirty: (dirty: boolean) => void; onSaved: () => void }) {
  const [data, setData] = useState<PluginUsagesResult>();
  const [selected, setSelected] = useState('chat:chat');
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [draft, setDraft] = useState<PluginLocationState>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [snapshot, setSnapshot] = useState<PluginRuntimeSnapshot>();
  const [runtimeError, setRuntimeError] = useState('');
  const [runtime, setRuntime] = useState<PluginRuntimeResult[]>([]);
  const application = useRuntimeApplication<PluginApplicationResult>();
  const locked = busy || Boolean(application.view?.busy);
  const runtimeScope = useRef<{ plugin_name: string; kind: 'chat' | 'edictum'; location_id: string }>();
  const runtimeRead = useMemo(() => new VersionedConfigRead([], (signal) => controlApi.getPluginRuntime(plugin.name, signal)), [plugin.name]);
  const runtimeRequest = useRef(0);
  const [sessionContext, setSessionContext] = useState<PluginSettingsContext | null>(null);
  const location = data?.locations.find((item) => locationKey(item) === selected);
  const capabilities = useMemo(() => pluginCapabilities(plugin), [plugin]);
  const dirty = !!location && !!draft && JSON.stringify(locationState(location)) !== JSON.stringify(draft);
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => { runtimeScope.current = undefined; setRuntime([]); }, [plugin.name]);
  useEffect(() => {
    let disposed = false;
    setError('');
    controlApi.getPluginUsages(plugin.name).then((result) => {
      if (disposed) return;
      setData(result);
      const first = result.locations.find((item) => locationKey(item) === selectedRef.current) || result.locations[0];
      if (first) { setSelected(locationKey(first)); setDraft(locationState(first)); }
    }).catch((err) => { if (!disposed) setError(pluginError(err)); });
    return () => { disposed = true; };
  }, [plugin.name, refresh]);
  // 显式重新读取才替换草稿, 运行状态轮询不参与配置初始化
  const readRuntime = useCallback(async (signal?: AbortSignal) => {
    const current = ++runtimeRequest.current;
    try {
      const result = await runtimeRead.read({ signal });
      if (current === runtimeRequest.current && !signal?.aborted) { setSnapshot(result); setRuntimeError(''); }
    } catch (error) {
      if (current !== runtimeRequest.current || signal?.aborted) return;
      setRuntimeError(pluginError(error));
      throw error;
    }
  }, [runtimeRead]);
  useEffect(() => {
    if (!application.view?.result) return;
    setRuntime(application.view.result.runtime); setError('');
    runtimeRequest.current++; runtimeRead.invalidate();
    void readRuntime().catch(() => undefined);
  }, [application.view?.result, readRuntime, runtimeRead]);
  useEffect(() => {
    const poll = startVisiblePoll(readRuntime, { interval: 5000 });
    return () => { runtimeRequest.current++; poll.stop(); };
  }, [readRuntime]);

  const selectLocation = (key: string) => {
    if (dirty && !confirmDiscard()) return;
    const next = data?.locations.find((item) => locationKey(item) === key);
    if (!next) return;
    setSelected(key);
    setDraft(locationState(next));
    setSaved(false);
    setError('');
  };

  const retry = async (allInstances = false) => {
    if (locked) return;
    setBusy(true); setError('');
    setRuntime([]);
    try {
      const scope = !allInstances && runtimeScope.current ? runtimeScope.current : { plugin_name: plugin.name };
      setRuntime((await application.apply(createPluginApplication(scope), 'plugins')).runtime);
    }
    catch (err) { if (!(err instanceof ApplicationUnconfirmedError && err.code === 'query_stopped')) setError(pluginError(err)); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (!location || !draft || locked) return;
    setBusy(true); setError(''); setSaved(false);
    application.clear(); setRuntime([]);
    let stored = false;
    try {
      const scope = { plugin_name: plugin.name, kind: location.kind, location_id: location.id };
      const operation = createPluginApplication(scope);
      const result = await controlApi.savePluginUsage(plugin.name, location, draft,
        { applyRuntime: false, baseUrl: operation.baseUrl, signal: application.getSignal() });
      stored = true;
      runtimeScope.current = scope;
      runtimeRequest.current++; runtimeRead.invalidate();
      setData(result);
      const updated = result.locations.find((item) => locationKey(item) === selected);
      if (updated) setDraft(locationState(updated));
      setSaved(true); setRuntime(result.runtime || []);
      onSaved();
      if (result.runtime === undefined) setRuntime((await application.apply(operation, 'plugins')).runtime);
      else await readRuntime().catch((err) => setRuntimeError(pluginError(err)));
    } catch (err) {
      if (!(err instanceof ApplicationUnconfirmedError && err.code === 'query_stopped')) setError((stored ? '使用配置已保存, 运行时应用未确认: ' : '') + pluginError(err));
    }
    finally { setBusy(false); }
  };
  const reread = () => {
    if (dirty && !confirmDiscard()) return;
    if (location) { setDraft(locationState(location)); setSaved(false); setError(''); }
    setRefresh((value) => value + 1);
  };

  const hasRuntimeIssue = Boolean(snapshot?.services.some((service) => service.instances.some((instance) =>
    instance.plugin.error || instance.plugin.drift || instance.plugin.restart_required)));

  if (!data && !error) {
    return (
      <Card padding="lg" className="space-y-3">
        <div className="flex items-center gap-2"><Skeleton className="h-8 w-8" /><Skeleton className="w-1/3" /></div>
        <Skeleton className="h-24 w-full" />
        <p role="status" className="text-[12px] text-fg-3">正在读取使用位置…</p>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card padding="lg" className="space-y-3">
        <div role="alert" className="tint-red flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)] px-3.5 py-2.5 text-[13px] text-fg-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 flex-none text-red" /><span className="min-w-0 break-words">{error}</span>
        </div>
        <Button onClick={() => setRefresh((value) => value + 1)}>重新读取</Button>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid items-start gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
        <Card padding="none" className="flex max-h-[calc(100vh-260px)] flex-col lg:sticky lg:top-0">
          <div className="space-y-2 p-3.5 pb-2">
            <div className="flex items-center gap-2">
              <h3 className="text-[13.5px] font-semibold text-fg">使用位置</h3>
              <span className="num rounded-full bg-[rgb(var(--rgb-teal)/0.12)] px-1.5 text-[11px] text-teal">{data.locations.filter((item) => item.present).length} 已添加</span>
            </div>
            <p className="text-[11.5px] leading-4 text-fg-3">插件在每个位置单独启用。关闭整个插件会保留各项能力的独立开关。</p>
            <select
              aria-label="使用位置"
              className="field h-9 w-full text-[12.5px]"
              value={selected}
              disabled={locked}
              onChange={(event) => selectLocation(event.target.value)}
            >
              {data.locations.map((item) => (
                <option key={locationKey(item)} value={locationKey(item)}>
                  {item.kind === 'chat' ? 'Chat' : `Edictum · ${item.label}`} · {item.present ? item.enabled ? '已启用' : '已添加，未启用' : '未添加'}
                </option>
              ))}
            </select>
          </div>
          <div className="scroll fade-edges min-h-0 flex-1 space-y-3 px-2 pb-3">
            {([['Chat', data.locations.filter((item) => item.kind === 'chat')], ['命名配置', data.locations.filter((item) => item.kind === 'edictum')]] as const).map(([groupLabel, locations]) => (
              <div key={groupLabel}>
                <div className="px-2 pb-1 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-fg-4">{groupLabel}</div>
                <div className="space-y-0.5">
                  {locations.map((item) => {
                    const key = locationKey(item);
                    const active = key === selected;
                    return (
                      <button
                        key={key}
                        type="button"
                        onClick={() => selectLocation(key)}
                        disabled={locked}
                        aria-current={active ? 'true' : undefined}
                        className={cn('relative flex w-full items-start gap-2.5 rounded-xl px-2 py-2 text-left transition-colors disabled:opacity-60', active ? 'bg-[rgb(var(--rgb-teal)/0.11)]' : 'hover:bg-glass-hover')}
                      >
                        {active && <span className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-[rgb(var(--rgb-teal))]" />}
                        <span className={cn('mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg', item.kind === 'chat' ? 'tint-accent bg-[rgb(var(--tint)/0.14)] text-tint' : 'tint-purple bg-[rgb(var(--tint)/0.14)] text-tint')}>
                          {item.kind === 'chat' ? <MessageSquare className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-1.5">
                            <span className="truncate text-[12.5px] font-medium text-fg">{item.kind === 'chat' ? 'Chat' : item.label}</span>
                            {item.availability.warnings?.length ? <AlertTriangle className="h-3 w-3 flex-none text-orange" /> : null}
                          </span>
                          <span className="block truncate font-mono text-[10.5px] text-fg-4">{item.kind === 'chat' ? 'chat' : item.id}</span>
                          <span className="mt-1 flex flex-wrap items-center gap-1.5">
                            <Badge tone={!item.present ? 'neutral' : item.enabled ? 'green' : 'orange'} className="!h-[17px] !px-1.5 !text-[10px]">
                              {!item.present ? '未添加' : item.enabled ? '已启用' : '已添加'}
                            </Badge>
                            <span className="num text-[10px] text-fg-4">{enabledCapCount(item.capabilities)}/{capCount(item.capabilities)} 能力</span>
                          </span>
                        </span>
                        <ChevronRight className={cn('mt-2 h-3.5 w-3.5 flex-none transition-transform', active ? 'text-teal' : 'text-fg-4')} />
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
          <div className="border-t border-line px-4 py-2.5 text-[11px] leading-4 text-fg-4">
            在群或 Chat 中修改插件参数后，新配置会在下一轮对话或下次激活时应用。
          </div>
        </Card>

        <div className="min-w-0 space-y-4">
          {location && draft ? (
            <>
              <Card padding="lg" tint="teal">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-fg-3">
                      <span>{location.kind === 'chat' ? 'Chat 位置' : '命名配置'}</span>
                      <ChevronRight className="h-3 w-3" />
                      <span className="font-mono">{location.id}</span>
                      <Badge tone="neutral" className="!h-[18px] !text-[10.5px]">rev {location.revision}</Badge>
                    </div>
                    <h2 className="mt-1 text-lg font-semibold text-fg">{location.kind === 'chat' ? 'Chat' : location.label}</h2>
                    <p className="mt-0.5 text-[12px] text-fg-3">
                      {location.present
                        ? location.enabled
                          ? '此处已启用，运行实例会按下方能力开关加载。'
                          : '此处已添加但未启用，能力开关会保留。'
                        : '此处尚未添加插件。添加后需要保存才会创建使用配置。'}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {draft.present ? (
                      <>
                        <label className="flex items-center gap-2 rounded-full border border-line bg-glass px-3 py-1 text-[12px] text-fg-2">
                          <input
                            aria-label="启用插件"
                            type="checkbox"
                            className="h-4 w-4 accent-[rgb(var(--rgb-teal))]"
                            checked={draft.enabled}
                            disabled={locked || (!location.availability.allowed && !draft.enabled)}
                            onChange={(event) => { setDraft({ ...draft, enabled: event.target.checked }); setSaved(false); }}
                          />
                          启用插件
                        </label>
                        <Button size="sm" variant="danger" disabled={locked} onClick={() => { setDraft({ ...draft, present: false }); setSaved(false); }}>移除</Button>
                      </>
                    ) : (
                      <TealPrimary size="sm" disabled={locked || !location.availability.allowed} onClick={() => { setDraft({ ...draft, present: true, enabled: true }); setSaved(false); }}>添加到此位置</TealPrimary>
                    )}
                  </div>
                </div>

                {!location.parent_enabled && (
                  <div className="tint-orange mt-3 flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.22)] bg-[rgb(var(--tint)/0.07)] px-3.5 py-2.5 text-[12px] text-fg-2">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-orange" />此命名配置已停用，运行实例还需启用对应会话。
                  </div>
                )}
                {location.availability.message && (
                  <div className={cn('mt-3 flex items-start gap-2 rounded-xl border px-3.5 py-2.5 text-[12px] text-fg-2',
                    location.availability.allowed ? 'border-line bg-glass' : 'tint-red border-[rgb(var(--tint)/0.22)] bg-[rgb(var(--tint)/0.07)]')}>
                    {location.availability.allowed ? <Server className="mt-0.5 h-3.5 w-3.5 flex-none text-fg-3" /> : <ShieldAlert className="mt-0.5 h-3.5 w-3.5 flex-none text-red" />}
                    {location.availability.message}
                  </div>
                )}
                {(location.availability.warnings ?? []).map((warning) => (
                  <div key={warning} className="tint-orange mt-2 flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.22)] bg-[rgb(var(--tint)/0.07)] px-3.5 py-2.5 text-[12px] text-fg-2">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-orange" />{warning}
                  </div>
                ))}

                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {location.kind === 'edictum' && (
                    <Link className="text-[12.5px] text-teal transition hover:underline" to={`/agents?edictum=${encodeURIComponent(location.id)}`}>打开命名配置参数</Link>
                  )}
                  {location.kind === 'chat' && (
                    <Link className="text-[12.5px] text-teal transition hover:underline" to="/chat">打开 Chat</Link>
                  )}
                  <span className="ml-auto text-[11px] text-fg-4">配置版本与运行实例状态分开保存</span>
                </div>
              </Card>

              {draft.present ? (
                <Card padding="lg">
                  <CardHeader
                    icon={<Wrench className="h-4 w-4" />}
                    title="能力开关"
                    description="能力声明来自插件元数据；这里控制当前使用位置是否启用某项能力。"
                    actions={<Badge tone={draft.enabled ? 'green' : 'orange'} dot>{draft.enabled ? '插件已启用' : '插件停用中'}</Badge>}
                  />
                  <CapabilityToggles
                    capabilities={capabilities}
                    values={draft.capabilities}
                    active={draft.enabled}
                    disabled={locked}
                    onChange={(kind, name, enabled) => { setDraft({ ...draft, capabilities: { ...draft.capabilities, [kind]: { ...draft.capabilities[kind], [name]: enabled } } }); setSaved(false); }}
                  />
                  {!draft.enabled && (
                    <p className="mt-3 flex items-center gap-2 text-[11.5px] text-orange"><AlertTriangle className="h-3.5 w-3.5" />插件停用中；能力开关会保留，重新启用后按这里的设置加载。</p>
                  )}
                  <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
                    <TealPrimary icon={<Save className="h-4 w-4" />} loading={busy} disabled={!dirty || locked} onClick={() => void save()}>{locked ? '处理中…' : '保存使用配置'}</TealPrimary>
                    <Button icon={<RefreshCw className="h-4 w-4" />} disabled={locked} onClick={reread}>重新读取</Button>
                    <span className="ml-auto flex items-center gap-2 text-[11.5px] text-fg-4">
                      {dirty && <><span className="dot tint-orange" data-live="1" />有未保存的修改</>}
                      {saved && !dirty && <><CheckCircle2 className="h-3.5 w-3.5 text-green" />使用配置已保存</>}
                    </span>
                  </div>
                </Card>
              ) : (
                <Card padding="lg">
                  <div className="flex flex-col items-center px-4 py-8 text-center">
                    <div className="grid h-12 w-12 place-items-center rounded-2xl border border-[rgb(var(--rgb-teal)/0.25)] bg-[rgb(var(--rgb-teal)/0.1)] text-teal"><Bot className="h-5 w-5" /></div>
                    <h3 className="mt-3 text-[14px] font-semibold text-fg">尚未添加到此位置</h3>
                    <p className="mt-1 max-w-sm text-[12.5px] leading-5 text-fg-3">
                      {location.availability.allowed ? '添加后可以按能力单独开关，并保存为独立的使用配置。' : location.availability.message ?? '此位置当前无法添加该插件。'}
                    </p>
                    <TealPrimary className="mt-4" disabled={!location.availability.allowed || locked} onClick={() => { setDraft({ ...draft, present: true, enabled: true }); setSaved(false); }}>添加此插件</TealPrimary>
                  </div>
                  <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-4">
                    <TealPrimary icon={<Save className="h-4 w-4" />} loading={busy} disabled={!dirty || locked} onClick={() => void save()}>{locked ? '处理中…' : '保存使用配置'}</TealPrimary>
                    <Button icon={<RefreshCw className="h-4 w-4" />} disabled={locked} onClick={reread}>重新读取</Button>
                    <span className="ml-auto flex items-center gap-2 text-[11.5px] text-fg-4">
                      {dirty && <><span className="dot tint-orange" data-live="1" />有未保存的修改</>}
                      {saved && !dirty && <><CheckCircle2 className="h-3.5 w-3.5 text-green" />使用配置已保存</>}
                    </span>
                  </div>
                </Card>
              )}

              {error && (
                <div role="alert" className="tint-red flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)] px-3.5 py-2.5 text-[13px] text-fg-2">
                  <AlertTriangle className="mt-0.5 h-4 w-4 flex-none text-red" /><span className="min-w-0 break-words">{error}</span>
                </div>
              )}
            </>
          ) : (
            <Card padding="lg"><p className="py-10 text-center text-[13px] text-fg-3">请选择一个使用位置</p></Card>
          )}
        </div>
      </div>

      {runtime.length > 0 && <PluginRuntimeStatus results={runtime} onRetry={() => void retry()} busy={locked} />}

      {application.view && (
        <RuntimeApplicationProgress
          id={application.view.application.id}
          label={application.view.application.label}
          snapshot={application.view.snapshot}
          resultOk={application.view.result?.ok}
          busy={application.view.busy}
          error={application.view.error}
          onQuery={() => void application.query()}
          completionText="运行应用已结束"
        />
      )}

      <Card padding="lg">
        <CardHeader
          icon={<Server className="h-4 w-4" />}
          title="运行实例"
          description="按服务与活动会话展示插件实际加载状态。"
          actions={<Button size="sm" icon={<RefreshCw className={cn('h-3.5 w-3.5', locked && 'animate-spin-slow')} />} disabled={locked} onClick={() => void readRuntime().catch((err) => setRuntimeError(pluginError(err)))}>刷新运行状态</Button>}
        />
        {runtimeError && <p role="alert" className="mb-2 text-[12.5px] text-red">{runtimeError}</p>}
        {!snapshot && !runtimeError && <p role="status" className="text-[12px] text-fg-3">正在读取运行状态…</p>}
        {snapshot?.services.map((service) => (
          <section key={service.target} className="mb-4 space-y-2 last:mb-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-[13px] font-semibold text-fg">{service.target}</h3>
              <Badge tone={service.status === 'available' ? 'green' : service.status === 'stopped' ? 'neutral' : 'red'} dot>
                {service.status === 'available' ? '服务可用' : service.status === 'stopped' ? '服务未运行' : '读取失败'}
              </Badge>
            </div>
            {service.status !== 'available' ? (
              <p className={cn('rounded-xl border px-3.5 py-3 text-[12px]', service.status === 'stopped' ? 'border-dashed border-line text-fg-3' : 'tint-red border-[rgb(var(--tint)/0.22)] bg-[rgb(var(--tint)/0.07)] text-fg-2')}>
                {service.status === 'stopped' ? '服务未运行，启动后查看实例' : `读取失败：${service.error || '未知错误'}`}
              </p>
            ) : service.instances.length === 0 ? (
              <p className="rounded-xl border border-dashed border-line px-3.5 py-3 text-[12px] text-fg-3">暂无加载此插件的活动实例，新实例激活时按已保存配置加载</p>
            ) : (
              <div className="grid gap-2.5 xl:grid-cols-2">
                {service.instances.map((instance) => {
                  const label = instance.plugin.restart_required ? '需要重新激活' : instance.plugin.drift ? '配置与运行状态不同步' : instance.plugin.status === 'loaded' ? instance.plugin.enabled ? '已加载并启用' : '已加载，未启用' : instance.plugin.status === 'error' ? '加载失败' : instance.plugin.status === 'disabled' ? '已停用' : '待应用';
                  const tone = instance.plugin.error || instance.plugin.status === 'error' ? 'red' : instance.plugin.restart_required || instance.plugin.drift ? 'orange' : instance.plugin.status === 'loaded' && instance.plugin.enabled ? 'green' : 'neutral';
                  return (
                    <div key={`${instance.platform_id}:${instance.session_id}`} className="space-y-3 rounded-xl border border-line bg-glass p-3.5">
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="break-all font-mono text-[12.5px] font-medium text-fg">{instance.platform_id} / {instance.session_id}</span>
                            <Badge tone="neutral">位置 {instance.location_id}</Badge>
                          </div>
                          <p className="mt-1 flex items-center gap-1.5 text-[11.5px]">
                            <StatusDot tone={tone} live={tone === 'green'} className="!h-1.5 !w-1.5" />
                            <span className={tone === 'red' ? 'text-red' : tone === 'orange' ? 'text-orange' : tone === 'green' ? 'text-green' : 'text-fg-3'}>
                              {label}{instance.plugin.error && ` · ${instance.plugin.error}`}
                            </span>
                          </p>
                        </div>
                      </div>
                      <div className="space-y-1 rounded-lg bg-[rgb(var(--tint)/0.04)] px-2.5 py-2">
                        {Object.entries(PLUGIN_CAPABILITY_LABELS).map(([kind, kindLabel]) => (
                          <p key={kind} className="break-words text-[11px] leading-4 text-fg-3">
                            {kindLabel}：{instance.plugin.capabilities.loaded_known === false ? '未提供加载明细'
                              : instance.plugin.capabilities.effective?.[kind]?.map((item) => `${item.name} (期望${item.requested_enabled ? '启用' : '停用'}; ${capabilityStatus(item)})`).join('、')
                              || Object.entries(instance.plugin.capabilities.loaded?.[kind] || {}).map(([name, enabled]) => `${name} (独立开关${enabled ? '启用' : '停用'})`).join('、')
                              || '未加载'}
                          </p>
                        ))}
                      </div>
                      <Button size="sm" onClick={() => setSessionContext({ platformId: instance.platform_id, sessionId: instance.session_id })}>配置此会话参数</Button>
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        ))}
        {hasRuntimeIssue && !locked && (
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-glass px-3.5 py-2.5">
            <p className="flex items-center gap-2 text-[12px] text-orange"><AlertTriangle className="h-3.5 w-3.5" />检测到实例配置不同步或加载失败</p>
            <Button size="sm" disabled={locked} onClick={() => void retry(true)}>重试实例应用</Button>
          </div>
        )}
      </Card>

      {error && !location && <p role="alert" className="text-[13px] text-red">{error}</p>}

      <SessionSettingsDrawer context={sessionContext} plugins={[plugin.name]} onClose={() => setSessionContext(null)} />
    </div>
  );
}
