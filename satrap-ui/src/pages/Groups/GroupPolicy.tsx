import { useEffect, useMemo, useState } from 'react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupPolicyValue } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';
import { GROUP_OVERRIDE_FIELDS, type OverrideFieldDef } from '@/utils/wakeOverrides';
import { policyField, validatePolicyValue } from '@/utils/wakePolicyContract';
import { useGroupContext } from './GroupLayout';

interface FieldDraft { mode: 'inherit' | 'value'; text: string }
type PolicyDraft = Record<string, FieldDraft>;

function display(value: unknown): string {
  if (Array.isArray(value)) return value.join('\n');
  if (value === null || value === undefined) return '';
  return String(value);
}

function draftFrom(explicit: Record<string, GroupPolicyValue>, fields: OverrideFieldDef[]): PolicyDraft {
  const result: PolicyDraft = {};
  for (const key of ['enabled', ...fields.map((field) => field.key)]) {
    const value = explicit[key];
    result[key] = value?.mode === 'value'
      ? { mode: 'value', text: display(value.value) }
      : { mode: 'inherit', text: '' };
  }
  return result;
}

function parseDraft(draft: PolicyDraft, fields: OverrideFieldDef[]): Record<string, GroupPolicyValue> {
  const result: Record<string, GroupPolicyValue> = {};
  if (draft.enabled.mode === 'value') {
    result.enabled = { mode: 'value', value: draft.enabled.text === 'true' };
  }
  for (const field of fields) {
    const item = draft[field.key];
    if (!item || item.mode === 'inherit') continue;
    let value: unknown;
    if (field.kind === 'number') {
      if (!item.text.trim()) throw new Error(`${field.label}需要数值`);
      value = Number(item.text);
    } else if (field.kind === 'bool') value = item.text === 'true';
    else if (field.kind === 'lines') value = item.text.split(/\r?\n/).map((word) => word.trim()).filter(Boolean);
    else value = item.text;
    const contract = policyField(field.key);
    if (!contract) throw new Error(`策略契约缺少 ${field.key}`);
    const issue = validatePolicyValue(contract, value, field.label);
    if (issue) throw new Error(issue);
    result[field.key] = { mode: 'value', value };
  }
  return result;
}

