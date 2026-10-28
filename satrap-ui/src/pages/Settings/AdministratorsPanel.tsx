import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ChevronRight, GitCompare, Globe, ListChecks, Plus, RefreshCw, Save, ShieldCheck,
  ShieldX, Trash2, UserCog, UserRound, Users,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { createUuid } from '@/utils/uuid';
import {
  administratorsApi, administratorError, administratorRuntimeLabel,
  type AdministratorChatRebind, type AdministratorGroup, type AdministratorOverride,
  type AdministratorOverrideRebind, type AdministratorPreview, type AdministratorRebind,
  type AdministratorPlugin, type AdministratorSnapshot,
} from '@/api/administrators';
import { getControlApiUrl } from '@/utils/constants';
import { Badge, Button, Drawer, Field, Input, Skeleton, toast } from '@/components/kit';
import { Banner, SectionCard } from './shared';
import { ACTIVATION_REFRESH_MS } from './useActivationLoad';
import { Select as KitSelect } from '@/components/kit/Select';

const selectClass = 'field w-full appearance-none';

const TITLES = {
  allowGroup: '已允许的插件',
  legacyExcluded: '旧版排除',
  excluded: '已排除的插件',
  allowOverride: '额外允许的插件',
  denyOverride: '否决的插件',
} as const;

function pluginBadges(plugin: AdministratorPlugin | undefined, platformIds: string[]): string {
  if (!plugin) return ' · 暂不可用';
  return [!plugin.supports_administrators ? ' · 未接入' : '',
    plugin.enabled === false ? ' · 未启用' : '',
    platformIds.length > 0 && plugin.loaded_platforms && !platformIds.some((id) => plugin.loaded_platforms!.includes(id)) ? ' · 平台不适用' : ''].join('');
}

/** 旧版或异常响应不具备目录结构时不要覆盖当前草稿 */
function isAdministratorSnapshot(value: unknown): value is AdministratorSnapshot {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<AdministratorSnapshot>;
  return typeof item.revision === 'string' && Array.isArray(item.groups) && Array.isArray(item.overrides)
    && Array.isArray(item.platforms) && Array.isArray(item.plugins);
}

