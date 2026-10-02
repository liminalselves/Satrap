import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { controlApi } from '@/api/control';
import type { ManagedPlugin, PluginLocation, PluginLocationState, PluginRuntimeResult, PluginRuntimeSnapshot, PluginUsagesResult } from '@/api/types';
import { PluginCapabilities, PLUGIN_CAPABILITY_LABELS } from '@/components/common/PluginCapabilities';
import { SessionPluginSettingsModal } from '@/components/common/SessionPluginSettingsModal';
import type { PluginSettingsContext } from '@/api/pluginSettings';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { PluginRuntimeStatus } from './GlobalPluginSettings';
import { pluginError } from './InstallPluginModal';

function locationState(location: PluginLocation): PluginLocationState {
  return { present: location.present, enabled: location.enabled, capabilities: location.capabilities };
}

const locationKey = (location: PluginLocation) => `${location.kind}:${location.id}`;

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
  const [sessionContext, setSessionContext] = useState<PluginSettingsContext | null>(null);
  const location = data?.locations.find((item) => locationKey(item) === selected);
  const dirty = !!location && !!draft && JSON.stringify(locationState(location)) !== JSON.stringify(draft);
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
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
  const readRuntime = useCallback(async () => {
    const result = await controlApi.getPluginRuntime(plugin.name);
    setSnapshot(result); setRuntimeError('');
  }, [plugin.name]);
  useEffect(() => {
    let disposed = false;
    let pending = false;
    const poll = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await controlApi.getPluginRuntime(plugin.name);
        if (!disposed) { setSnapshot(result); setRuntimeError(''); }
      } catch (err) { if (!disposed) setRuntimeError(pluginError(err)); }
      finally { pending = false; }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [plugin.name]);
  const retry = async () => {
    setBusy(true); setError('');
    try { setRuntime((await controlApi.reconcilePlugins()).runtime); await readRuntime(); }
    catch (err) { setError(pluginError(err)); }
    finally { setBusy(false); }
  };
  const save = async () => {
    if (!location || !draft) return;
    setBusy(true); setError(''); setSaved(false);
    try {
      const result = await controlApi.savePluginUsage(plugin.name, location, draft);
      setData(result);
      const updated = result.locations.find((item) => locationKey(item) === selected);
      if (updated) setDraft(locationState(updated));
      setSaved(true); setRuntime(result.runtime || []);
      onSaved();
      await readRuntime().catch((err) => setRuntimeError(pluginError(err)));
    } catch (err) { setError(pluginError(err)); }
    finally { setBusy(false); }
  };
  return <div className="space-y-4">
    <Card className="space-y-4">
      <p className="text-sm text-text-secondary">插件在每个使用位置分别启用。关闭整个插件会保留各项能力的独立开关。</p>
      {error && <p role="alert" className="text-error">{error}</p>}
      {data && <label className="block text-sm">使用位置<select aria-label="使用位置" className="glass-input mt-2 w-full" value={selected} disabled={busy} onChange={(event) => {
        if (dirty && !confirmDiscard()) return;
        const next = data.locations.find((item) => locationKey(item) === event.target.value);
        if (next) { setSelected(locationKey(next)); setDraft(locationState(next)); setSaved(false); setError(''); }
      }}>{data.locations.map((item) => <option key={locationKey(item)} value={locationKey(item)}>{item.kind === 'chat' ? 'Chat' : `Edictum · ${item.label}`} · {item.present ? item.enabled ? '已启用' : '已添加，未启用' : '未添加'}</option>)}</select></label>}
      {location && draft && <>
        {location.availability.message && <p className="text-sm text-text-tertiary">{location.availability.message}</p>}
        {!location.parent_enabled && <p className="text-sm text-text-tertiary">此命名配置已停用，运行实例还需启用对应会话。</p>}
        <div className="flex flex-wrap items-center gap-3">
          {draft.present ? <><label className="flex items-center gap-2 text-sm"><input aria-label="启用插件" type="checkbox" checked={draft.enabled} disabled={busy || (!location.availability.allowed && !draft.enabled)} onChange={(event) => { setDraft({ ...draft, enabled: event.target.checked }); setSaved(false); }} />启用插件</label><Button size="sm" variant="danger" disabled={busy} onClick={() => { setDraft({ ...draft, present: false }); setSaved(false); }}>移除</Button></> : <Button disabled={busy || !location.availability.allowed} onClick={() => { setDraft({ ...draft, present: true, enabled: true }); setSaved(false); }}>添加到此位置</Button>}
          {location.kind === 'edictum' && <Link className="text-sm text-accent" to={`/sessions?edictum=${encodeURIComponent(location.id)}`}>打开命名配置参数</Link>}
          {location.kind === 'chat' && <Link className="text-sm text-accent" to="/chat">打开 Chat</Link>}
        </div>
        {draft.present && <PluginCapabilities capabilities={plugin.capabilities} values={draft.capabilities} active={draft.enabled} disabled={busy} onChange={(kind, name, enabled) => { setDraft({ ...draft, capabilities: { ...draft.capabilities, [kind]: { ...draft.capabilities[kind], [name]: enabled } } }); setSaved(false); }} />}
        <div className="flex flex-wrap gap-2"><Button variant="primary" disabled={!dirty || busy} onClick={() => void save()}>{busy ? '处理中…' : '保存使用配置'}</Button><Button disabled={busy} onClick={() => { if (!dirty || confirmDiscard()) { setSaved(false); setRefresh((value) => value + 1); } }}>重新读取</Button></div>
        {dirty && <p className="text-xs text-text-tertiary">有未保存的修改</p>}
      </>}
      {!data && <Button onClick={() => setRefresh((value) => value + 1)} disabled={busy}>重新读取</Button>}
      {saved && <p role="status" className="text-success">使用配置已保存</p>}
      {runtime.length > 0 && <PluginRuntimeStatus results={runtime} onRetry={() => void retry()} busy={busy} />}
    </Card>
    <Card className="space-y-4">
      <div className="flex items-center justify-between gap-2"><h2 className="text-lg font-semibold">运行实例</h2><Button size="sm" onClick={() => void readRuntime().catch((err) => setRuntimeError(pluginError(err)))}>刷新运行状态</Button></div>
      {runtimeError && <p role="alert" className="text-error">{runtimeError}</p>}
      {!snapshot && !runtimeError && <p role="status">正在读取运行状态…</p>}
      {snapshot?.services.map((service) => <section key={service.target} className="space-y-2">
        <h3 className="font-medium">{service.target}</h3>
        {service.status !== 'available' ? <p className="text-sm text-text-tertiary">{service.status === 'stopped' ? '服务未运行，启动后查看实例' : `读取失败：${service.error || '未知错误'}`}</p> : service.instances.length === 0 ? <p className="text-sm text-text-tertiary">暂无加载此插件的活动实例，新实例激活时按已保存配置加载</p> : service.instances.map((instance) => <div key={`${instance.platform_id}:${instance.session_id}`} className="space-y-2 rounded-lg bg-glass p-3">
          <p className="break-all text-sm">{instance.platform_id} / {instance.session_id} · {instance.location_id}</p>
          <p className={instance.plugin.error ? 'text-sm text-error' : 'text-sm text-text-secondary'}>{instance.plugin.restart_required ? '需要重新激活' : instance.plugin.drift ? '配置与运行状态不同步' : instance.plugin.status === 'loaded' ? instance.plugin.enabled ? '已加载并启用' : '已加载，未启用' : instance.plugin.status === 'error' ? '加载失败' : instance.plugin.status === 'disabled' ? '已停用' : '待应用'}{instance.plugin.error && ` · ${instance.plugin.error}`}</p>
          <div className="space-y-1 text-xs text-text-secondary">{Object.entries(PLUGIN_CAPABILITY_LABELS).map(([kind, label]) => <p key={kind}>{label}：{instance.plugin.capabilities.loaded_known === false ? '未提供加载明细' : Object.entries(instance.plugin.capabilities.loaded?.[kind] || {}).map(([name, enabled]) => `${name} (${instance.plugin.enabled && enabled ? '启用' : '停用'})`).join('、') || '未加载'}</p>)}</div>
          <Button size="sm" onClick={() => setSessionContext({ platformId: instance.platform_id, sessionId: instance.session_id })}>配置此会话参数</Button>
        </div>)}
      </section>)}
      {snapshot?.services.some((service) => service.instances.some((instance) => instance.plugin.error || instance.plugin.drift || instance.plugin.restart_required)) && <Button disabled={busy} onClick={() => void retry()}>重试实例应用</Button>}
    </Card>
    <SessionPluginSettingsModal context={sessionContext} plugins={[plugin.name]} onClose={() => setSessionContext(null)} />
  </div>;
}
