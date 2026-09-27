import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '@/api/client';
import { groupApi, type GroupPolicyValue } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { useGroupContext } from './GroupLayout';

type Provider = 'session_class' | 'edictum';
interface Draft {
  bindingMode: 'inherit' | 'value';
  provider: Provider;
  configName: string;
  scopeMode: 'inherit' | 'value';
  scope: 'group_member' | 'group_shared';
  modelMode: 'inherit' | 'value';
  model: string;
  promptMode: 'inherit' | 'value';
  prompt: string;
  plugins: Array<{ name: string; mode: 'enabled' | 'disabled'; config: Record<string, unknown> }>;
}

function fromConfig(explicit: Record<string, GroupPolicyValue>): Draft {
  const binding = explicit.binding?.mode === 'value' && typeof explicit.binding.value === 'object'
    ? explicit.binding.value as { provider?: Provider; config_name?: string } : null;
  const scope = explicit.scope?.mode === 'value' ? explicit.scope.value : null;
  const model = explicit.model?.mode === 'value' ? explicit.model.value : null;
  const prompt = explicit.prompt?.mode === 'value' ? explicit.prompt.value : null;
  const plugins = explicit.plugins?.mode === 'value' && Array.isArray(explicit.plugins.value)
    ? explicit.plugins.value as Draft['plugins'] : [];
  return {
    bindingMode: binding ? 'value' : 'inherit', provider: binding?.provider || 'session_class',
    configName: binding?.config_name || '',
    scopeMode: scope ? 'value' : 'inherit',
    scope: scope === 'group_shared' ? 'group_shared' : 'group_member',
    modelMode: typeof model === 'string' ? 'value' : 'inherit', model: typeof model === 'string' ? model : '',
    promptMode: typeof prompt === 'string' ? 'value' : 'inherit', prompt: typeof prompt === 'string' ? prompt : '',
    plugins,
  };
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '保存失败';
}