/** 插件名单区块: 标题、逐行插件卡片与可选的添加选择器 */
function PluginListSection({ title, hint, names, snapshot, addLabel, taken, platformIds = [], onAdd, onRemove }: {
  title: string;
  hint?: string;
  names: string[];
  snapshot: AdministratorSnapshot | null;
  addLabel?: string;
  taken?: string[];
  platformIds?: string[];
  onAdd?: (name: string) => void;
  onRemove: (name: string) => void;
}) {
  const options = (snapshot?.plugins || []).filter((item) => item.supports_administrators && item.enabled !== false
    && !(taken || names).includes(item.name)
    && (platformIds.length === 0 || !item.loaded_platforms || item.loaded_platforms.some((id) => platformIds.includes(id))));
  return <div className="space-y-2" aria-label={title}>
    <div>
      <p className="text-[13px] font-medium text-fg">{title}</p>
      {hint && <p className="mt-0.5 text-[12px] leading-5 text-fg-3">{hint}</p>}
    </div>
    {names.length === 0 && <p className="rounded-lg border border-dashed border-line px-3 py-2.5 text-[12px] text-fg-4">尚未添加</p>}
    {names.map((name) => {
      const plugin = snapshot?.plugins.find((item) => item.name === name);
      return <div key={name} className="rounded-xl border border-line bg-glass p-3 text-sm">
        <div className="flex flex-wrap items-center gap-1.5">
          <p className="font-mono text-[12.5px] font-medium text-fg">{name}{pluginBadges(plugin, platformIds)}</p>
          <Button size="sm" variant="ghost" className="ml-auto !h-7" aria-label={`移除 ${name}`} onClick={() => onRemove(name)}>移除</Button>
        </div>
        {plugin?.description && <p className="mt-0.5 text-[11.5px] text-fg-3">{plugin.description}</p>}
        {plugin?.supports_administrators && (
          <ul className="mt-1 space-y-1 text-[11.5px] leading-5 text-fg-3">
            {Object.entries(plugin.management_permissions).filter(([, rule]) => rule.system_admin).map(([id, rule]) => <li key={id}>
              · {rule.description}
              {!!rule.requirements?.length && <p className="mt-0.5 text-[11px] text-fg-4">{rule.requirements.join('；')}</p>}
            </li>)}
          </ul>
        )}
      </div>;
    })}
    {addLabel && onAdd && (
      options.length > 0
        ? <KitSelect aria-label={addLabel} className={selectClass} value=""
          onChange={(value) => { if (value) onAdd(value); }}>
          <option value="">{addLabel}…</option>
          {options.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
        </KitSelect>
        : <p className="text-[11.5px] text-fg-4">没有可添加的插件 (需已接入、已启用且适用于当前平台)</p>
    )}
  </div>;
}

function PlatformSelect({ value, platforms, onChange, label }: {
  value: string; platforms: AdministratorSnapshot['platforms']; onChange: (value: string) => void; label: string;
}) {
  const platform = platforms.find((item) => item.id === value);
  return <KitSelect aria-label={label} className={selectClass} value={value} onChange={(value) => onChange(value)}>
    <option value="">选择已配置的平台</option>
    {!platform && value && <option value={value}>{value} · 平台已移除</option>}
    {platforms.map((item) => <option key={item.id} value={item.id}>{item.name || item.id} · {item.type}{item.enabled ? '' : ' · 已停用'}</option>)}
  </KitSelect>;
}

function PreviewRow({ plugin }: { plugin: AdministratorPreview['members'][number]['plugins'][number] }) {
  return (
    <li className="rounded-lg border border-line bg-glass px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className={cn('font-mono text-[12px] font-medium', plugin.allowed ? 'text-fg' : 'text-fg-3 line-through')}>{plugin.name}</span>
        <Badge tone={plugin.allowed ? 'green' : 'red'}>{plugin.allowed ? '有效' : '无效'}</Badge>
        <span className="text-[11.5px] text-fg-3">{plugin.permissions.map((item) => item.description).join('、') || '无系统管理权限'}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-3 text-[11px] text-fg-4">
        <span>允许来源: {plugin.sources?.allow.join(', ') || '无'}</span>
        {!!plugin.sources?.deny.length && <span className="text-red">否决来源: {plugin.sources.deny.join(', ')}</span>}
      </div>
    </li>
  );
}

interface PanelProps {
  i: number;
  active: boolean;
  backendRunning: boolean;
  onSaved?: (groups: AdministratorGroup[], overrides: AdministratorOverride[], revision: string, previousRevision: string) => void;
  onDirty: (dirty: boolean) => void;
  register: (actions: { save: () => Promise<boolean>; undo: () => void }) => void;
}

export function AdministratorsPanel({ i, active, backendRunning, onSaved, onDirty, register }: PanelProps) {
  const [snapshot, setSnapshot] = useState<AdministratorSnapshot | null>(null);
  const [groups, setGroups] = useState<AdministratorGroup[]>([]);
  const [overrides, setOverrides] = useState<AdministratorOverride[]>([]);
  const [rebind, setRebind] = useState<AdministratorRebind[]>([]);
  const [rebindChats, setRebindChats] = useState<AdministratorChatRebind[]>([]);
  const [rebindOverrides, setRebindOverrides] = useState<AdministratorOverrideRebind[]>([]);
  const [preview, setPreview] = useState<AdministratorPreview | null>(null);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [comparison, setComparison] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [checkingRuntime, setCheckingRuntime] = useState(false);
  const readController = useRef<AbortController | null>(null);
  const [openGroup, setOpenGroup] = useState<string | null>(null);
  const [openOverride, setOpenOverride] = useState<string | null>(null);

  const dirty = snapshot !== null
    && (JSON.stringify(groups) !== JSON.stringify(snapshot.groups)
      || JSON.stringify(overrides) !== JSON.stringify(snapshot.overrides)
      || rebind.length > 0 || rebindOverrides.length > 0 || rebindChats.length > 0);
  const dirtyRef = useRef(dirty);
  const busyRef = useRef(busy);
  const requests = useRef(0);
  dirtyRef.current = dirty;
  busyRef.current = busy;

  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  const currentRebind = rebind.filter((item) => groups.some((group) => group.id === item.group_id
    && group.members.some((member) => member.platform_id === item.platform_id && member.user_id === item.user_id)));
  const currentRebindChats = rebindChats.filter((item) => groups.some((group) => group.id === item.group_id
    && group.chats?.some((chat) => chat.platform_id === item.platform_id && chat.chat_id === item.chat_id)));
  const currentRebindOverrides = rebindOverrides.filter((item) => overrides.some((entry) => entry.id === item.override_id
    && entry.platform_id === item.platform_id && entry.user_id === item.user_id));

  const load = useCallback(async () => {
    if (busyRef.current) return;
    const request = ++requests.current;
    const source = getControlApiUrl();
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    setLoading(true);
    setCheckingRuntime(false);
    try {
      const value = await administratorsApi.read(controller.signal, source);
      if (request !== requests.current || source !== getControlApiUrl() || controller.signal.aborted) return;
      if (!isAdministratorSnapshot(value)) {
        setLoadError('管理员目录响应格式无效');
        setLoading(false);
        setCheckingRuntime(false);
        return;
      }
      if (dirtyRef.current) {
        setSnapshot((previous) => previous ? { ...previous, platforms: value.platforms, plugins: value.plugins, plugin_errors: value.plugin_errors, runtime: value.runtime } : value);
      } else {
        setSnapshot(value);
        setGroups(value.groups);
        setOverrides(value.overrides);
        setRebind([]);
        setRebindOverrides([]);
        setRebindChats([]);
        setConflict(false);
        setComparison(false);
      }
      setError('');
      setLoadError('');
      setLoading(false);
      setCheckingRuntime(true);
      const confirmed = await administratorsApi.runtime(controller.signal, source);
      if (request !== requests.current || source !== getControlApiUrl() || controller.signal.aborted) return;
      if (confirmed) setSnapshot((current) => current && current.section_revision === value.section_revision
        ? { ...current, runtime: confirmed.section_revision === value.section_revision ? confirmed.runtime
          : { status: 'unconfirmed', error: '管理员配置已变化, 请重新读取后确认' } } : current);
    } catch (cause) {
      if (request === requests.current && source === getControlApiUrl() && !controller.signal.aborted) setLoadError(administratorError(cause));
    } finally {
      if (request === requests.current) { setLoading(false); setCheckingRuntime(false); }
    }
  }, []);

  // 滚动经过分节会反复激活; 刚读取过的数据直接沿用, 不重复请求
  const loadedAt = useRef(0);
  useEffect(() => {
    if (snapshot) loadedAt.current = Date.now();
  }, [snapshot]);
  useEffect(() => {
    if (active && Date.now() - loadedAt.current > ACTIVATION_REFRESH_MS) void load();
    else if (!active) { setCheckingRuntime(false); setLoading(false); }
    return () => { requests.current++; readController.current?.abort(); };
  }, [active, load]);
  useEffect(() => () => { requests.current++; }, []);

  const change = (id: string, update: (group: AdministratorGroup) => AdministratorGroup) => {
    setGroups((current) => current.map((group) => group.id === id ? update(group) : group));
    setPreview(null);
  };

  const changeOverride = (id: string, update: (entry: AdministratorOverride) => AdministratorOverride) => {
    setOverrides((current) => current.map((entry) => entry.id === id ? update(entry) : entry));
    setPreview(null);
  };

  const save = async (): Promise<boolean> => {
    if (!snapshot || busyRef.current) return false;
    requests.current++;
    readController.current?.abort();
    setCheckingRuntime(false);
    busyRef.current = true;
    setBusy(true);
    setLoading(false);
    setError('');
    setLoadError('');
    try {
      const value = await administratorsApi.save(groups, overrides, snapshot.revision, currentRebind, currentRebindOverrides, currentRebindChats);
      setSnapshot(value);
      setGroups(value.groups);
      setOverrides(value.overrides);
      setRebind([]);
      setRebindOverrides([]);
      setRebindChats([]);
      setConflict(false);
      setComparison(false);
      setPreview(null);
      onSaved?.(value.groups, value.overrides, value.revision, snapshot.revision);
      if (value.runtime.status === 'applied') toast.success(administratorRuntimeLabel(value.runtime));
      else toast.warning(administratorRuntimeLabel(value.runtime));
      return true;
    } catch (cause) {
      setError(administratorError(cause));
      setConflict(typeof cause === 'object' && cause !== null && 'response' in cause && (cause.response as { status?: number })?.status === 409);
      return false;
    } finally {
      setBusy(false);
      busyRef.current = false;
    }
  };

  const run = async (action: 'preview' | 'apply' | 'rebase') => {
    if (!snapshot || busyRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    requests.current++;
    readController.current?.abort();
    setCheckingRuntime(false);
    setError('');
    try {
      if (action === 'preview') setPreview(await administratorsApi.preview(groups, overrides, currentRebind, currentRebindOverrides, currentRebindChats));
      else if (action === 'apply') {
        const value = await administratorsApi.apply(snapshot.section_revision);
        setSnapshot((current) => current && { ...current, runtime: value.runtime });
        if (value.runtime.status === 'applied') toast.success(administratorRuntimeLabel(value.runtime));
        else toast.warning(administratorRuntimeLabel(value.runtime));
      } else {
        const value = await administratorsApi.read();
        setSnapshot(value);
        setConflict(false);
        setComparison(true);
        toast.warning('已保留草稿并读取最新版本, 请比较管理组与成员例外后再保存');
      }
      return true;
    } catch (cause) {
      setError(administratorError(cause));
      return false;
    } finally {
      setBusy(false);
      busyRef.current = false;
      setLoading(false);
    }
  };

  const undo = useCallback(() => {
    if (!snapshot) return;
    setGroups(snapshot.groups);
    setOverrides(snapshot.overrides);
    setRebind([]);
    setRebindChats([]);
    setRebindOverrides([]);
    setConflict(false);
    setComparison(false);
    setPreview(null);
    setError('');
    toast.success('已放弃管理员设置的修改');
  }, [snapshot]);

  useEffect(() => { const actions = { save, undo }; register(actions); });

  const addGroup = () => {
    const id = createUuid();
    setGroups((current) => [...current, { id, name: '', enabled: true, protect: true, members: [], chats: [], plugin_scope: { mode: 'selected', included: [], excluded: [] } }]);
    setOpenGroup(id);
    setPreview(null);
  };

  const addOverride = () => {
    const id = createUuid();
    setOverrides((current) => [...current, { id, enabled: true, protect: false, platform_id: '', user_id: '', allow: [], deny: [] }]);
    setOpenOverride(id);
    setPreview(null);
  };

  const editingGroup = groups.find((group) => group.id === openGroup) ?? null;
  const editingOverride = overrides.find((entry) => entry.id === openOverride) ?? null;
  const runtime = snapshot?.runtime;
  const invalidCount = groups.reduce((sum, group) => sum
    + group.members.filter((member) => {
      const platform = snapshot?.platforms.find((item) => item.id === member.platform_id);
      return Boolean(member.platform_instance_id) && platform?.instance_id !== member.platform_instance_id;
    }).length
    + (group.chats ?? []).filter((chat) => {
      const platform = snapshot?.platforms.find((item) => item.id === chat.platform_id);
      return Boolean(chat.platform_instance_id) && platform?.instance_id !== chat.platform_instance_id;
    }).length, 0)
    + overrides.filter((entry) => {
      const platform = snapshot?.platforms.find((item) => item.id === entry.platform_id);
      return Boolean(entry.platform_instance_id) && platform?.instance_id !== entry.platform_instance_id;
    }).length;

  return (
    <SectionCard id="admins" i={i} tint="teal" icon={<Users className="h-4 w-4" />}
      title="管理员" description="管理员身份来自真实平台消息。授权插件后可免填该插件的调用者名单, 功能开关、操作范围和审批仍然生效">
      <div className="space-y-4" aria-label="管理员设置">
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="ghost" icon={<RefreshCw className={cn('h-4 w-4', (loading || checkingRuntime) && 'animate-spin-slow')} />}
            onClick={() => void load()} disabled={busy || loading}>刷新目录与状态</Button>
          <Button size="sm" variant="tinted" tint="teal" icon={<Plus className="h-4 w-4" />} onClick={addGroup} disabled={!snapshot || busy || loading}>新增管理组</Button>
          <Button size="sm" variant="tinted" tint="accent" icon={<Plus className="h-4 w-4" />} onClick={addOverride} disabled={!snapshot || busy || loading}>新增成员例外</Button>
          <Button size="sm" icon={<ListChecks className="h-4 w-4" />} onClick={() => void run('preview')} disabled={!snapshot || busy || loading}>预览有效权限</Button>
          <Button size="sm" variant="primary" icon={<Save className="h-4 w-4" />} loading={busy} disabled={!snapshot || !dirty || busy} onClick={() => void save()}>保存并应用</Button>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-line bg-glass px-3.5 py-2.5 text-[11.5px] text-fg-3">
          <span className="num">修订 <code className="font-mono text-fg-2">{snapshot?.revision || '—'}</code></span>
          <span>管理组 <span className="num font-medium text-fg-2">{groups.length}</span></span>
          <span>成员例外 <span className="num font-medium text-fg-2">{overrides.length}</span></span>
          <span>已接入插件 <span className="num font-medium text-fg-2">{snapshot?.plugins.filter((plugin) => plugin.supports_administrators && plugin.enabled !== false).length ?? 0}</span></span>
          {invalidCount > 0 && <Badge tone="orange" dot>{invalidCount} 个失效身份</Badge>}
          {!backendRunning && <Badge tone="orange" dot>后端未运行 · 保存后下次启动生效</Badge>}
          {checkingRuntime && <span className="ml-auto animate-pulse">正在确认管理员运行状态…</span>}
        </div>

        {loading && !snapshot && (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="rounded-xl border border-line bg-glass p-4">
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="mt-2 h-3 w-1/2" />
                <Skeleton className="mt-4 h-5 w-3/4" />
              </div>
            ))}
          </div>
        )}

        {loadError && <Banner tone="error" title="读取管理员设置失败">{loadError}</Banner>}
        {error && !conflict && <Banner tone="error">{error}</Banner>}

        {!!snapshot?.plugin_errors?.length && (
          <Banner tone="warning" title="以下插件无法加载, 不会授予管理员权限">
            <ul className="space-y-0.5">
              {snapshot.plugin_errors.map((item, index) => <li key={`${item.name}-${index}`}>{item.name}: {item.error}</li>)}
            </ul>
          </Banner>
        )}

        {snapshot?.migrated_from_legacy && (
          <Banner tone="info" title="已迁移旧版“指定插件”模式的组排除">
            <p>已将旧版“指定插件”模式下的组排除迁移为成员例外, 保存后生效。</p>
            <p className="text-[12px] text-fg-3">迁移按当前成员展开, 之后新加入该组的成员不会自动继承这条否决。</p>
          </Banner>
        )}

        {!!snapshot?.migration_pending?.length && (
          <Banner tone="warning" title={`${snapshot.migration_pending.length} 个组的旧排除无法安全展开`}>
            <p>{snapshot.migration_pending.length} 个组的旧排除无法安全展开为成员例外, 未自动迁移; 请检查群聊授权或对应例外条目的启用状态。</p>
            <p className="text-[12px] text-fg-3">涉及管理组: {snapshot.migration_pending.join(', ')}。未迁移期间这些旧排除仍按原规则生效, 可原样保留。</p>
          </Banner>
        )}

        {snapshot && runtime && (
          <Banner tone={runtime.status === 'unconfirmed' ? 'error' : 'info'}
            actions={runtime.status === 'unconfirmed' && !dirty
              ? <Button size="sm" variant="ghost" disabled={busy || dirty || loading} onClick={() => void run('apply')}>重试应用已保存配置</Button>
              : undefined}>
            {administratorRuntimeLabel(runtime)}{dirty ? ' · 当前修改尚未保存' : ''}
            {runtime.error && <p className="mt-0.5 text-[12px] text-fg-3">{runtime.error}</p>}
          </Banner>
        )}
        {snapshot && !runtime && (
          <Banner tone="info">尚未配置管理员运行状态。</Banner>
        )}

        {(conflict || comparison) && (
          <Banner tone={conflict ? 'error' : 'warning'}
            title={conflict ? '配置已被其他操作修改, 草稿已保留' : undefined}
            actions={<>
              {conflict && <Button size="sm" variant="ghost" onClick={() => void run('rebase')} disabled={busy}>保留草稿并读取最新版本</Button>}
            </>}>
            {conflict ? (
              <p>配置已被其他操作修改, 草稿已保留。请读取最新版本, 比较差异后再保存。</p>
            ) : (
              <p>已读取最新版本, 以下显示已保存的配置。编辑区仍是你的草稿, 请比较后再保存。</p>
            )}
            <details open={comparison} className="mt-2 rounded-lg border border-line bg-glass px-3 py-2">
              <summary className="cursor-pointer list-none text-[12px] font-medium text-fg-2 marker:hidden">
                <GitCompare className="mr-1 inline h-3.5 w-3.5" />最近读取的管理组与成员例外
              </summary>
              <div className="mt-2 grid gap-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-fg-4">管理组</p>
                  {snapshot?.groups.map((group) => (
                    <div key={group.id} className="text-[11.5px] leading-5 text-fg-2">
                      <p className="font-medium">{group.name} · {group.enabled ? '启用' : '停用'} · {group.protect === false ? '不保护账号' : '保护账号'}</p>
                      <p className="text-fg-3">成员: {group.members.map((member) => `${member.platform_id} / ${member.user_id}`).join(', ') || '无'}</p>
                      <p className="text-fg-3">授权群聊: {group.chats?.map((chat) => `${chat.platform_id} / ${chat.chat_id}`).join(', ') || '无'}</p>
                      <p className="text-fg-3">适用插件: {group.plugin_scope.mode === 'all'
                        ? `所有已接入插件, 排除 ${group.plugin_scope.excluded.join(', ') || '无'}`
                        : group.plugin_scope.included.join(', ') || '无'}</p>
                    </div>
                  ))}
                </div>
                <div className="space-y-1.5">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-fg-4">成员例外</p>
                  {snapshot?.overrides.map((entry) => (
                    <div key={entry.id} className="text-[11.5px] leading-5 text-fg-2">
                      <p className="font-medium">{entry.platform_id} / {entry.user_id} · {entry.enabled ? '启用' : '停用'} · {entry.protect ? '保护账号' : '不保护账号'}</p>
                      <p className="text-fg-3">额外允许: {entry.allow.join(', ') || '无'}; 否决: {entry.deny.join(', ') || '无'}</p>
                    </div>
                  ))}
                </div>
              </div>
            </details>
          </Banner>
        )}

        {!loading && snapshot && groups.length === 0 && overrides.length === 0 && (
          <div className="rounded-xl border border-dashed border-[var(--glass-border-strong)] px-4 py-10 text-center">
            <ShieldCheck className="mx-auto h-8 w-8 text-fg-4" />
            <p className="mt-3 text-sm font-medium text-fg">尚未配置管理组与成员例外</p>
            <p className="mt-1 text-[12.5px] text-fg-3">各插件继续使用自己的原有名单。新增管理组后, 指定成员即可获得插件的系统管理权限。</p>
            <div className="mt-4 flex justify-center gap-2">
              <Button size="sm" variant="primary" icon={<Plus className="h-4 w-4" />} onClick={addGroup}>新增管理组</Button>
              <Button size="sm" icon={<Plus className="h-4 w-4" />} onClick={addOverride}>新增成员例外</Button>
            </div>
          </div>
        )}

        {groups.length > 0 && (
          <div>
            <div className="mb-2 flex items-center gap-2">
              <Users className="h-3.5 w-3.5 text-teal" />
              <h3 className="text-[12px] font-semibold uppercase tracking-wider text-fg-4">管理组</h3>
              <span className="num text-[11px] text-fg-4">{groups.length}</span>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {groups.map((group) => {
                const scopeSummary = group.plugin_scope.mode === 'all'
                  ? `所有插件 · 排除 ${group.plugin_scope.excluded.length} 个`
                  : `指定插件 ${group.plugin_scope.included.length} 个${group.plugin_scope.excluded.length ? ` · 旧版排除 ${group.plugin_scope.excluded.length} 个` : ''}`;
                const groupInvalid = [...group.members, ...(group.chats || [])].some((member) => {
                  const platform = snapshot?.platforms.find((item) => item.id === member.platform_id);
                  return Boolean(member.platform_instance_id) && platform?.instance_id !== member.platform_instance_id;
                });
                return <div key={group.id} aria-label={`管理组卡片 ${group.name || '未命名'}`}>
                  <button type="button" disabled={busy || loading} onClick={() => { setOpenGroup(group.id); setOpenOverride(null); }}
                    className="tile tint-teal group w-full !rounded-xl p-3.5 text-left disabled:opacity-60">
                    <span className="flex items-start gap-2.5">
                      <span className="grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.15)] text-tint"><Users className="h-4 w-4" /></span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[13.5px] font-medium text-fg">{group.name || '未命名管理组'}</span>
                        <span className="mt-0.5 block text-[11.5px] text-fg-3">{group.enabled ? '启用' : '停用'} · {group.protect === false ? '不保护账号' : '保护账号'} · 成员 {group.members.length} 人 · 授权群聊 {group.chats?.length || 0} 个 · {scopeSummary}{groupInvalid ? ' · 有失效身份' : ''}</span>
                      </span>
                      <span className="ml-auto shrink-0 text-[11.5px] text-fg-3">配置 ▸</span>
                      <ChevronRight className="mt-1 h-4 w-4 flex-none text-fg-4 transition-transform duration-300 group-hover:translate-x-0.5 group-hover:text-tint" />
                    </span>
                    <span className="mt-2.5 flex flex-wrap gap-1.5">
                      <Badge tone={group.enabled ? 'green' : 'neutral'} dot live={group.enabled}>{group.enabled ? '启用' : '停用'}</Badge>
                      <Badge tone={group.protect === false ? 'neutral' : 'orange'}>{group.protect === false ? '不保护账号' : '保护账号'}</Badge>
                      <Badge tone="teal">成员 {group.members.length}</Badge>
                      <Badge tone="teal">群聊 {(group.chats ?? []).length}</Badge>
                      {groupInvalid && <Badge tone="red" dot>有失效身份</Badge>}
                    </span>
                  </button>
                </div>;
              })}
            </div>
          </div>
        )}

        {overrides.length > 0 && (
          <div>
            <div className="mb-2 flex items-center gap-2">
              <UserCog className="h-3.5 w-3.5 text-accent" />
              <h3 className="text-[12px] font-semibold uppercase tracking-wider text-fg-4">成员例外</h3>
              <span className="num text-[11px] text-fg-4">{overrides.length}</span>
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {overrides.map((entry) => {
                const platform = snapshot?.platforms.find((item) => item.id === entry.platform_id);
                const invalid = Boolean(entry.platform_instance_id) && platform?.instance_id !== entry.platform_instance_id;
                const entryName = entry.platform_id && entry.user_id ? `${entry.platform_id} / ${entry.user_id}` : '未填写身份';
                return <div key={entry.id} aria-label={`成员例外卡片 ${entryName}`}>
                  <button type="button" disabled={busy || loading} onClick={() => { setOpenOverride(entry.id); setOpenGroup(null); }}
                    className="tile tint-accent group w-full !rounded-xl p-3.5 text-left disabled:opacity-60">
                    <span className="flex items-start gap-2.5">
                      <span className="grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.15)] text-tint"><UserCog className="h-4 w-4" /></span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-mono text-[12px] font-medium text-fg">{entryName}</span>
                        <span className="mt-0.5 block text-[11.5px] text-fg-3">{entry.enabled ? '启用' : '停用'} · {entry.protect ? '保护账号' : '不保护账号'} · 允许 {entry.allow.length} 个 · 否决 {entry.deny.length} 个{invalid ? ' · 身份已失效' : ''}</span>
                      </span>
                      <span className="ml-auto shrink-0 text-[11.5px] text-fg-3">配置 ▸</span>
                      <ChevronRight className="mt-1 h-4 w-4 flex-none text-fg-4 transition-transform duration-300 group-hover:translate-x-0.5 group-hover:text-tint" />
                    </span>
                    <span className="mt-2.5 flex flex-wrap gap-1.5">
                      <Badge tone="green">允许 {entry.allow.length}</Badge>
                      <Badge tone="red">否决 {entry.deny.length}</Badge>
                      {invalid && <Badge tone="orange" dot>身份已失效</Badge>}
                    </span>
                  </button>
                </div>;
              })}
            </div>
          </div>
        )}

        {preview && (
          <div className="space-y-3 rounded-xl border border-line bg-glass p-4" aria-label="有效权限预览">
            <p className="font-medium text-fg">草稿的有效管理员权限, 尚未保存</p>
            {!!preview.chats?.length && <p className="text-sm text-fg-3">群聊预览展示全员的基础授权范围, 实际调用仍叠加该成员的例外否决和插件自身限制。</p>}
            {preview.chats?.map((chat) => (
              <div key={`chat:${chat.platform_id}/${chat.chat_id}`} className="rounded-lg border border-line bg-glass p-3">
                <p className="flex flex-wrap items-center gap-2 text-[12.5px] font-medium text-fg">
                  <Globe className="h-3.5 w-3.5 text-fg-3" />{chat.platform_id} / 群 {chat.chat_id}
                  <span className="text-[11px] font-normal text-fg-4">· 群内全体成员</span>
                </p>
                <ul className="mt-2 space-y-1.5">{chat.plugins.map((plugin) => <PreviewRow key={plugin.name} plugin={plugin} />)}</ul>
              </div>
            ))}
            {preview.members.length === 0 && !preview.chats?.length && (
              <div className="py-10 text-center">
                <ShieldX className="mx-auto h-8 w-8 text-fg-4" />
                <p className="mt-2 text-sm text-fg-3">当前草稿没有任何成员或群聊授权</p>
              </div>
            )}
            {preview.members.map((member) => (
              <div key={`${member.platform_id}/${member.user_id}`} className="rounded-lg border border-line bg-glass p-3">
                <p className="flex flex-wrap items-center gap-2 text-[12.5px]">
                  <UserRound className="h-3.5 w-3.5 text-fg-3" />
                  <span className="font-mono text-fg">{member.platform_id} / {member.user_id}</span>
                  {member.override_id && <Badge tone="accent">成员例外</Badge>}
                </p>
                <ul className="mt-2 space-y-1.5">
                  {member.plugins.map((plugin) => <PreviewRow key={plugin.name} plugin={plugin} />)}
                </ul>
              </div>
            ))}
          </div>
        )}

        <p className="flex items-start gap-1.5 text-[11.5px] leading-5 text-fg-4">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 flex-none" />
          管理组只授予插件范围, 名单仅影响系统管理员授权; 需要否决某人时使用成员例外。保存后会立即对后续请求生效, 已加载的插件可能需要下一次调用才读取新名单。
        </p>
      </div>

      <Drawer open={editingGroup !== null} onClose={() => setOpenGroup(null)} label="编辑管理组" width={860}>
        {editingGroup && snapshot && (
          <GroupEditor group={editingGroup} snapshot={snapshot} rebind={currentRebind} rebindChats={currentRebindChats}
            busy={busy} dirty={dirty}
            onChange={(update) => change(editingGroup.id, update)}
            onDelete={() => {
              setGroups((current) => current.filter((item) => item.id !== editingGroup.id));
              setRebind((current) => current.filter((item) => item.group_id !== editingGroup.id));
              setRebindChats((current) => current.filter((item) => item.group_id !== editingGroup.id));
              setOpenGroup(null);
              setPreview(null);
            }}
            onRebindMember={(member) => {
              setRebind((current) => current.some((item) => item.group_id === editingGroup.id
                && item.platform_id === member.platform_id && item.user_id === member.user_id)
                ? current
                : [...current, { group_id: editingGroup.id, platform_id: member.platform_id, user_id: member.user_id }]);
              setPreview(null);
            }}
            onRebindChat={(chat) => {
              setRebindChats((current) => current.some((item) => item.group_id === editingGroup.id
                && item.platform_id === chat.platform_id && item.chat_id === chat.chat_id)
                ? current
                : [...current, { group_id: editingGroup.id, platform_id: chat.platform_id, chat_id: chat.chat_id }]);
              setPreview(null);
            }}
            onClose={() => setOpenGroup(null)}
            onSave={() => void save()} />
        )}
      </Drawer>

      <Drawer open={editingOverride !== null} onClose={() => setOpenOverride(null)} label="编辑成员例外" width={820}>
        {editingOverride && snapshot && (
          <OverrideEditor entry={editingOverride} snapshot={snapshot} rebindOverrides={currentRebindOverrides}
            busy={busy} dirty={dirty}
            onChange={(update) => changeOverride(editingOverride.id, update)}
            onDelete={() => {
              setOverrides((current) => current.filter((item) => item.id !== editingOverride.id));
              setRebindOverrides((current) => current.filter((item) => item.override_id !== editingOverride.id));
              setOpenOverride(null);
              setPreview(null);
            }}
            onRebind={(entry) => {
              setRebindOverrides((current) => current.some((item) => item.override_id === entry.id
                && item.platform_id === entry.platform_id && item.user_id === entry.user_id)
                ? current
                : [...current, { override_id: entry.id, platform_id: entry.platform_id, user_id: entry.user_id }]);
              setPreview(null);
            }}
            onClose={() => setOpenOverride(null)}
            onSave={() => void save()} />
        )}
      </Drawer>
    </SectionCard>
  );
}

