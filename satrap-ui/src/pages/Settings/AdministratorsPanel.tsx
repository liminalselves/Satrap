import { useCallback, useEffect, useRef, useState } from 'react';
import { administratorsApi, administratorError, administratorRuntimeLabel, type AdministratorGroup, type AdministratorPreview, type AdministratorRebind, type AdministratorSnapshot } from '@/api/administrators';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { toast } from '@/components/ui/Toast';

export function AdministratorsPanel({ active, onSaved }: { active: boolean; onSaved?: (groups: AdministratorGroup[], revision: string, previousRevision: string) => void }) {
  const [snapshot, setSnapshot] = useState<AdministratorSnapshot | null>(null);
  const [groups, setGroups] = useState<AdministratorGroup[]>([]);
  const [rebind, setRebind] = useState<AdministratorRebind[]>([]);
  const [preview, setPreview] = useState<AdministratorPreview | null>(null);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [comparison, setComparison] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const dirty = snapshot !== null && (JSON.stringify(groups) !== JSON.stringify(snapshot.groups) || rebind.length > 0);
  const dirtyRef = useRef(dirty);
  const busyRef = useRef(busy);
  const requests = useRef(0);
  dirtyRef.current = dirty;
  busyRef.current = busy;
  const currentRebind = rebind.filter(item => groups.some(group => group.id === item.group_id && group.members.some(member => member.platform_id === item.platform_id && member.user_id === item.user_id)));

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
        setRebind([]);
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

  const save = async () => {
    if (!snapshot || busyRef.current) return;
    requests.current++;
    busyRef.current = true;
    setBusy(true);
    setLoading(false);
    setError('');
    try {
      const value = await administratorsApi.save(groups, snapshot.revision, currentRebind);
      setSnapshot(value);
      setGroups(value.groups);
      setRebind([]);
      setConflict(false);
      setComparison(false);
      setPreview(null);
      onSaved?.(value.groups, value.revision, snapshot.revision);
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
      if (action === 'preview') setPreview(await administratorsApi.preview(groups, currentRebind));
      else if (action === 'apply') {
        const value = await administratorsApi.apply(snapshot.section_revision);
        setSnapshot(current => current && { ...current, runtime: value.runtime });
        toast(value.runtime.status === 'applied' ? 'success' : 'warning', administratorRuntimeLabel(value.runtime));
      } else {
        const value = await administratorsApi.read();
        setSnapshot(value);
        setConflict(false);
        setComparison(true);
        toast('warning', '已保留草稿并读取最新版本, 请比较管理组后再保存');
      }
    } catch (cause) {
      setError(administratorError(cause));
    } finally {
      setBusy(false);
      busyRef.current = false;
      setLoading(false);
    }
  };

  const selectClass = 'glass-input w-full';
  return <div className="space-y-4 pt-4" aria-label="管理员设置">
    <p className="text-sm text-text-secondary">管理员身份来自真实平台消息。授权插件后可免填该插件的调用者名单, 功能开关、操作范围和审批仍然生效。</p>
    <div className="flex flex-wrap gap-2">
      <Button disabled={busy || loading} onClick={() => void load()}>刷新目录与状态</Button>
      <Button disabled={!snapshot || busy} onClick={() => {
        setGroups(current => [...current, { id: crypto.randomUUID(), name: '', enabled: true, members: [], plugin_scope: { mode: 'selected', included: [], excluded: [] } }]);
        setPreview(null);
      }}>新增管理组</Button>
      <Button disabled={!snapshot || busy} onClick={() => void run('preview')}>预览有效权限</Button>
      <Button variant="primary" disabled={!snapshot || !dirty || busy} onClick={() => void save()}>保存并应用</Button>
    </div>
    {loading && <p className="text-sm text-text-secondary">正在读取管理员设置…</p>}
    {error && <div role="alert" className="rounded-md border border-red-500/30 p-3 text-sm text-red-400">{error}</div>}
    {!!snapshot?.plugin_errors?.length && <div role="alert" className="rounded-md border border-border-glass p-3 text-sm"><p>以下插件无法加载, 不会授予管理员权限:</p><ul>{snapshot.plugin_errors.map((item, index) => <li key={`${item.name}-${index}`}>{item.name}: {item.error}</li>)}</ul></div>}
    {(conflict || comparison) && <div className="space-y-2 rounded-md border border-border-glass p-3 text-sm">
      <p>{conflict ? '配置已被其他操作修改, 草稿已保留。请读取最新版本, 比较差异后再保存。' : '已读取最新版本, 以下显示已保存的配置。编辑区仍是你的草稿, 请比较后再保存。'}</p>
      {conflict && <Button disabled={busy} onClick={() => void run('rebase')}>保留草稿并读取最新版本</Button>}
      <details open={comparison}><summary>最近读取的管理组</summary><ul className="space-y-2">{snapshot?.groups.map(group => <li key={group.id}><p>{group.name} · {group.enabled ? '启用' : '停用'}</p><p>成员: {group.members.map(member => `${member.platform_id} / ${member.user_id}`).join(', ') || '无'}</p><p>适用插件: {group.plugin_scope.mode === 'all' ? '所有已接入插件' : group.plugin_scope.included.join(', ') || '无'}; 排除: {group.plugin_scope.excluded.join(', ') || '无'}</p></li>)}</ul></details>
    </div>}
    {snapshot && <div className="rounded-md border border-border-glass p-3 text-sm">
      <p>{administratorRuntimeLabel(snapshot.runtime)}{dirty ? ' · 当前修改尚未保存' : ''}</p>
      {snapshot.runtime.error && <p className="text-text-secondary">{snapshot.runtime.error}</p>}
      {snapshot.runtime.status === 'unconfirmed' && <Button className="mt-2" disabled={busy || dirty} onClick={() => void run('apply')}>重试应用已保存配置</Button>}
    </div>}
    {snapshot && !groups.length && <p className="text-text-secondary">尚未配置管理组。各插件继续使用自己的原有名单。</p>}
    {groups.map(group => <fieldset key={group.id} disabled={busy || loading} className="space-y-4 rounded-md border border-border-glass p-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="min-w-48 flex-1 text-sm">管理组名称<Input value={group.name} maxLength={128} placeholder="例如: 主要管理员" onChange={event => change(group.id, current => ({ ...current, name: event.target.value }))} /></label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={group.enabled} onChange={event => change(group.id, current => ({ ...current, enabled: event.target.checked }))} />启用此组</label>
        <Button onClick={() => { setGroups(current => current.filter(item => item.id !== group.id)); setRebind(current => current.filter(item => item.group_id !== group.id)); setPreview(null); }}>删除管理组</Button>
      </div>
      <div className="space-y-2">
        <p className="text-sm font-medium">管理员成员</p>
        {group.members.map((member, index) => {
          const platform = snapshot?.platforms.find(item => item.id === member.platform_id);
          const invalid = member.platform_instance_id && platform?.instance_id !== member.platform_instance_id;
          const rebound = rebind.some(item => item.group_id === group.id && item.platform_id === member.platform_id && item.user_id === member.user_id);
          return <div key={index} className="space-y-2 rounded-md bg-glass p-3">
            <div className="grid items-end gap-2 md:grid-cols-[1fr_1fr_auto]">
              <label className="text-sm">平台<select aria-label="管理员平台" className={selectClass} value={member.platform_id} onChange={event => change(group.id, current => ({ ...current, members: current.members.map((item, at) => at === index ? { platform_id: event.target.value, user_id: item.user_id } : item) }))}>
                <option value="">选择已配置的平台</option>
                {!platform && member.platform_id && <option value={member.platform_id}>{member.platform_id} · 平台已移除</option>}
                {snapshot?.platforms.map(item => <option key={item.id} value={item.id}>{item.name || item.id} · {item.type}{item.enabled ? '' : ' · 已停用'}</option>)}
              </select></label>
              <label className="text-sm">平台用户识别号<Input value={member.user_id} maxLength={256} placeholder="填写用户 ID, 不是昵称" onChange={event => change(group.id, current => ({ ...current, members: current.members.map((item, at) => at === index ? { ...item, user_id: event.target.value } : item) }))} /></label>
              <Button onClick={() => change(group.id, current => ({ ...current, members: current.members.filter((_, at) => at !== index) }))}>移除成员</Button>
            </div>
            {invalid && <div className="text-sm text-amber-500">{rebound ? '保存时将明确绑定到当前平台实例' : '原平台已移除或重建, 此身份授权已失效'}{platform && !rebound && <Button className="ml-2" onClick={() => { setRebind(current => [...current, { group_id: group.id, platform_id: member.platform_id, user_id: member.user_id }]); setPreview(null); }}>绑定到当前平台</Button>}</div>}
          </div>;
        })}
        <Button onClick={() => change(group.id, current => ({ ...current, members: [...current.members, { platform_id: '', user_id: '' }] }))}>添加管理员成员</Button>
      </div>
      <label className="block text-sm">适用插件<select aria-label="适用插件" className={selectClass} value={group.plugin_scope.mode} onChange={event => change(group.id, current => ({ ...current, plugin_scope: { ...current.plugin_scope, mode: event.target.value as 'selected' | 'all', included: [] } }))}>
        <option value="selected">指定插件</option><option value="all">所有已接入插件, 包含未来接入的插件</option>
      </select></label>
      <p className="text-sm text-text-secondary">排除优先于其他管理组的允许, 仅影响系统管理员授权。插件原名单仍可独立授权。</p>
      <div className="grid gap-3 md:grid-cols-2">
        {[...new Set([...(snapshot?.plugins.map(item => item.name) || []), ...group.plugin_scope.included, ...group.plugin_scope.excluded])].map(name => {
          const plugin = snapshot?.plugins.find(item => item.name === name);
          return <div key={name} className="space-y-2 rounded-md border border-border-glass p-3 text-sm">
            <p className="font-medium">{name}{!plugin ? ' · 暂不可用' : !plugin.supports_administrators ? ' · 未接入' : ''}</p>
            {plugin?.supports_administrators && <ul className="text-text-secondary">{Object.entries(plugin.management_permissions).filter(([, rule]) => rule.system_admin).map(([id, rule]) => <li key={id}>{rule.description}{!!rule.requirements?.length && <p className="mt-1 text-xs">{rule.requirements.join('；')}</p>}</li>)}</ul>}
            <div className="flex gap-4">
              {group.plugin_scope.mode === 'selected' && <label className="flex items-center gap-1"><input type="checkbox" checked={group.plugin_scope.included.includes(name)} disabled={!!plugin && !plugin.supports_administrators && !group.plugin_scope.included.includes(name)} onChange={event => change(group.id, current => ({ ...current, plugin_scope: { ...current.plugin_scope, included: event.target.checked ? [...current.plugin_scope.included, name] : current.plugin_scope.included.filter(item => item !== name) } }))} />允许</label>}
              <label className="flex items-center gap-1"><input type="checkbox" checked={group.plugin_scope.excluded.includes(name)} onChange={event => change(group.id, current => ({ ...current, plugin_scope: { ...current.plugin_scope, excluded: event.target.checked ? [...current.plugin_scope.excluded, name] : current.plugin_scope.excluded.filter(item => item !== name) } }))} />排除</label>
            </div>
          </div>;
        })}
      </div>
    </fieldset>)}
    {preview && <div className="space-y-3 rounded-md border border-border-glass p-4" aria-label="有效权限预览">
      <p className="font-medium">草稿的有效管理员权限, 尚未保存</p>
      {preview.members.map(member => <div key={`${member.platform_id}/${member.user_id}`} className="text-sm"><p>{member.platform_id} / {member.user_id}</p>
        {member.plugins.length ? <ul>{member.plugins.map(plugin => <li key={plugin.name}>{plugin.name}: {plugin.permissions.map(item => item.description).join('、')}</li>)}</ul> : <p className="text-text-secondary">未获得插件管理员权限</p>}
      </div>)}
    </div>}
  </div>;
}