export function GroupSession() {
  const { adapterId, groupId, account, isRunning, historical, config, setConfig } = useGroupContext();
  const [draft, setDraft] = useState<Draft>(() => fromConfig(config.explicit.session || {}));
  const [baseline, setBaseline] = useState(() => JSON.stringify(fromConfig(config.explicit.session || {})));
  const [options, setOptions] = useState<Awaited<ReturnType<typeof groupApi.bindingOptions>>['items']>([]);
  const [models, setModels] = useState<string[]>([]);
  const [plugins, setPlugins] = useState<Awaited<ReturnType<typeof groupApi.bindingOptions>>['plugins']>([]);
  const [loadingOptions, setLoadingOptions] = useState(false);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [applying, setApplying] = useState(false);
  const [showImpact, setShowImpact] = useState(false);
  const dirty = JSON.stringify(draft) !== baseline;
  const explicit = config.explicit.session;
  useDirtyGuard(dirty);

  const retryApply = async () => {
    if (!isRunning || historical || applying || dirty) return;
    setApplying(true);
    try { setConfig(await groupApi.applyConfig(adapterId, groupId, account, config.saved_revision)); setError(''); }
    catch (caught) { setError(errorText(caught)); }
    finally { setApplying(false); }
  };

  useEffect(() => {
    if (dirty) return;
    const fresh = fromConfig(explicit || {});
    setDraft(fresh);
    setBaseline(JSON.stringify(fresh));
  }, [config.revision, config.account, dirty, explicit]);

  useEffect(() => {
    if (!isRunning) return;
    let cancelled = false;
    setLoadingOptions(true);
    groupApi.bindingOptions(adapterId, account).then((result) => {
      if (!cancelled) { setOptions(result.items); setModels(result.models || []); setPlugins(result.plugins || []); setError(''); }
    }).catch((caught) => { if (!cancelled) setError(errorText(caught)); })
      .finally(() => { if (!cancelled) setLoadingOptions(false); });
    return () => { cancelled = true; };
  }, [adapterId, account, isRunning]);

  const binding = config.effective.session.binding as { provider?: string; config_name?: string } | undefined;
  const instanceSummary = config.session_instances;
  const effectiveScope = String(config.effective.session.scope || 'legacy_user');
  const selected = options.find((item) => item.provider === draft.provider && item.config_name === draft.configName);
  const previousRoute = fromConfig(config.explicit.session || {});
  const routeChanged = draft.bindingMode !== previousRoute.bindingMode
    || (draft.bindingMode === 'value' && (draft.provider !== previousRoute.provider || draft.configName !== previousRoute.configName))
    || draft.scopeMode !== previousRoute.scopeMode
    || (draft.scopeMode === 'value' && draft.scope !== previousRoute.scope);
  const sessionFields = draft.bindingMode === 'value'
    ? selected?.session_fields || ['binding', 'scope'] : config.capabilities.session_fields;
  const unsupportedOverrides = [
    draft.modelMode === 'value' && !sessionFields.includes('model') ? '模型' : '',
    draft.promptMode === 'value' && !sessionFields.includes('prompt') ? '提示词' : '',
    draft.plugins.length > 0 && !sessionFields.includes('plugins') ? '插件' : '',
  ].filter(Boolean);
  const canSave = !historical && isRunning && !saving && dirty
    && unsupportedOverrides.length === 0
    && (draft.bindingMode === 'inherit' || (!!draft.configName && !!selected?.available && !!selected.enabled))
    && (!sessionFields.includes('model') || draft.modelMode === 'inherit' || models.includes(draft.model))
    && draft.prompt.length <= 20000;
  const pluginDraft = (name: string) => draft.plugins.find((item) => item.name === name);
  const changePlugin = (name: string, mode: 'inherit' | 'enabled' | 'disabled') => {
    setDraft((old) => ({ ...old, plugins: [
      ...old.plugins.filter((item) => item.name !== name),
      ...(mode === 'inherit' ? [] : [{ name, mode, config: old.plugins.find((item) => item.name === name)?.config || {} }]),
    ] }));
  };
  const changePluginField = (name: string, key: string, value: unknown) => {
    setDraft((old) => ({ ...old, plugins: old.plugins.map((item) => item.name === name
      ? { ...item, config: value === undefined
        ? Object.fromEntries(Object.entries(item.config).filter(([field]) => field !== key))
        : { ...item.config, [key]: value } } : item) }));
  };

  const submit = async () => {
    if (!canSave) return;
    const values: Record<string, GroupPolicyValue> = { ...(config.explicit.session as Record<string, GroupPolicyValue> || {}) };
    if (draft.bindingMode === 'value') {
      values.binding = { mode: 'value', value: { provider: draft.provider, config_name: draft.configName } };
    } else delete values.binding;
    if (draft.scopeMode === 'value') values.scope = { mode: 'value', value: draft.scope };
    else delete values.scope;
    if (draft.modelMode === 'value') values.model = { mode: 'value', value: draft.model };
    else delete values.model;
    if (draft.promptMode === 'value') values.prompt = { mode: 'value', value: draft.prompt };
    else delete values.prompt;
    if (draft.plugins.length) values.plugins = { mode: 'value', value: draft.plugins };
    else delete values.plugins;
    setSaving(true);
    try {
      const saved = await groupApi.saveConfig(adapterId, groupId, {
        expected_self_id: account, expected_revision: config.revision,
        base_revision: config.base_revision, section: 'session', values,
      }, true);
      setConfig(saved);
      const fresh = fromConfig(saved.explicit.session || {});
      setDraft(fresh);
      setBaseline(JSON.stringify(fresh));
      setError('');
      setShowImpact(false);
    } catch (caught) { setError(errorText(caught)); }
    finally { setSaving(false); }
  };

  return <div className="space-y-4">
    {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
    <Card className="space-y-4">
      <h2 className="text-lg font-semibold">会话绑定</h2>
      <p className="text-sm text-text-secondary">当前: {binding?.provider || '未知'} / {binding?.config_name || '未指定'} · 来源: {config.sources.session.binding || '平台'}</p>
      <label className="block text-sm">绑定来源
        <select className="glass-input mt-1 w-full max-w-md" value={draft.bindingMode}
          onChange={(event) => setDraft((old) => ({ ...old, bindingMode: event.target.value as Draft['bindingMode'] }))}
          disabled={historical || saving}>
          <option value="inherit">继承平台</option><option value="value">本群指定</option>
        </select>
      </label>
      {draft.bindingMode === 'value' && <div className="grid gap-3 md:grid-cols-2">
        <label className="block text-sm">Provider
          <select className="glass-input mt-1 w-full" value={draft.provider}
            onChange={(event) => setDraft((old) => ({ ...old, provider: event.target.value as Provider, configName: '' }))}
            disabled={historical || saving}>
            <option value="session_class">SessionClass</option><option value="edictum">Edictum</option>
          </select>
        </label>
        <label className="block text-sm">命名配置
          <select className="glass-input mt-1 w-full" value={draft.configName}
            onChange={(event) => setDraft((old) => ({ ...old, configName: event.target.value }))}
            disabled={historical || saving || loadingOptions}>
            <option value="">{loadingOptions ? '加载中' : '请选择命名配置'}</option>
            {options.filter((item) => item.provider === draft.provider).map((item) => (
              <option key={`${item.provider}:${item.config_name}`} value={item.config_name} disabled={!item.enabled || !item.available}>
                {item.config_name}{item.enabled && item.available ? '' : ' · 不可用'}
              </option>
            ))}
            {draft.configName && !selected && <option value={draft.configName} disabled>{draft.configName} · 已删除</option>}
          </select>
        </label>
        {selected && (!selected.available || !selected.enabled) && <p className="text-sm text-warning">所选命名配置已删除或停用, 请重新选择</p>}
      </div>}
      <p className="text-sm text-text-secondary">命名配置与模型资源在原管理页维护; 修改原配置可能影响其他群。
        <Link className="ml-2 text-accent hover:underline" to="/sessions">打开或复制命名配置</Link>
        <Link className="ml-2 text-accent hover:underline" to="/models">创建模型配置</Link>
      </p>
      <div className="space-y-1 text-sm text-text-secondary">
        <p>当前路由可归属实例: {instanceSummary?.current_route_count ?? '待读取'}; 含旧路由共 {instanceSummary?.known_scoped_count ?? '待读取'}。旧版按用户共享的历史无法可靠按群统计</p>
        <p>实例显式覆盖: 模型 {instanceSummary?.override_counts.model ?? '—'}, 提示词 {instanceSummary?.override_counts.prompt ?? '—'}, 插件 {instanceSummary?.override_counts.plugins ?? '—'}</p>
        <Link className="text-accent hover:underline" to={`/sessions?${new URLSearchParams({ groupAdapter: adapterId, groupAccount: account, groupId })}`}>查看本群可归属实例</Link>
        {instanceSummary?.session_ids.length ? <p className="break-all">本群可归属实例 ID: {instanceSummary.session_ids.join(', ')}</p> : null}
      </div>
      {unsupportedOverrides.length > 0 && <div role="alert" className="space-y-2 text-sm text-warning">
        <p>目标配置不支持已有的 {unsupportedOverrides.join('、')} 覆盖。请明确恢复继承后再保存, 现有值不会自动丢弃</p>
        <div className="flex flex-wrap gap-2">
          {unsupportedOverrides.includes('模型') && <Button size="sm" variant="subtle" onClick={() => setDraft((old) => ({ ...old, modelMode: 'inherit' }))}>模型恢复继承</Button>}
          {unsupportedOverrides.includes('提示词') && <Button size="sm" variant="subtle" onClick={() => setDraft((old) => ({ ...old, promptMode: 'inherit' }))}>提示词恢复继承</Button>}
          {unsupportedOverrides.includes('插件') && <Button size="sm" variant="subtle" onClick={() => setDraft((old) => ({ ...old, plugins: [] }))}>插件恢复继承</Button>}
        </div>
      </div>}
    </Card>
    <Card className="space-y-4">
      <h2 className="text-lg font-semibold">模型与提示词</h2>
      <p className="text-sm text-text-secondary">命名配置 → 本群覆盖 → 会话实例覆盖。当前模型: {String(config.effective.session.model || '未配置')} ({config.sources.session.model || '未知'})</p>
      {sessionFields.includes('model') ? <label className="block text-sm">模型来源
        <select className="glass-input mt-1 block w-full max-w-md" value={draft.modelMode} disabled={historical || saving}
          onChange={(event) => setDraft((old) => ({ ...old, modelMode: event.target.value as Draft['modelMode'] }))}>
          <option value="inherit">继承命名配置</option><option value="value">本群指定</option>
        </select>
        {draft.modelMode === 'value' && <select className="glass-input mt-2 block w-full max-w-md" value={draft.model} disabled={historical || saving}
          onChange={(event) => setDraft((old) => ({ ...old, model: event.target.value }))}>
          <option value="">选择模型配置</option>{models.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>}
      </label> : <p className="text-sm text-warning">当前 Provider 不支持群级模型覆盖</p>}
      {sessionFields.includes('prompt') ? <label className="block text-sm">系统提示词 · 当前来源: {config.sources.session.prompt || '命名配置'}
        <select className="glass-input mt-1 block w-full max-w-md" value={draft.promptMode} disabled={historical || saving}
          onChange={(event) => setDraft((old) => ({ ...old, promptMode: event.target.value as Draft['promptMode'] }))}>
          <option value="inherit">继承命名配置</option><option value="value">本群替换</option>
        </select>
        {draft.promptMode === 'value' && <><textarea className="glass-input mt-2 min-h-28 w-full" value={draft.prompt} maxLength={20000}
          onChange={(event) => setDraft((old) => ({ ...old, prompt: event.target.value }))} disabled={historical || saving} />
          <span className="text-xs text-text-secondary">{draft.prompt ? `${draft.prompt.length} / 20000 字符` : '显式清空'}</span></>}
      </label> : <p className="text-sm text-warning">当前 Provider 不支持群级提示词覆盖</p>}
    </Card>
    <Card className="space-y-4">
      <h2 className="text-lg font-semibold">插件</h2>
      <p className="text-sm text-text-secondary">仅展示插件 schema 允许会话覆盖的字段。未选择时继承命名配置</p>
      {!sessionFields.includes('plugins') && <p className="text-sm text-warning">当前 Provider 不支持群级插件覆盖</p>}
      {sessionFields.includes('plugins') && plugins.map((plugin) => {
        const entry = pluginDraft(plugin.name);
        return <div key={plugin.name} className="rounded-lg border border-glass-border p-3 space-y-2 text-sm">
          <label className="block font-medium">{plugin.name} <span className="text-text-secondary">{plugin.description}</span>
            <select className="glass-input mt-1 block w-full max-w-md" value={entry?.mode || 'inherit'} disabled={historical || saving}
              onChange={(event) => changePlugin(plugin.name, event.target.value as 'inherit' | 'enabled' | 'disabled')}>
              <option value="inherit">继承</option><option value="enabled">启用</option><option value="disabled">停用</option>
            </select>
          </label>
          {entry?.mode === 'enabled' && Object.entries(plugin.config_schema).filter(([, field]) => field.session_overridable).map(([key, field]) => {
            const value = entry.config[key];
            const update = (raw: string) => changePluginField(plugin.name, key,
              raw === '' && field.type !== 'string' && field.type !== 'textarea' ? undefined
                : field.type === 'number' ? Number(raw) : field.type === 'bool' ? raw === 'true'
                : field.type === 'knowledge_bases' ? raw.split(',').map((part) => part.trim()).filter(Boolean) : raw);
            return <label key={key} className="block">{key} <span className="text-text-secondary">{field.description}</span>
              {field.type === 'bool' || field.type === 'select' ? <select className="glass-input mt-1 w-full"
                value={value === undefined ? '' : String(value)} disabled={historical || saving}
                onChange={(event) => update(event.target.value)}>
                <option value="">继承插件参数</option>
                {(field.type === 'bool' ? ['true', 'false'] : (field.options || []).map(String)).map((option) =>
                  <option key={option} value={option}>{option}</option>)}
              </select> : <input className="glass-input mt-1 w-full" type={field.type === 'number' ? 'number' : 'text'}
                min={field.minimum} max={field.maximum} step={field.integer ? 1 : 'any'}
                value={Array.isArray(value) ? value.join(', ') : String(value ?? '')} disabled={historical || saving}
                onChange={(event) => update(event.target.value)} />}
              {value !== undefined && <Button size="sm" variant="subtle" onClick={() => changePluginField(plugin.name, key, undefined)}>恢复继承</Button>}
            </label>;
          })}
        </div>;
      })}
    </Card>
    <Card className="space-y-4">
      <h2 className="text-lg font-semibold">会话范围</h2>
      <p className="text-sm text-text-secondary">当前: {effectiveScope === 'group_shared' ? '全群共享' : effectiveScope === 'group_member' ? '按成员隔离' : '沿用平台旧范围'} · 来源: {config.sources.session.scope || '平台'}</p>
      <label className="block text-sm">范围来源
        <select className="glass-input mt-1 w-full max-w-md" value={draft.scopeMode}
          onChange={(event) => setDraft((old) => ({ ...old, scopeMode: event.target.value as Draft['scopeMode'] }))}
          disabled={historical || saving}>
          <option value="inherit">继承平台</option><option value="value">本群指定</option>
        </select>
      </label>
      {draft.scopeMode === 'value' && <label className="block text-sm">本群范围
        <select className="glass-input mt-1 w-full max-w-md" value={draft.scope}
          onChange={(event) => setDraft((old) => ({ ...old, scope: event.target.value as Draft['scope'] }))}
          disabled={historical || saving}>
          <option value="group_member">按成员隔离</option><option value="group_shared">全群共享</option>
        </select>
      </label>}
      <p className="text-sm text-text-secondary">切换绑定或范围后, 后续请求创建新会话。原历史保留且不自动合并</p>
    </Card>
    {!historical && <Card className="flex flex-wrap items-center justify-between gap-3">
      <span className="text-sm text-text-secondary">{dirty ? '有未保存修改' : config.apply_status === 'applied' ? (config.active_instance_count === 0 ? '已保存, 后续实例首轮应用' : '配置已生效') : config.apply_status === 'failed' ? '配置应用失败' : '将在下次会话安全轮次应用'}</span>
      <div className="flex gap-2">
        {config.apply_status === 'failed' && <Button variant="subtle" onClick={retryApply} disabled={!isRunning || applying || dirty}>重试应用</Button>}
        <Button variant="primary" onClick={() => setShowImpact(true)} disabled={!canSave}>保存并应用</Button>
      </div>
    </Card>}
    <Modal open={showImpact} onClose={() => setShowImpact(false)} title={routeChanged ? '切换会话绑定或范围' : '应用会话覆盖'}>
      <div className="space-y-4 text-sm text-text-secondary">
        <p>旧绑定: {binding?.provider || '平台'} / {binding?.config_name || '未指定'}, 范围: {effectiveScope}</p>
        <p>新绑定: {draft.bindingMode === 'inherit' ? '继承平台' : `${draft.provider} / ${draft.configName}`}, 范围: {draft.scopeMode === 'inherit' ? '继承平台' : draft.scope}</p>
          <p>{routeChanged ? '此后使用新会话; 原历史保留, 不自动迁移' : '现有会话在下一安全轮次应用覆盖; 历史保留'}</p>
          {routeChanged && <p>当前路由可归属实例 {instanceSummary?.current_route_count ?? '未知'} 个; 旧路由和旧版按用户共享的历史会保留, 其中旧版历史可能未计入</p>}
        <div className="flex justify-end gap-2"><Button variant="subtle" onClick={() => setShowImpact(false)}>取消</Button><Button variant="primary" onClick={submit} disabled={saving}>{routeChanged ? '确认保存并切换' : '确认保存覆盖'}</Button></div>
      </div>
    </Modal>
  </div>;
}