function GroupEditor({ group, snapshot, rebind, rebindChats, busy, dirty, onClose, onChange, onDelete, onRebindMember, onRebindChat, onSave }: {
  group: AdministratorGroup;
  snapshot: AdministratorSnapshot;
  rebind: AdministratorRebind[];
  rebindChats: AdministratorChatRebind[];
  busy: boolean;
  dirty: boolean;
  onClose: () => void;
  onChange: (update: (group: AdministratorGroup) => AdministratorGroup) => void;
  onDelete: () => void;
  onRebindMember: (member: AdministratorGroup['members'][number]) => void;
  onRebindChat: (chat: NonNullable<AdministratorGroup['chats']>[number]) => void;
  onSave: () => void;
}) {
  const groupPlatforms = [...new Set([...group.members, ...(group.chats ?? [])].map((item) => item.platform_id)
    .filter((id) => snapshot.platforms.some((item) => item.id === id)))];

  return <div className="flex h-full flex-col tint-teal">
    <div className="flex items-start gap-3 border-b border-line px-5 py-4">
      <div className="grid h-9 w-9 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.15)] text-tint"><Users className="h-[18px] w-[18px]" /></div>
      <div className="min-w-0 flex-1">
        <h2 className="text-base font-semibold text-fg">{group.name || '未命名管理组'}</h2>
        <p className="mt-0.5 text-[12px] text-fg-3">修改会保留在草稿中, 点击“保存并应用”后生效</p>
      </div>
      <Button size="sm" variant="ghost" onClick={onClose}>完成</Button>
    </div>

    <div className="scroll min-h-0 flex-1 space-y-5 px-5 py-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="管理组名称" className="sm:col-span-2" hint="用于在内置指令与日志中识别该组">
          <Input value={group.name} maxLength={128} placeholder="例如: 主要管理员"
            onChange={(event) => onChange((current) => ({ ...current, name: event.target.value }))} />
        </Field>
        <div className="rounded-xl border border-line bg-glass px-4">
          <div className="flex items-center justify-between gap-6 py-3">
            <div className="min-w-0">
              <p className="text-sm font-medium text-fg">启用此组</p>
              <p className="mt-0.5 text-[13px] leading-5 text-fg-3">停用后组内授权立即失效</p>
            </div>
            <label className="flex items-center gap-2 text-sm text-fg-2">
              <input type="checkbox" aria-label="启用此组" className="h-4 w-4 accent-[var(--fg-accent)]" checked={group.enabled}
                onChange={(event) => onChange((current) => ({ ...current, enabled: event.target.checked }))} />
              启用
            </label>
          </div>
        </div>
        <div className="rounded-xl border border-line bg-glass px-4">
          <div className="flex items-center justify-between gap-6 py-3">
            <div className="min-w-0">
              <p className="text-sm font-medium text-fg">账号保护</p>
              <p className="mt-0.5 text-[13px] leading-5 text-fg-3">仅个人成员加入好友删除保护名单, 不扩展到授权群聊全体成员</p>
            </div>
            <button type="button" role="switch" aria-checked={group.protect !== false} aria-label="账号保护"
              onClick={() => onChange((current) => ({ ...current, protect: current.protect === false }))}
              className={cn('relative inline-flex h-[22px] w-10 shrink-0 items-center rounded-full border px-[2px] transition-all duration-300',
                group.protect !== false ? 'border-[rgb(var(--rgb-accent))] bg-[rgb(var(--rgb-accent))]' : 'border-[var(--glass-border-strong)] bg-glass-active')}>
              <span className={cn('h-4 w-4 rounded-full bg-white shadow-sm transition-transform duration-300', group.protect !== false ? 'translate-x-[18px]' : 'translate-x-0 bg-fg-3')} />
            </button>
          </div>
        </div>
      </div>

      <div className="space-y-2.5">
        <div className="flex items-center justify-between gap-2">
          <div>
            <p className="text-[13px] font-medium text-fg">管理员成员</p>
            <p className="mt-0.5 text-[11.5px] text-fg-3">填写平台用户识别号, 不是昵称。失效身份需要重新绑定平台实例。</p>
          </div>
          <Button size="sm" variant="tinted" tint="teal" icon={<Plus className="h-3.5 w-3.5" />}
            onClick={() => onChange((current) => ({ ...current, members: [...current.members, { platform_id: '', user_id: '' }] }))}>添加管理员成员</Button>
        </div>
        {group.members.length === 0 && <p className="rounded-lg border border-dashed border-line px-3 py-3 text-[12px] text-fg-4">尚未添加成员, 此组不会授予任何人权限。</p>}
        {group.members.map((member, index) => {
          const platform = snapshot.platforms.find((item) => item.id === member.platform_id);
          const invalid = Boolean(member.platform_instance_id) && platform?.instance_id !== member.platform_instance_id;
          const rebound = rebind.some((item) => item.group_id === group.id && item.platform_id === member.platform_id && item.user_id === member.user_id);
          return <div key={index} className="space-y-2 rounded-xl border border-line bg-glass p-3">
            <div className="grid items-end gap-2 md:grid-cols-[1fr_1fr_auto]">
              <label className="text-[13px] font-medium text-fg-2">平台
                <PlatformSelect label="管理员平台" value={member.platform_id} platforms={snapshot.platforms}
                  onChange={(value) => onChange((current) => ({
                    ...current,
                    members: current.members.map((item, at) => at === index ? { ...item, platform_id: value } : item),
                  }))} />
              </label>
              <Field label="平台用户识别号">
                <Input value={member.user_id} maxLength={256} placeholder="填写用户 ID, 不是昵称"
                  onChange={(event) => onChange((current) => ({
                    ...current,
                    members: current.members.map((item, at) => at === index ? { ...item, user_id: event.target.value } : item),
                  }))} />
              </Field>
              <Button size="sm" variant="ghost" onClick={() => onChange((current) => ({ ...current, members: current.members.filter((_, at) => at !== index) }))}>移除成员</Button>
            </div>
            {invalid && <Banner tone="warning" compact>
              {rebound ? '保存时将明确绑定到当前平台实例' : '原平台已移除或重建, 此身份授权已失效'}
              {platform && !rebound && <Button size="sm" variant="ghost" className="ml-2 !h-6" onClick={() => onRebindMember(member)}>绑定到当前平台</Button>}
            </Banner>}
          </div>;
        })}
      </div>

      <div className="space-y-2.5">
        <div className="flex items-center justify-between gap-2">
          <div>
            <p className="text-[13px] font-medium text-fg">授权群聊</p>
            <p className="mt-0.5 text-[11.5px] text-fg-3">群内任何成员发起请求时都获得本组的插件管理权限, 私聊和其他群不继承。账号保护仅适用于上方指定的个人成员。</p>
          </div>
          <Button size="sm" variant="tinted" tint="teal" icon={<Plus className="h-3.5 w-3.5" />}
            onClick={() => onChange((current) => ({ ...current, chats: [...(current.chats ?? []), { platform_id: '', chat_id: '' }] }))}>添加授权群聊</Button>
        </div>
        {(group.chats ?? []).length === 0 && <p className="rounded-lg border border-dashed border-line px-3 py-3 text-[12px] text-fg-4">尚未授权群聊。</p>}
        {(group.chats ?? []).map((chat, index) => {
          const platform = snapshot.platforms.find((item) => item.id === chat.platform_id);
          const invalid = Boolean(chat.platform_instance_id) && platform?.instance_id !== chat.platform_instance_id;
          const rebound = rebindChats.some((item) => item.group_id === group.id && item.platform_id === chat.platform_id && item.chat_id === chat.chat_id);
          return <div key={index} className="space-y-2 rounded-xl border border-line bg-glass p-3">
            <div className="grid items-end gap-2 md:grid-cols-[1fr_1fr_auto]">
              <label className="text-[13px] font-medium text-fg-2">平台
                <PlatformSelect label="授权群聊平台" value={chat.platform_id} platforms={snapshot.platforms}
                  onChange={(value) => onChange((current) => ({
                    ...current,
                    chats: (current.chats ?? []).map((item, at) => at === index ? { ...item, platform_id: value } : item),
                  }))} />
              </label>
              <Field label="群聊识别号">
                <Input value={chat.chat_id} maxLength={256} placeholder="填写群号或群 ID, 不是群名"
                  onChange={(event) => onChange((current) => ({
                    ...current,
                    chats: (current.chats ?? []).map((item, at) => at === index ? { ...item, chat_id: event.target.value } : item),
                  }))} />
              </Field>
              <Button size="sm" variant="ghost" onClick={() => onChange((current) => ({ ...current, chats: (current.chats ?? []).filter((_, at) => at !== index) }))}>移除群聊</Button>
            </div>
            {invalid && <Banner tone="warning" compact>
              {rebound ? '保存时将明确绑定到当前平台实例' : '原平台已移除或重建, 此群聊授权已失效'}
              {platform && !rebound && <Button size="sm" variant="ghost" className="ml-2 !h-6"
                onClick={() => onRebindChat(chat)}>绑定到当前平台</Button>}
            </Banner>}
          </div>;
        })}
      </div>

      <div className="space-y-3">
        <label className="block text-[13px] font-medium text-fg-2">适用插件
          <KitSelect aria-label="适用插件" className={selectClass} value={group.plugin_scope.mode}
            onChange={(value) => onChange((current) => ({
              ...current,
              plugin_scope: { ...current.plugin_scope, mode: value as 'selected' | 'all', included: [] },
            }))}>
            <option value="selected">指定插件</option>
            <option value="all">所有已接入插件, 包含未来接入的插件</option>
          </KitSelect>
        </label>
        <p className="text-sm text-fg-3">管理组只授予插件范围, 名单仅影响系统管理员授权; 插件原名单仍可独立授权。需要否决某人时使用成员例外。</p>
        {group.plugin_scope.mode === 'selected' ? <>
          <PluginListSection title={TITLES.allowGroup} names={group.plugin_scope.included} snapshot={snapshot}
            addLabel="添加允许的插件" taken={[...group.plugin_scope.included, ...group.plugin_scope.excluded]} platformIds={groupPlatforms}
            onAdd={(name) => onChange((current) => ({
              ...current,
              plugin_scope: { ...current.plugin_scope, included: [...current.plugin_scope.included, name] },
            }))}
            onRemove={(name) => onChange((current) => ({
              ...current,
              plugin_scope: { ...current.plugin_scope, included: current.plugin_scope.included.filter((item) => item !== name) },
            }))} />
          {group.plugin_scope.excluded.length > 0 && (
            <PluginListSection title={TITLES.legacyExcluded} names={group.plugin_scope.excluded} snapshot={snapshot}
              platformIds={groupPlatforms}
              hint="旧版指定插件模式的排除仍按原规则生效, 只可移除; 新否决请使用成员例外, 读取时已按当前成员自动展开。"
              onRemove={(name) => onChange((current) => ({
                ...current,
                plugin_scope: { ...current.plugin_scope, excluded: current.plugin_scope.excluded.filter((item) => item !== name) },
              }))} />
          )}
        </> : <PluginListSection title={TITLES.excluded} names={group.plugin_scope.excluded} snapshot={snapshot}
          hint="未列出的插件全部授予, 包含未来接入的插件; 排除作用于全部成员。"
          addLabel="添加排除的插件" platformIds={groupPlatforms}
          onAdd={(name) => onChange((current) => ({
            ...current,
            plugin_scope: { ...current.plugin_scope, excluded: [...current.plugin_scope.excluded, name] },
          }))}
          onRemove={(name) => onChange((current) => ({
            ...current,
            plugin_scope: { ...current.plugin_scope, excluded: current.plugin_scope.excluded.filter((item) => item !== name) },
          }))} />}
      </div>

      <div className="rounded-xl border border-[rgb(var(--rgb-red)/0.25)] bg-[rgb(var(--rgb-red)/0.05)] p-3">
        <p className="text-[12px] font-medium text-fg-2">删除管理组</p>
        <p className="mt-0.5 text-[11.5px] text-fg-3">删除后该组的成员授权与群聊授权立即从草稿中移除, 保存后生效。</p>
        <Button size="sm" variant="danger" className="mt-2" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={onDelete}>删除管理组</Button>
      </div>
    </div>

    <div className="flex items-center justify-between gap-2 border-t border-line px-5 py-3">
      <span className="text-[11.5px] text-fg-4">{dirty ? '有未保存的修改' : '所有修改已保存'}</span>
      <div className="flex gap-2">
        <Button variant="ghost" onClick={onClose} disabled={busy}>完成</Button>
        <Button variant="primary" icon={<Save className="h-4 w-4" />} loading={busy} disabled={!dirty} onClick={onSave}>保存并应用</Button>
      </div>
    </div>
  </div>;
}

