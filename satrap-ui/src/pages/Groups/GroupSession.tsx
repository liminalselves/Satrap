import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, Bot, CheckCircle2, Clock3, Layers, Route, Save, UserCheck } from 'lucide-react';
import { groupApi, type GroupPolicyValue } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { Modal } from '@/components/kit/Modal';
import { Textarea } from '@/components/kit/Field';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';
import { useGroupContext } from './GroupLayout';
import { FieldBlock, Note, SectionHead } from './shared';
import { Select as KitSelect } from '@/components/kit/Select';

type Provider = 'session_class' | 'edictum';
const sourceLabels: Record<string, string> = { platform: '平台默认', conversation_kind: '群聊类型设置', group: '本群覆盖', named_config: 'Agent 命名配置' };
const scopeLabels: Record<string, string> = { group_shared: '全群共享', group_member: '按成员隔离', legacy_user: '沿用平台旧范围' };
const scopeLabel = (scope: string) => scopeLabels[scope] || scope;
const providerLabels: Record<string, string> = { session_class: 'SessionClass', edictum: 'Edictum' };
const providerLabel = (provider: string) => providerLabels[provider] || provider;
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
      }, isRunning);
      setConfig(saved);
      const fresh = fromConfig(saved.explicit.session || {});
      setDraft(fresh);
      setBaseline(JSON.stringify(fresh));
      setError('');
      setShowImpact(false);
    } catch (caught) { setError(errorText(caught)); }
    finally { setSaving(false); }
  };

  return <div className="space-y-4 animate-fade">
    {error && <Note tone="red" title="无法保存">{error}</Note>}
    {config.apply_status === 'failed' && (
      <Note tone="red" title="配置应用失败" icon={<AlertTriangle className="h-4 w-4" />}
        action={<Button size="sm" variant="danger" loading={applying} onClick={retryApply} disabled={!isRunning || historical || dirty}>重试应用</Button>}>
        {config.apply_error || '未提供失败原因'}
      </Note>
    )}

    <Card padding="lg">
      <SectionHead tint="pink" title="会话绑定" icon={<Route className="h-4 w-4" />} description="平台默认 → 群聊类型设置 → 本群覆盖" />
      <div className="mb-3 space-y-1.5 rounded-xl border border-line bg-glass p-3">
        {config.binding_chain?.map((layer) => (
          <div key={layer.source} className="flex items-center gap-2 text-[12px]">
            <span className={`h-1.5 w-1.5 rounded-full ${layer.source === 'group' ? 'bg-pink' : layer.source === 'conversation_kind' ? 'bg-purple' : 'bg-fg-4'}`} />
            <span className="w-24 flex-none text-fg-3">{sourceLabels[layer.source] || layer.source}</span>
            <span className="font-mono text-[11.5px] text-fg-2">{layer.mode === 'inherit' ? '继承 · ' : ''}{providerLabel(layer.provider)} / {layer.config_name || '未指定'}</span>
          </div>
        ))}
        <p className="border-t border-line pt-2 text-[12px] text-fg-3">当前: <span className="font-mono text-fg-2">{providerLabel(binding?.provider || '') || '未知'} / {binding?.config_name || '未指定'}</span> · 来源 <span className="text-fg-2">{sourceLabels[config.sources.session.binding] || config.sources.session.binding || '平台默认'}</span></p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <FieldBlock label="绑定来源">
          <KitSelect className="field w-full" value={draft.bindingMode}
            onChange={(value) => setDraft((old) => ({ ...old, bindingMode: value as Draft['bindingMode'] }))}
            disabled={historical || saving}>
            <option value="inherit">继承群聊 Agent（未指定时使用平台默认）</option><option value="value">本群指定</option>
          </KitSelect>
        </FieldBlock>
        {draft.bindingMode === 'value' && <>
          <FieldBlock label="流程">
            <KitSelect className="field w-full" value={draft.provider}
              onChange={(value) => setDraft((old) => ({ ...old, provider: value as Provider, configName: '' }))}
              disabled={historical || saving}>
              <option value="session_class">SessionClass</option><option value="edictum">Edictum</option>
            </KitSelect>
          </FieldBlock>
          <FieldBlock label="命名配置" className="sm:col-span-2"
            hint={selected ? selected.description : draft.configName ? '所选配置已删除, 请重新选择' : '请选择一个可用的命名配置'}>
            <KitSelect className="field w-full" value={draft.configName}
              onChange={(value) => setDraft((old) => ({ ...old, configName: value }))}
              disabled={historical || saving || loadingOptions}>
              <option value="">{loadingOptions ? '加载中' : '请选择命名配置'}</option>
              {options.filter((item) => item.provider === draft.provider).map((item) => (
                <option key={`${item.provider}:${item.config_name}`} value={item.config_name} disabled={!item.enabled || !item.available}>
                  {item.config_name}{item.enabled && item.available ? '' : ' · 不可用'}
                </option>
              ))}
              {draft.configName && !selected && <option value={draft.configName} disabled>{draft.configName} · 已删除</option>}
            </KitSelect>
          </FieldBlock>
        </>}
      </div>
      {draft.bindingMode === 'value' && selected && (!selected.available || !selected.enabled) && <Note tone="orange" className="mt-2">所选命名配置已删除或停用, 请重新选择。</Note>}
      <p className="mt-3 text-[11.5px] text-fg-3">
        命名配置与模型资源在原管理页维护; 修改原配置可能影响其他群。
        <Link className="ml-1 text-pink hover:underline" to="/agents">打开或复制命名配置</Link>
        <Link className="ml-1 text-pink hover:underline" to="/models">创建模型配置</Link>
      </p>
      <div className="mt-3 grid gap-2 rounded-xl border border-line bg-glass p-3 text-[11.5px] text-fg-3 sm:grid-cols-3">
        <div>当前路由可归属实例: <span className="num font-medium text-fg-2">{instanceSummary?.current_route_count ?? '待读取'}</span></div>
        <div>含旧路由共 <span className="num font-medium text-fg-2">{instanceSummary?.known_scoped_count ?? '待读取'}</span></div>
        <div>实例显式覆盖: 模型 {instanceSummary?.override_counts.model ?? '—'}, 提示词 {instanceSummary?.override_counts.prompt ?? '—'}, 插件 {instanceSummary?.override_counts.plugins ?? '—'}</div>
        <div className="break-all sm:col-span-3">
          {instanceSummary?.session_ids.length
            ? <>本群可归属实例 ID: {instanceSummary.session_ids.map((id) => <span key={id} className="num mr-2 font-mono text-fg-2">{id}</span>)}</>
            : <>本群可归属实例 ID: 暂无</>}
          <Link className="ml-1 text-pink hover:underline" to={`/conversations/instances?${new URLSearchParams({ groupAdapter: adapterId, groupAccount: account, groupId })}`}>查看本群可归属实例</Link>
        </div>
      </div>
      <p className="mt-2 text-[11px] text-fg-4">旧版按用户共享的历史无法可靠按群统计。</p>
    </Card>

    <Card padding="lg">
      <SectionHead tint="pink" title="模型与提示词" icon={<Bot className="h-4 w-4" />}
        description={`命名配置 → 本群覆盖 → 会话实例覆盖。当前模型 ${String(config.effective.session.model || '未配置')} (${config.sources.session.model || '未知'})`} />
      <label className="block text-[13px] font-medium text-fg-2">模型来源
        <KitSelect className="field mt-1.5 block w-full max-w-md" value={draft.modelMode} disabled={historical || saving}
          onChange={(value) => setDraft((old) => ({ ...old, modelMode: value as Draft['modelMode'] }))}>
          <option value="inherit">继承命名配置</option><option value="value">本群指定</option>
        </KitSelect>
        {draft.modelMode === 'value' && sessionFields.includes('model') && <KitSelect className="field mt-2 block w-full max-w-md" aria-label="模型配置" value={draft.model} disabled={historical || saving}
          onChange={(value) => setDraft((old) => ({ ...old, model: value }))}>
          <option value="">选择模型配置</option>{models.map((name) => <option key={name} value={name}>{name}</option>)}
        </KitSelect>}
        {draft.modelMode === 'value' && !sessionFields.includes('model') && <span className="mt-1 block text-[11.5px] font-normal text-orange">当前流程不支持群级模型覆盖</span>}
      </label>
      <label className="mt-4 block text-[13px] font-medium text-fg-2">系统提示词 · 当前来源: {config.sources.session.prompt || '命名配置'}
        <KitSelect className="field mt-1.5 block w-full max-w-md" value={draft.promptMode} disabled={historical || saving}
          onChange={(value) => setDraft((old) => ({ ...old, promptMode: value as Draft['promptMode'] }))}>
          <option value="inherit">继承命名配置</option><option value="value">本群替换</option>
        </KitSelect>
        {draft.promptMode === 'value' && sessionFields.includes('prompt') && <>
          <Textarea rows={4} className="mt-2 block w-full max-w-3xl !text-[12.5px]" value={draft.prompt} maxLength={20000}
            onChange={(event) => setDraft((old) => ({ ...old, prompt: event.target.value }))} disabled={historical || saving} />
          <span className="mt-1 block text-[11.5px] font-normal text-fg-3">{draft.prompt ? `${draft.prompt.length} / 20000 字符` : '显式清空'}</span>
        </>}
        {draft.promptMode === 'value' && !sessionFields.includes('prompt') && <span className="mt-1 block text-[11.5px] font-normal text-orange">当前流程不支持群级提示词覆盖</span>}
      </label>
    </Card>

    {unsupportedOverrides.length > 0 && (
      <Note tone="orange" title={`目标配置不支持已有的 ${unsupportedOverrides.join('、')} 覆盖`}
        action={<div className="flex flex-wrap gap-1.5">
          {unsupportedOverrides.includes('模型') && <Button size="sm" variant="ghost" onClick={() => setDraft((old) => ({ ...old, modelMode: 'inherit' }))}>模型恢复继承</Button>}
          {unsupportedOverrides.includes('提示词') && <Button size="sm" variant="ghost" onClick={() => setDraft((old) => ({ ...old, promptMode: 'inherit' }))}>提示词恢复继承</Button>}
          {unsupportedOverrides.includes('插件') && <Button size="sm" variant="ghost" onClick={() => setDraft((old) => ({ ...old, plugins: [] }))}>插件恢复继承</Button>}
        </div>}>
        请明确恢复继承后再保存, 现有值不会自动丢弃。
      </Note>
    )}

    <Card padding="lg">
      <SectionHead tint="pink" title="插件" icon={<Layers className="h-4 w-4" />} description="仅展示插件 schema 允许会话覆盖的字段; 未选择时继承命名配置" />
      {!sessionFields.includes('plugins') && <Note tone="orange" className="mb-3">当前流程不支持群级插件覆盖。</Note>}
      <div className="space-y-2">
        {plugins.map((plugin) => {
          const entry = pluginDraft(plugin.name);
          return (
            <div key={plugin.name} className="rounded-xl border border-line bg-glass p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-medium text-fg">{plugin.name}</span>
                <span className="text-[11.5px] text-fg-3">{plugin.description}</span>
                {entry && <Badge tone={entry.mode === 'enabled' ? 'green' : 'neutral'}>{entry.mode === 'enabled' ? '本群启用' : '本群停用'}</Badge>}
                <label className="ml-auto text-[12px] text-fg-3">
                  <span className="sr-only">{plugin.name} 覆盖</span>
                  <KitSelect className="field !h-8 !w-[110px] !text-[12px]" value={entry?.mode || 'inherit'} disabled={historical || saving || !sessionFields.includes('plugins')}
                    aria-label={`${plugin.name} 覆盖`}
                    onChange={(value) => changePlugin(plugin.name, value as 'inherit' | 'enabled' | 'disabled')}>
                    <option value="inherit">继承</option><option value="enabled">启用</option><option value="disabled">停用</option>
                  </KitSelect>
                </label>
              </div>
              {entry?.mode === 'enabled' && Object.entries(plugin.config_schema).filter(([, field]) => field.session_overridable).map(([key, field]) => {
                const value = entry.config[key];
                const update = (raw: string) => changePluginField(plugin.name, key,
                  raw === '' && field.type !== 'string' && field.type !== 'textarea' ? undefined
                    : field.type === 'number' ? Number(raw) : field.type === 'bool' ? raw === 'true'
                    : field.type === 'knowledge_bases' ? raw.split(',').map((part) => part.trim()).filter(Boolean) : raw);
                return <div key={key} className="mt-2 grid gap-1.5 border-t border-line pt-2 sm:grid-cols-[150px_1fr] sm:items-center">
                  <span className="text-[12px] text-fg-2">{key} <span className="text-[10.5px] text-fg-4">{field.description}</span></span>
                  <div className="flex items-center gap-1.5">
                    {field.type === 'bool' || field.type === 'select' ? (
                      <KitSelect className="field !h-8 !text-[12px]" aria-label={key} value={value === undefined ? '' : String(value)} disabled={historical || saving}
                        onChange={(value) => update(value)}>
                        <option value="">继承插件参数</option>
                        {(field.type === 'bool' ? ['true', 'false'] : (field.options || []).map(String)).map((option) =>
                          <option key={option} value={option}>{option}</option>)}
                      </KitSelect>
                    ) : (
                      <input className="field !h-8 !text-[12px]" aria-label={key} type={field.type === 'number' ? 'number' : 'text'}
                        min={field.minimum} max={field.maximum} step={field.integer ? 1 : 'any'}
                        value={Array.isArray(value) ? value.join(', ') : String(value ?? '')} disabled={historical || saving}
                        onChange={(event) => update(event.target.value)} />
                    )}
                    {value !== undefined && <Button size="sm" variant="ghost" onClick={() => changePluginField(plugin.name, key, undefined)}>恢复继承</Button>}
                  </div>
                </div>;
              })}
            </div>
          );
        })}
        {plugins.length === 0 && <p className="text-[12px] text-fg-3">当前平台未返回可覆盖的插件。</p>}
      </div>
    </Card>

    <Card padding="lg">
      <SectionHead tint="pink" title="会话范围" icon={<UserCheck className="h-4 w-4" />}
        description={`当前: ${scopeLabel(effectiveScope)} · 来源: ${sourceLabels[config.sources.session.scope] || config.sources.session.scope || '平台默认'} · 切换后后续请求创建新会话`} />
      <div className="grid gap-3 sm:grid-cols-2">
        <FieldBlock label="范围来源">
          <KitSelect className="field w-full" value={draft.scopeMode}
            onChange={(value) => setDraft((old) => ({ ...old, scopeMode: value as Draft['scopeMode'] }))}
            disabled={historical || saving}>
            <option value="inherit">继承平台</option><option value="value">本群指定</option>
          </KitSelect>
        </FieldBlock>
        {draft.scopeMode === 'value' && (
          <FieldBlock label="本群范围">
            <KitSelect className="field w-full" value={draft.scope}
              onChange={(value) => setDraft((old) => ({ ...old, scope: value as Draft['scope'] }))}
              disabled={historical || saving}>
              <option value="group_member">按成员隔离</option><option value="group_shared">全群共享</option>
            </KitSelect>
          </FieldBlock>
        )}
      </div>
      <p className="mt-2 text-[11.5px] text-fg-3">切换绑定或范围后, 后续请求创建新会话。原历史保留且不自动合并。</p>
    </Card>

    {!historical && <Card padding="md" className="flex flex-wrap items-center justify-between gap-3">
      <span className="flex items-center gap-2 text-[12px] text-fg-3">
        {dirty ? <><Clock3 className="h-3.5 w-3.5 text-orange" />有未保存修改</>
          : config.apply_status === 'applied' ? <><CheckCircle2 className="h-3.5 w-3.5 text-green" />{config.active_instance_count === 0 ? '已保存, 后续实例首轮应用' : '配置已生效'}</>
            : <><AlertTriangle className="h-3.5 w-3.5 text-orange" />将在下次会话安全轮次应用</>}
      </span>
      <div className="flex gap-2">
        {config.apply_status === 'failed' && <Button variant="ghost" loading={applying} onClick={retryApply} disabled={!isRunning || dirty}>重试应用</Button>}
        <Button variant="primary" icon={<Save className="h-4 w-4" />} disabled={!canSave} onClick={() => setShowImpact(true)}>保存并应用</Button>
      </div>
    </Card>}

    <Modal open={showImpact} onClose={() => setShowImpact(false)} title={routeChanged ? '切换会话绑定或范围' : '应用会话覆盖'}
      icon={<Route className="h-[18px] w-[18px]" />} size="md" className="tint-pink"
      footer={<>
        <Button variant="ghost" onClick={() => setShowImpact(false)}>取消</Button>
        <Button variant="primary" loading={saving} onClick={submit}>{routeChanged ? '确认保存并切换' : '确认保存覆盖'}</Button>
      </>}>
      <div className="space-y-2.5 text-[12.5px] text-fg-2">
        <p>旧绑定: <span className="font-mono">{providerLabel(binding?.provider || '') || '平台'} / {binding?.config_name || '未指定'}</span>, 范围 {scopeLabel(effectiveScope)}</p>
        <p>新绑定: <span className="font-mono">{draft.bindingMode === 'inherit' ? '继承群聊 Agent（未指定时使用平台默认）' : `${providerLabel(draft.provider)} / ${draft.configName}`}</span>, 范围 {draft.scopeMode === 'inherit' ? '继承平台' : scopeLabel(draft.scope)}</p>
        <Note tone="orange">{routeChanged ? '此后使用新会话; 原历史保留, 不自动迁移' : '现有会话在下一安全轮次应用覆盖; 历史保留'}</Note>
        {routeChanged && <p className="text-fg-3">当前路由可归属实例 {instanceSummary?.current_route_count ?? '未知'} 个; 旧路由和旧版按用户共享的历史会保留, 其中旧版历史可能未计入。</p>}
      </div>
    </Modal>
  </div>;
}