export function GroupPolicy() {
  const { adapterId, groupId, account, isRunning, historical, config, setConfig, reload } = useGroupContext();
  const fields = useMemo(() => GROUP_OVERRIDE_FIELDS.filter((field) => config.capabilities.policy_fields.includes(field.key)), [config.capabilities.policy_fields]);
  const explicit = useMemo(() => config.explicit.policy || {}, [config.explicit.policy]);
  const [draft, setDraft] = useState<PolicyDraft>(() => draftFrom(explicit, fields));
  const [baseline, setBaseline] = useState(() => JSON.stringify(draftFrom(explicit, fields)));
  const [saving, setSaving] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [probeText, setProbeText] = useState('');
  const [probeAtSelf, setProbeAtSelf] = useState(false);
  const [dryRunResult, setDryRunResult] = useState('');
  const [dryRunning, setDryRunning] = useState(false);
  const dirty = JSON.stringify(draft) !== baseline;
  useDirtyGuard(dirty);

  useEffect(() => {
    if (dirty) return;
    const fresh = draftFrom(explicit, fields);
    setDraft(fresh);
    setBaseline(JSON.stringify(fresh));
  }, [config.revision, config.account, dirty, explicit, fields]);

  const change = (key: string, partial: Partial<FieldDraft>) => {
    setDraft((previous) => ({ ...previous, [key]: { ...previous[key], ...partial } }));
  };
  const save = async () => {
    if (saving || historical) return;
    let values: Record<string, GroupPolicyValue>;
    try { values = parseDraft(draft, fields); }
    catch (caught) { setError(errorText(caught)); return; }
    setSaving(true);
    setError('');
    try {
      const saved = await groupApi.saveConfig(adapterId, groupId, {
        expected_self_id: account, expected_revision: config.revision,
        base_revision: config.base_revision, section: 'policy', values,
      }, isRunning);
      setConfig(saved);
      const fresh = draftFrom(saved.explicit.policy || {}, fields);
      setDraft(fresh);
      setBaseline(JSON.stringify(fresh));
      setConflict(false);
    } catch (caught) {
      setError(errorText(caught));
      if (caught instanceof ApiError && caught.status === 409) setConflict(true);
    } finally { setSaving(false); }
  };
  const retryApply = async () => {
    if (!isRunning || historical || applying) return;
    setApplying(true);
    try { setConfig(await groupApi.applyConfig(adapterId, groupId, account, config.saved_revision)); setError(''); }
    catch (caught) { setError(errorText(caught)); }
    finally { setApplying(false); }
  };
  const dryRun = async () => {
    if (!isRunning || dryRunning) return;
    let values: Record<string, GroupPolicyValue>;
    try { values = parseDraft(draft, fields); }
    catch (caught) { setError(errorText(caught)); return; }
    setDryRunning(true);
    try {
      const result = await groupApi.dryRunPolicy(adapterId, groupId, {
        expected_self_id: account, expected_revision: config.revision,
        base_revision: config.base_revision, values,
        scenario: { probe: { text: probeText, at_self: probeAtSelf } },
      });
      setDryRunResult(`基于当前草稿: ${result.response_enabled ? '允许响应' : '响应已关闭'}; 显式唤醒${result.explicit.triggered === null ? '无法判断' : result.explicit.triggered ? '触发' : '未触发'} (${result.explicit.reason})`);
      setError('');
    } catch (caught) { setError(errorText(caught)); }
    finally { setDryRunning(false); }
  };
  const serverLatest = async () => { await reload(); setConflict(false); };
  const discardDraft = async () => {
    const fresh = draftFrom(config.explicit.policy || {}, fields);
    setDraft(fresh);
    setBaseline(JSON.stringify(fresh));
    setConflict(false);
    await reload();
  };

  return (
    <div className="space-y-4">
      {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
      {conflict && <Card role="alert" className="space-y-2 border border-warning">
        <p>服务器配置已变化。草稿仍保留, 不会自动覆盖其他修改</p>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={serverLatest}>查看服务器最新值</Button>
          <Button size="sm" variant="subtle" onClick={() => {
            try { void navigator.clipboard.writeText(JSON.stringify(parseDraft(draft, fields), null, 2)); }
            catch (caught) { setError(errorText(caught)); }
          }}>复制我的草稿</Button>
          <Button size="sm" variant="subtle" onClick={discardDraft}>放弃草稿并重新加载</Button>
        </div>
      </Card>}
      <Card className="space-y-3">
        <h2 className="text-lg font-semibold">本群是否响应</h2>
        <p className="text-sm text-text-secondary">有效状态: {config.effective.policy.enabled ? '开启' : '关闭'} · {config.sources.policy.enabled?.source_label || '来源未知'}</p>
        <label className="block text-sm">本群设置
          <select className="glass-input mt-1 w-full max-w-sm" value={draft.enabled.mode === 'inherit' ? 'inherit' : draft.enabled.text}
            onChange={(event) => change('enabled', event.target.value === 'inherit' ? { mode: 'inherit', text: '' } : { mode: 'value', text: event.target.value })}
            disabled={historical || saving}>
            <option value="inherit">继承账号接入模式</option><option value="true">开启</option><option value="false">关闭</option>
          </select>
        </label>
        {!config.effective.policy.enabled && config.sources.policy.enabled?.source === 'platform' && (
          <p className="text-sm text-warning">平台总开关已关闭。本群设置不能越过平台总开关</p>
        )}
      </Card>
      <Card className="space-y-4">
        <h2 className="text-lg font-semibold">自动参与、显式触发与回复</h2>
        <p className="text-sm text-text-secondary">字段来自平台策略契约。继承和显式清空分别保存</p>
        <div className="grid gap-4 lg:grid-cols-2">
          {fields.map((field) => {
            const current = draft[field.key];
            const source = config.sources.policy[field.key]?.source_label || '平台默认';
            return <div key={field.key} className="space-y-2 rounded-lg border border-glass-border p-3">
              <label className="block text-sm font-medium" htmlFor={`policy-mode-${field.key}`}>{field.label}</label>
              <p className="text-xs text-text-secondary">当前有效值: {display(config.effective.policy[field.key]) || '空'} · {source}</p>
              <select id={`policy-mode-${field.key}`} className="glass-input w-full" value={current.mode}
                onChange={(event) => change(field.key, { mode: event.target.value as FieldDraft['mode'], text: event.target.value === 'value' ? current.text || display(config.effective.policy[field.key]) : '' })}
                disabled={historical || saving}>
                <option value="inherit">继承</option><option value="value">显式设置</option>
              </select>
              {current.mode === 'value' && (field.kind === 'lines' ? (
                <textarea className="glass-input w-full min-h-20" aria-label={field.label} value={current.text}
                  onChange={(event) => change(field.key, { text: event.target.value })} disabled={historical || saving} />
              ) : field.kind === 'bool' ? (
                <select className="glass-input w-full" aria-label={field.label} value={current.text}
                  onChange={(event) => change(field.key, { text: event.target.value })} disabled={historical || saving}>
                  <option value="true">开启</option><option value="false">关闭</option>
                </select>
              ) : field.kind === 'select' ? (
                <select className="glass-input w-full" aria-label={field.label} value={current.text}
                  onChange={(event) => change(field.key, { text: event.target.value })} disabled={historical || saving}>
                  <option value="">选择值</option>
                  {field.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                  {field.offValue !== undefined && <option value={String(field.offValue)}>{field.offLabel || '显式关闭'}</option>}
                </select>
              ) : (
                <input className="glass-input w-full" aria-label={field.label} type="number" min={field.min} max={field.max}
                  step={field.integer ? 1 : 'any'} value={current.text} placeholder={field.placeholder}
                  onChange={(event) => change(field.key, { text: event.target.value })} disabled={historical || saving} />
              ))}
              {field.offValue !== undefined && current.mode === 'value' && field.kind !== 'select' && (
                <Button size="sm" variant="subtle" onClick={() => change(field.key, { text: display(field.offValue) })} disabled={historical || saving}>
                  {field.offLabel || '显式关闭'}
                </Button>
              )}
            </div>;
          })}
        </div>
      </Card>
      <Card className="space-y-3">
        <h2 className="text-lg font-semibold">有效策略试算</h2>
        <p className="text-sm text-text-secondary">用未保存的草稿和示例消息判断唤醒, 不发送消息或调用模型</p>
        <label className="block text-sm">示例消息
          <textarea className="glass-input mt-1 w-full min-h-20" value={probeText} onChange={(event) => setProbeText(event.target.value)} />
        </label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={probeAtSelf} onChange={(event) => setProbeAtSelf(event.target.checked)} />示例消息 @机器人</label>
        <Button onClick={dryRun} disabled={!isRunning || dryRunning}>试算草稿</Button>
        {dryRunResult && <p className="text-sm text-text-secondary" role="status">{dryRunResult}</p>}
      </Card>
      {!historical && <Card className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-sm text-text-secondary">{dirty ? '有未保存修改' : config.apply_status === 'applied' ? '配置已生效' : '配置已保存, 尚未生效'}</span>
        <div className="flex gap-2">
          {config.apply_status !== 'applied' && <Button variant="subtle" onClick={retryApply} disabled={!isRunning || applying || dirty}>重试应用</Button>}
          <Button variant="primary" onClick={save} disabled={!dirty || saving}>保存并应用</Button>
        </div>
      </Card>}
    </div>
  );
}