function OverrideEditor({ entry, snapshot, rebindOverrides, busy, dirty, onClose, onChange, onDelete, onRebind, onSave }: {
  entry: AdministratorOverride;
  snapshot: AdministratorSnapshot;
  rebindOverrides: AdministratorOverrideRebind[];
  busy: boolean;
  dirty: boolean;
  onClose: () => void;
  onChange: (update: (entry: AdministratorOverride) => AdministratorOverride) => void;
  onDelete: () => void;
  onRebind: (entry: AdministratorOverride) => void;
  onSave: () => void;
}) {
  const platform = snapshot.platforms.find((item) => item.id === entry.platform_id);
  const invalid = Boolean(entry.platform_instance_id) && platform?.instance_id !== entry.platform_instance_id;
  const rebound = rebindOverrides.some((item) => item.override_id === entry.id && item.platform_id === entry.platform_id && item.user_id === entry.user_id);
  const platformIds = platform ? [entry.platform_id] : [];

  return <div className="flex h-full flex-col tint-accent">
    <div className="flex items-start gap-3 border-b border-line px-5 py-4">
      <div className="grid h-9 w-9 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.15)] text-tint"><UserCog className="h-[18px] w-[18px]" /></div>
      <div className="min-w-0 flex-1">
        <h2 className="text-base font-semibold text-fg">成员例外</h2>
        <p className="mt-0.5 font-mono text-[12px] text-fg-3">{entry.platform_id || '未选择平台'} / {entry.user_id || '未填写身份'}</p>
      </div>
      <Button size="sm" variant="ghost" onClick={onClose}>完成</Button>
    </div>

    <div className="scroll min-h-0 flex-1 space-y-5 px-5 py-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-xl border border-line bg-glass px-4">
          <div className="flex items-center justify-between gap-6 py-3">
            <div className="min-w-0">
              <p className="text-sm font-medium text-fg">启用此条目</p>
              <p className="mt-0.5 text-[13px] leading-5 text-fg-3">停用后允许与否决都不生效</p>
            </div>
            <label className="flex items-center gap-2 text-sm text-fg-2">
              <input type="checkbox" aria-label="启用此条目" className="h-4 w-4 accent-[var(--fg-accent)]" checked={entry.enabled}
                onChange={(event) => onChange((current) => ({ ...current, enabled: event.target.checked }))} />
              启用
            </label>
          </div>
        </div>
        <div className="rounded-xl border border-line bg-glass px-4">
          <div className="flex items-center justify-between gap-6 py-3">
            <div className="min-w-0">
              <p className="text-sm font-medium text-fg">账号保护</p>
              <p className="mt-0.5 text-[13px] leading-5 text-fg-3">此身份加入好友删除保护名单</p>
            </div>
            <button type="button" role="switch" aria-checked={entry.protect === true} aria-label="账号保护"
              onClick={() => onChange((current) => ({ ...current, protect: current.protect !== true }))}
              className={cn('relative inline-flex h-[22px] w-10 shrink-0 items-center rounded-full border px-[2px] transition-all duration-300',
                entry.protect === true ? 'border-[rgb(var(--rgb-accent))] bg-[rgb(var(--rgb-accent))]' : 'border-[var(--glass-border-strong)] bg-glass-active')}>
              <span className={cn('h-4 w-4 rounded-full bg-white shadow-sm transition-transform duration-300', entry.protect === true ? 'translate-x-[18px]' : 'translate-x-0 bg-fg-3')} />
            </button>
          </div>
        </div>
      </div>

      <div className="grid items-end gap-2 md:grid-cols-2">
        <label className="text-[13px] font-medium text-fg-2">平台
          <PlatformSelect label="例外平台" value={entry.platform_id} platforms={snapshot.platforms}
            onChange={(value) => onChange((current) => ({ ...current, platform_id: value }))} />
        </label>
        <Field label="平台用户识别号">
          <Input value={entry.user_id} maxLength={256} placeholder="填写用户 ID, 不是昵称"
            onChange={(event) => onChange((current) => ({ ...current, user_id: event.target.value }))} />
        </Field>
      </div>

      {invalid && <Banner tone="warning">
        {rebound ? '保存时将明确绑定到当前平台实例' : '原平台已移除或重建, 此例外已失效'}
        {platform && !rebound && <Button size="sm" variant="ghost" className="ml-2 !h-6" onClick={() => onRebind(entry)}>绑定到当前平台</Button>}
      </Banner>}

      <p className="text-[12px] leading-5 text-fg-3">允许为本不属于任何组的人补充授权; 否决压过所有管理组的允许, 包括其他组的允许。同一插件加入一侧名单时会自动移出另一侧。</p>

      <PluginListSection title={TITLES.allowOverride} names={entry.allow} snapshot={snapshot}
        addLabel="添加允许的插件" taken={entry.allow} platformIds={platformIds}
        onAdd={(name) => onChange((current) => ({
          ...current, allow: [...current.allow, name], deny: current.deny.filter((item) => item !== name),
        }))}
        onRemove={(name) => onChange((current) => ({ ...current, allow: current.allow.filter((item) => item !== name) }))} />

      <PluginListSection title={TITLES.denyOverride} names={entry.deny} snapshot={snapshot}
        hint="否决压过此人的全部允许来源。" addLabel="添加否决的插件" taken={entry.deny} platformIds={platformIds}
        onAdd={(name) => onChange((current) => ({
          ...current, deny: [...current.deny, name], allow: current.allow.filter((item) => item !== name),
        }))}
        onRemove={(name) => onChange((current) => ({ ...current, deny: current.deny.filter((item) => item !== name) }))} />

      <div className="rounded-xl border border-[rgb(var(--rgb-red)/0.25)] bg-[rgb(var(--rgb-red)/0.05)] p-3">
        <p className="text-[12px] font-medium text-fg-2">删除成员例外</p>
        <p className="mt-0.5 text-[11.5px] text-fg-3">删除后此人的允许与否决都会移除, 恢复管理组与插件原名单的裁定。</p>
        <Button size="sm" variant="danger" className="mt-2" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={onDelete}>删除例外</Button>
      </div>
    </div>

    <div className="flex items-center justify-between gap-2 border-t border-line px-5 py-3">
      <span className="text-[11.5px] text-fg-4">{dirty ? '有未保存的修改' : '所有修改已保存'}</span>
      <div className="flex gap-2">
        <Button variant="ghost" onClick={onClose} disabled={busy}>完成</Button>
        <Button variant="primary" icon={<Save className="h-4 w-4" />} loading={busy} disabled={!dirty} onClick={onSave}>保存并应用</Button>
      </div>
    </div>
  </div>;
}
