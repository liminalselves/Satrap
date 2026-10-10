import { useCallback, useEffect, useRef, useState } from 'react';
import { administratorsApi, administratorError, administratorRuntimeLabel, type AdministratorGroup, type AdministratorOverride, type AdministratorOverrideRebind, type AdministratorPreview, type AdministratorRebind, type AdministratorPlugin, type AdministratorSnapshot } from '@/api/administrators';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Modal } from '@/components/ui/Modal';
import { Toggle } from '@/components/ui/Toggle';
import { toast } from '@/components/ui/Toast';

// 名单式插件编辑: 界面直接操作 included/excluded/allow/deny 四个名单, 添加即生效, 没有中间表态层
const pluginBadges = (plugin: AdministratorPlugin | undefined, platformIds: string[]): string => {
  if (!plugin) return ' · 暂不可用';
  return [!plugin.supports_administrators ? ' · 未接入' : '', plugin.enabled === false ? ' · 未启用' : '',
    platformIds.length > 0 && plugin.loaded_platforms && !platformIds.some(id => plugin.loaded_platforms!.includes(id)) ? ' · 平台不适用' : ''].join('');
};

// 一个插件名单区块: 标题, 说明, 逐行插件卡片与可选的添加选择器
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
  // 选择器只提供已接入且已启用的插件; 有平台上下文时还要求在该平台实际加载
  // 已在名单中的条目不受过滤影响, 保留显示以便移除
  const options = (snapshot?.plugins || []).filter(item => item.supports_administrators && item.enabled !== false
    && !(taken || names).includes(item.name)
    && (platformIds.length === 0 || !item.loaded_platforms || item.loaded_platforms.some(id => platformIds.includes(id))));
  return <div className="space-y-2" aria-label={title}>
    <p className="text-sm font-medium">{title}</p>
    {hint && <p className="text-xs text-text-secondary">{hint}</p>}
    {names.map(name => {
      const plugin = snapshot?.plugins.find(item => item.name === name);
      return <div key={name} className="flex items-start gap-2 rounded-md bg-glass p-3 text-sm">
        <div className="min-w-0 flex-1">
          <p className="font-medium">{name}{pluginBadges(plugin, platformIds)}</p>
          {plugin?.supports_administrators && <ul className="text-text-secondary">
            {Object.entries(plugin.management_permissions).filter(([, rule]) => rule.system_admin).map(([id, rule]) => <li key={id}>
              {rule.description}
              {!!rule.requirements?.length && <p className="mt-1 text-xs">{rule.requirements.join('；')}</p>}
            </li>)}
          </ul>}
        </div>
        <Button aria-label={`移除 ${name}`} onClick={() => onRemove(name)}>移除</Button>
      </div>;
    })}
    {addLabel && onAdd && <select aria-label={addLabel} className={selectClass} value=""
      onChange={event => { if (event.target.value) onAdd(event.target.value); }}>
      <option value="">添加插件…</option>
      {options.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
    </select>}
  </div>;
}

const selectClass = 'glass-input w-full';

export function AdministratorsPanel({ active, onSaved }: { active: boolean; onSaved?: (groups: AdministratorGroup[], overrides: AdministratorOverride[], revision: string, previousRevision: string) => void }) {
  const [snapshot, setSnapshot] = useState<AdministratorSnapshot | null>(null);
  const [groups, setGroups] = useState<AdministratorGroup[]>([]);
  const [overrides, setOverrides] = useState<AdministratorOverride[]>([]);
  const [rebind, setRebind] = useState<AdministratorRebind[]>([]);
  const [rebindOverrides, setRebindOverrides] = useState<AdministratorOverrideRebind[]>([]);
  const [preview, setPreview] = useState<AdministratorPreview | null>(null);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [comparison, setComparison] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  // 卡片平铺只显示摘要, 点击后在弹窗中编辑; 新建的卡片直接打开弹窗
  const [openCard, setOpenCard] = useState<string | null>(null);
  const dirty = snapshot !== null
    && (JSON.stringify(groups) !== JSON.stringify(snapshot.groups)
      || JSON.stringify(overrides) !== JSON.stringify(snapshot.overrides)
      || rebind.length > 0 || rebindOverrides.length > 0);
  const dirtyRef = useRef(dirty);
  const busyRef = useRef(busy);
  const requests = useRef(0);
  dirtyRef.current = dirty;
  busyRef.current = busy;
  const currentRebind = rebind.filter(item => groups.some(group => group.id === item.group_id && group.members.some(member => member.platform_id === item.platform_id && member.user_id === item.user_id)));
  const currentRebindOverrides = rebindOverrides.filter(item => overrides.some(entry => entry.id === item.override_id && entry.platform_id === item.platform_id && entry.user_id === item.user_id));

  const load = useCallback(async () => {
    if (busyRef.current) return;
    const request = ++requests.current;
    setLoading(true);
    try {
      const value = await administratorsApi.read();
      if (request !== requests.current) return;
      if (dirtyRef.current) {
        setSnapshot(previous => previous ? { ...previous, platforms: value.platforms, plugins: value.plugins, plugin_errors: value.plugin_errors, runtime: value.runtime } : value);
      } else {
        setSnapshot(value);
        setGroups(value.groups);
        setOverrides(value.overrides);
        setRebind([]);
        setRebindOverrides([]);
        setConflict(false);
        setComparison(false);
      }
      setError('');
    } catch (cause) {
      if (request === requests.current) setError(administratorError(cause));
    } finally {
      if (request === requests.current) setLoading(false);
    }
  }, []);

  useEffect(() => { if (active) void load(); }, [active, load]);
  useEffect(() => () => { requests.current++; }, []);

  const change = (id: string, update: (group: AdministratorGroup) => AdministratorGroup) => {
    setGroups(current => current.map(group => group.id === id ? update(group) : group));
    setPreview(null);
  };

  const changeOverride = (id: string, update: (entry: AdministratorOverride) => AdministratorOverride) => {
    setOverrides(current => current.map(entry => entry.id === id ? update(entry) : entry));
    setPreview(null);
  };

  const save = async () => {
    if (!snapshot || busyRef.current) return;
    requests.current++;
    busyRef.current = true;
    setBusy(true);
    setLoading(false);
    setError('');
    try {
      const value = await administratorsApi.save(groups, overrides, snapshot.revision, currentRebind, currentRebindOverrides);
      setSnapshot(value);
      setGroups(value.groups);
      setOverrides(value.overrides);
      setRebind([]);
      setRebindOverrides([]);
      setConflict(false);
      setComparison(false);
      setPreview(null);
      onSaved?.(value.groups, value.overrides, value.revision, snapshot.revision);
      toast(value.runtime.status === 'applied' ? 'success' : 'warning', administratorRuntimeLabel(value.runtime));
    } catch (cause) {
      setError(administratorError(cause));
      setConflict(typeof cause === 'object' && cause !== null && 'response' in cause && (cause.response as { status?: number })?.status === 409);
    } finally {
      setBusy(false);
      busyRef.current = false;
    }
  };

  const run = async (action: 'preview' | 'apply' | 'rebase') => {
    if (!snapshot || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    requests.current++;
    setError('');
    try {
      if (action === 'preview') setPreview(await administratorsApi.preview(groups, overrides, currentRebind, currentRebindOverrides));
      else if (action === 'apply') {
        const value = await administratorsApi.apply(snapshot.section_revision);
        setSnapshot(current => current && { ...current, runtime: value.runtime });
        toast(value.runtime.status === 'applied' ? 'success' : 'warning', administratorRuntimeLabel(value.runtime));
      } else {
        const value = await administratorsApi.read();
        setSnapshot(value);
        setConflict(false);
        setComparison(true);
        toast('warning', '已保留草稿并读取最新版本, 请比较管理组与成员例外后再保存');
      }
    } catch (cause) {
      setError(administratorError(cause));
    } finally {
      setBusy(false);
      busyRef.current = false;
      setLoading(false);
    }
  };

  return <div className="space-y-4 pt-4" aria-label="管理员设置">
    <p className="text-sm text-text-secondary">管理员身份来自真实平台消息。授权插件后可免填该插件的调用者名单, 功能开关、操作范围和审批仍然生效。</p>
    <div className="flex flex-wrap gap-2">
      <Button disabled={busy || loading} onClick={() => void load()}>刷新目录与状态</Button>
      <Button disabled={!snapshot || busy} onClick={() => {
        const id = crypto.randomUUID();
        setGroups(current => [...current, { id, name: '', enabled: true, protect: true, members: [], plugin_scope: { mode: 'selected', included: [], excluded: [] } }]);
        setOpenCard(id);
        setPreview(null);
      }}>新增管理组</Button>
      <Button disabled={!snapshot || busy} onClick={() => {
        const id = crypto.randomUUID();
        setOverrides(current => [...current, { id, enabled: true, protect: false, platform_id: '', user_id: '', allow: [], deny: [] }]);
        setOpenCard(id);
        setPreview(null);
      }}>新增成员例外</Button>
      <Button disabled={!snapshot || busy} onClick={() => void run('preview')}>预览有效权限</Button>
      <Button variant="primary" disabled={!snapshot || !dirty || busy} onClick={() => void save()}>保存并应用</Button>
    </div>
    {loading && <p className="text-sm text-text-secondary">正在读取管理员设置…</p>}
    {error && <div role="alert" className="rounded-md border border-error p-3 text-sm text-error">{error}</div>}
    {!!snapshot?.plugin_errors?.length && <div role="alert" className="rounded-md border border-border-glass p-3 text-sm"><p>以下插件无法加载, 不会授予管理员权限:</p><ul>{snapshot.plugin_errors.map((item, index) => <li key={`${item.name}-${index}`}>{item.name}: {item.error}</li>)}</ul></div>}
    {snapshot?.migrated_from_legacy && <div role="alert" className="rounded-md border border-border-glass p-3 text-sm">
      <p>已将旧版“指定插件”模式下的组排除迁移为成员例外, 保存后生效。</p>
      <p className="text-text-secondary">迁移按当前成员展开, 之后新加入该组的成员不会自动继承这条否决。</p>
    </div>}
    {!!snapshot?.migration_pending?.length && <div role="alert" className="rounded-md border border-border-glass p-3 text-sm">
      <p>{snapshot.migration_pending.length} 个组的旧排除与现有例外的启用状态冲突, 未自动迁移; 请调整对应例外条目的启用状态后保存。</p>
      <p className="text-text-secondary">涉及管理组: {snapshot.migration_pending.join(', ')}。未迁移期间这些旧排除仍按原规则生效, 可原样保留。</p>
    </div>}
    {(conflict || comparison) && <div className="space-y-2 rounded-md border border-border-glass p-3 text-sm">
      <p>{conflict ? '配置已被其他操作修改, 草稿已保留。请读取最新版本, 比较差异后再保存。' : '已读取最新版本, 以下显示已保存的配置。编辑区仍是你的草稿, 请比较后再保存。'}</p>
      {conflict && <Button disabled={busy} onClick={() => void run('rebase')}>保留草稿并读取最新版本</Button>}
      <details open={comparison}><summary>最近读取的管理组与成员例外</summary>
        <ul className="space-y-2">{snapshot?.groups.map(group => <li key={group.id}><p>{group.name} · {group.enabled ? '启用' : '停用'} · {group.protect === false ? '不保护账号' : '保护账号'}</p><p>成员: {group.members.map(member => `${member.platform_id} / ${member.user_id}`).join(', ') || '无'}</p><p>适用插件: {group.plugin_scope.mode === 'all' ? `所有已接入插件, 排除 ${group.plugin_scope.excluded.join(', ') || '无'}` : group.plugin_scope.included.join(', ') || '无'}</p></li>)}</ul>
        {!!snapshot?.overrides.length && <ul className="space-y-2">{snapshot.overrides.map(entry => <li key={entry.id}><p>{entry.platform_id} / {entry.user_id} · {entry.enabled ? '启用' : '停用'} · {entry.protect ? '保护账号' : '不保护账号'}</p><p>额外允许: {entry.allow.join(', ') || '无'}; 否决: {entry.deny.join(', ') || '无'}</p></li>)}</ul>}
      </details>
    </div>}
    {snapshot && <div className="rounded-md border border-border-glass p-3 text-sm">
      <p>{administratorRuntimeLabel(snapshot.runtime)}{dirty ? ' · 当前修改尚未保存' : ''}</p>
      {snapshot.runtime.error && <p className="text-text-secondary">{snapshot.runtime.error}</p>}
      {snapshot.runtime.status === 'unconfirmed' && <Button className="mt-2" disabled={busy || dirty} onClick={() => void run('apply')}>重试应用已保存配置</Button>}
    </div>}
    {snapshot && !groups.length && !overrides.length && <p className="text-text-secondary">尚未配置管理组与成员例外。各插件继续使用自己的原有名单。</p>}
    {!!groups.length && <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
    {groups.map(group => {
      const scopeSummary = group.plugin_scope.mode === 'all'
        ? `所有插件, 排除 ${group.plugin_scope.excluded.length} 个`
        : `指定插件 ${group.plugin_scope.included.length} 个${group.plugin_scope.excluded.length ? ` · 旧版排除 ${group.plugin_scope.excluded.length} 个` : ''}`;
      const groupInvalid = group.members.some(member => {
        const platform = snapshot?.platforms.find(item => item.id === member.platform_id);
        return member.platform_instance_id && platform?.instance_id !== member.platform_instance_id;
      });
      // 插件选择器按成员所在平台过滤, 取有效成员平台的并集; 无有效成员时不按平台过滤
      const groupPlatforms = [...new Set(group.members.map(member => member.platform_id)
        .filter(id => snapshot?.platforms.some(item => item.id === id)))];
      return <div key={group.id} aria-label={`管理组卡片 ${group.name || '未命名'}`}
        className="rounded-md border border-border-glass p-4">
      <button type="button" disabled={busy || loading} onClick={() => setOpenCard(group.id)}
        className="flex w-full flex-col items-start gap-1 text-left text-sm">
        <span className="flex w-full items-center gap-2">
          <span className="font-medium">{group.name || '未命名管理组'}</span>
          <span className="ml-auto shrink-0 text-text-secondary">配置 ▸</span>
        </span>
        <span className="text-text-secondary">{group.enabled ? '启用' : '停用'} · {group.protect === false ? '不保护账号' : '保护账号'} · 成员 {group.members.length} 人 · {scopeSummary}{groupInvalid ? ' · 有失效身份' : ''}</span>
      </button>
      <Modal open={openCard === group.id} onClose={() => setOpenCard(null)} title={group.name || '管理组'} size="3xl">
      <fieldset disabled={busy || loading} className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="min-w-48 flex-1 text-sm">管理组名称
          <Input value={group.name} maxLength={128} placeholder="例如: 主要管理员"
            onChange={event => change(group.id, current => ({ ...current, name: event.target.value }))} />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={group.enabled}
            onChange={event => change(group.id, current => ({ ...current, enabled: event.target.checked }))} />启用此组
        </label>
        <div className="flex items-center gap-2 text-sm">
          <span id={`protect-${group.id}`}>账号保护</span>
          <Toggle checked={group.protect !== false} title="组成员加入好友删除保护名单"
            onChange={value => change(group.id, current => ({ ...current, protect: value }))} />
        </div>
        <Button onClick={() => {
          setGroups(current => current.filter(item => item.id !== group.id));
          setRebind(current => current.filter(item => item.group_id !== group.id));
          setOpenCard(null);
          setPreview(null);
        }}>删除管理组</Button>
      </div>
      <div className="space-y-2">
        <p className="text-sm font-medium">管理员成员</p>
        {group.members.map((member, index) => {
          const platform = snapshot?.platforms.find(item => item.id === member.platform_id);
          const invalid = member.platform_instance_id && platform?.instance_id !== member.platform_instance_id;
          const rebound = rebind.some(item => item.group_id === group.id && item.platform_id === member.platform_id && item.user_id === member.user_id);
          return <div key={index} className="space-y-2 rounded-md bg-glass p-3">
            <div className="grid items-end gap-2 md:grid-cols-[1fr_1fr_auto]">
              <label className="text-sm">平台
                <select aria-label="管理员平台" className={selectClass} value={member.platform_id}
                  onChange={event => change(group.id, current => ({
                    ...current,
                    members: current.members.map((item, at) => at === index ? { platform_id: event.target.value, user_id: item.user_id } : item),
                  }))}>
                  <option value="">选择已配置的平台</option>
                  {!platform && member.platform_id && <option value={member.platform_id}>{member.platform_id} · 平台已移除</option>}
                  {snapshot?.platforms.map(item => <option key={item.id} value={item.id}>{item.name || item.id} · {item.type}{item.enabled ? '' : ' · 已停用'}</option>)}
                </select>
              </label>
              <label className="text-sm">平台用户识别号
                <Input value={member.user_id} maxLength={256} placeholder="填写用户 ID, 不是昵称"
                  onChange={event => change(group.id, current => ({
                    ...current,
                    members: current.members.map((item, at) => at === index ? { ...item, user_id: event.target.value } : item),
                  }))} />
              </label>
              <Button onClick={() => change(group.id, current => ({
                ...current,
                members: current.members.filter((_, at) => at !== index),
              }))}>移除成员</Button>
            </div>
            {invalid && <div className="text-sm text-warning">
              {rebound ? '保存时将明确绑定到当前平台实例' : '原平台已移除或重建, 此身份授权已失效'}
              {platform && !rebound && <Button className="ml-2" onClick={() => {
                setRebind(current => [...current, { group_id: group.id, platform_id: member.platform_id, user_id: member.user_id }]);
                setPreview(null);
              }}>绑定到当前平台</Button>}
            </div>}
          </div>;
        })}
        <Button onClick={() => change(group.id, current => ({
          ...current,
          members: [...current.members, { platform_id: '', user_id: '' }],
        }))}>添加管理员成员</Button>
      </div>
      <label className="block text-sm">适用插件
        <select aria-label="适用插件" className={selectClass} value={group.plugin_scope.mode}
          onChange={event => change(group.id, current => ({
            ...current,
            plugin_scope: { ...current.plugin_scope, mode: event.target.value as 'selected' | 'all', included: [] },
          }))}>
          <option value="selected">指定插件</option><option value="all">所有已接入插件, 包含未来接入的插件</option>
        </select>
      </label>
      <p className="text-sm text-text-secondary">管理组只授予插件范围, 名单仅影响系统管理员授权; 插件原名单仍可独立授权。需要否决某人时使用成员例外。</p>
      {group.plugin_scope.mode === 'selected' ? <>
        <PluginListSection title="已允许的插件" names={group.plugin_scope.included} snapshot={snapshot}
          addLabel="添加允许的插件" taken={[...group.plugin_scope.included, ...group.plugin_scope.excluded]} platformIds={groupPlatforms}
          onAdd={name => change(group.id, current => ({
            ...current,
            plugin_scope: { ...current.plugin_scope, included: [...current.plugin_scope.included, name] },
          }))}
          onRemove={name => change(group.id, current => ({
            ...current,
            plugin_scope: { ...current.plugin_scope, included: current.plugin_scope.included.filter(item => item !== name) },
          }))} />
        {group.plugin_scope.excluded.length > 0 &&
          <PluginListSection title="旧版排除" names={group.plugin_scope.excluded} snapshot={snapshot}
            platformIds={groupPlatforms}
            hint="旧版指定插件模式的排除仍按原规则生效, 只可移除; 新否决请使用成员例外, 读取时已按当前成员自动展开。"
            onRemove={name => change(group.id, current => ({
              ...current,
              plugin_scope: { ...current.plugin_scope, excluded: current.plugin_scope.excluded.filter(item => item !== name) },
            }))} />}
      </> : <PluginListSection title="已排除的插件" names={group.plugin_scope.excluded} snapshot={snapshot}
        hint="未列出的插件全部授予, 包含未来接入的插件; 排除作用于全部成员。"
        addLabel="添加排除的插件" platformIds={groupPlatforms}
        onAdd={name => change(group.id, current => ({
          ...current,
          plugin_scope: { ...current.plugin_scope, excluded: [...current.plugin_scope.excluded, name] },
        }))}
        onRemove={name => change(group.id, current => ({
          ...current,
          plugin_scope: { ...current.plugin_scope, excluded: current.plugin_scope.excluded.filter(item => item !== name) },
        }))} />}
      </fieldset>
      </Modal>
      </div>;
    })}
    </div>}
    {!!overrides.length && <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
    {overrides.map(entry => {
      const platform = snapshot?.platforms.find(item => item.id === entry.platform_id);
      const invalid = entry.platform_instance_id && platform?.instance_id !== entry.platform_instance_id;
      const rebound = rebindOverrides.some(item => item.override_id === entry.id && item.platform_id === entry.platform_id && item.user_id === entry.user_id);
      const entryName = entry.platform_id && entry.user_id ? `${entry.platform_id} / ${entry.user_id}` : '未填写身份';
      return <div key={entry.id} aria-label={`成员例外卡片 ${entryName}`}
        className="rounded-md border border-border-glass p-4">
        <button type="button" disabled={busy || loading} onClick={() => setOpenCard(entry.id)}
          className="flex w-full flex-col items-start gap-1 text-left text-sm">
          <span className="flex w-full items-center gap-2">
            <span className="font-medium">{entryName}</span>
            <span className="ml-auto shrink-0 text-text-secondary">配置 ▸</span>
          </span>
          <span className="text-text-secondary">{entry.enabled ? '启用' : '停用'} · {entry.protect ? '保护账号' : '不保护账号'} · 允许 {entry.allow.length} 个 · 否决 {entry.deny.length} 个{invalid ? ' · 身份已失效' : ''}</span>
        </button>
        <Modal open={openCard === entry.id} onClose={() => setOpenCard(null)} title={`成员例外 ${entryName}`} size="3xl">
        <fieldset disabled={busy || loading} className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={entry.enabled}
              onChange={event => changeOverride(entry.id, current => ({ ...current, enabled: event.target.checked }))} />启用此条目
          </label>
          <div className="flex items-center gap-2 text-sm">
            <span id={`override-protect-${entry.id}`}>账号保护</span>
            <Toggle checked={entry.protect === true} title="此身份加入好友删除保护名单"
              onChange={value => changeOverride(entry.id, current => ({ ...current, protect: value }))} />
          </div>
          <Button onClick={() => {
            setOverrides(current => current.filter(item => item.id !== entry.id));
            setRebindOverrides(current => current.filter(item => item.override_id !== entry.id));
            setOpenCard(null);
            setPreview(null);
          }}>删除例外</Button>
        </div>
        <div className="grid items-end gap-2 md:grid-cols-[1fr_1fr]">
          <label className="text-sm">平台
            <select aria-label="例外平台" className={selectClass} value={entry.platform_id}
              onChange={event => changeOverride(entry.id, current => ({ ...current, platform_id: event.target.value }))}>
              <option value="">选择已配置的平台</option>
              {!platform && entry.platform_id && <option value={entry.platform_id}>{entry.platform_id} · 平台已移除</option>}
              {snapshot?.platforms.map(item => <option key={item.id} value={item.id}>{item.name || item.id} · {item.type}{item.enabled ? '' : ' · 已停用'}</option>)}
            </select>
          </label>
          <label className="text-sm">平台用户识别号
            <Input value={entry.user_id} maxLength={256} placeholder="填写用户 ID, 不是昵称"
              onChange={event => changeOverride(entry.id, current => ({ ...current, user_id: event.target.value }))} />
          </label>
        </div>
        {invalid && <div className="text-sm text-warning">
          {rebound ? '保存时将明确绑定到当前平台实例' : '原平台已移除或重建, 此例外已失效'}
          {platform && !rebound && <Button className="ml-2" onClick={() => {
            setRebindOverrides(current => [...current, { override_id: entry.id, platform_id: entry.platform_id, user_id: entry.user_id }]);
            setPreview(null);
          }}>绑定到当前平台</Button>}
        </div>}
        <p className="text-sm text-text-secondary">允许为本不属于任何组的人补充授权; 否决压过所有管理组的允许, 包括其他组的允许。同一插件加入一侧名单时会自动移出另一侧。</p>
        <PluginListSection title="额外允许的插件" names={entry.allow} snapshot={snapshot}
          addLabel="添加允许的插件" taken={entry.allow} platformIds={platform ? [entry.platform_id] : []}
          onAdd={name => changeOverride(entry.id, current => ({
            ...current, allow: [...current.allow, name], deny: current.deny.filter(item => item !== name),
          }))}
          onRemove={name => changeOverride(entry.id, current => ({
            ...current, allow: current.allow.filter(item => item !== name),
          }))} />
        <PluginListSection title="否决的插件" names={entry.deny} snapshot={snapshot}
          hint="否决压过此人的全部允许来源。" addLabel="添加否决的插件" taken={entry.deny} platformIds={platform ? [entry.platform_id] : []}
          onAdd={name => changeOverride(entry.id, current => ({
            ...current, deny: [...current.deny, name], allow: current.allow.filter(item => item !== name),
          }))}
          onRemove={name => changeOverride(entry.id, current => ({
            ...current, deny: current.deny.filter(item => item !== name),
          }))} />
        </fieldset>
        </Modal>
      </div>;
    })}
    </div>}
    {preview && <div className="space-y-3 rounded-md border border-border-glass p-4" aria-label="有效权限预览">
      <p className="font-medium">草稿的有效管理员权限, 尚未保存</p>
      {preview.members.map(member => <div key={`${member.platform_id}/${member.user_id}`} className="text-sm">
        <p>{member.platform_id} / {member.user_id}{member.override_id ? ' · 成员例外' : ''}</p>
        {member.plugins.length ? <ul className="space-y-1">{member.plugins.map(plugin => <li key={plugin.name}>
          <span className={plugin.allowed ? 'font-medium' : 'text-text-secondary line-through'}>{plugin.name}</span>
          <span className={`ml-1.5 rounded border px-1.5 py-0.5 text-xs ${plugin.allowed ? 'border-border-glass text-text-secondary' : 'border-error text-error'}`}>{plugin.allowed ? '有效' : '无效'}</span>
          <span className="text-text-secondary">: {plugin.permissions.map(item => item.description).join('、')}</span>
          {!!plugin.sources?.allow.length && <span className="text-text-secondary"> · 允许来源: {plugin.sources.allow.join(', ')}</span>}
          {!!plugin.sources?.deny.length && <span className="text-error"> · 否决来源: {plugin.sources.deny.join(', ')}</span>}
        </li>)}</ul> : <p className="text-text-secondary">未获得插件管理员权限</p>}
      </div>)}
    </div>}
  </div>;
}
