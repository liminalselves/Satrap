import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, Clock3, FlaskConical, Play, Save, ShieldCheck, SlidersHorizontal } from 'lucide-react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupPolicyValue } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { Textarea } from '@/components/kit/Field';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';
import { GROUP_OVERRIDE_FIELDS, type OverrideFieldDef } from '@/utils/wakeOverrides';
import { policyField, validatePolicyValue } from '@/utils/wakePolicyContract';
import { useGroupContext } from './GroupLayout';
import { FieldBlock, Note, SectionHead } from './shared';
import { Select as KitSelect } from '@/components/kit/Select';

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
    <div className="space-y-4 animate-fade">
      {error && <Note tone="red" title="无法保存">{error}</Note>}
      {conflict && <Note tone="orange" title="服务器配置已变化, 草稿仍保留" icon={<AlertTriangle className="h-4 w-4" />}
        action={<div className="flex flex-wrap gap-1.5">
          <Button size="sm" onClick={serverLatest}>查看服务器最新值</Button>
          <Button size="sm" variant="ghost" onClick={() => {
            try { void navigator.clipboard.writeText(JSON.stringify(parseDraft(draft, fields), null, 2)); }
            catch (caught) { setError(errorText(caught)); }
          }}>复制我的草稿</Button>
          <Button size="sm" variant="ghost" onClick={discardDraft}>放弃草稿并重新加载</Button>
        </div>}>
        不会自动覆盖其他管理员的修改。
      </Note>}

      <Card padding="lg">
        <SectionHead tint="pink" title="本群是否响应" icon={<ShieldCheck className="h-4 w-4" />}
          description={`有效状态: ${config.effective.policy.enabled ? '开启' : '关闭'} · ${config.sources.policy.enabled?.source_label || '来源未知'}`} />
        <FieldBlock label="本群设置" hint="继承账号接入模式时, 由平台总开关与群默认决定" className="max-w-sm">
          <KitSelect className="field w-full" value={draft.enabled.mode === 'inherit' ? 'inherit' : draft.enabled.text}
            onChange={(next) => change('enabled', next === 'inherit' ? { mode: 'inherit', text: '' } : { mode: 'value', text: next })}
            disabled={historical || saving}>
            <option value="inherit">继承账号接入模式</option><option value="true">开启</option><option value="false">关闭</option>
          </KitSelect>
        </FieldBlock>
        {!config.effective.policy.enabled && config.sources.policy.enabled?.source === 'platform' && (
          <Note tone="orange" className="mt-3">平台总开关已关闭。本群设置不能越过平台总开关。</Note>
        )}
      </Card>

      <Card padding="lg">
        <SectionHead tint="pink" title="自动参与、显式触发与回复" icon={<SlidersHorizontal className="h-4 w-4" />}
          description="字段来自平台策略契约。继承和显式清空分别保存" />
        <div className="grid gap-3 lg:grid-cols-2">
          {fields.map((field) => {
            const current = draft[field.key];
            const source = config.sources.policy[field.key]?.source_label || '平台默认';
            return <div key={field.key} className="space-y-2 rounded-xl border border-line bg-glass p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[12.5px] font-medium text-fg">{field.label}</span>
                {current.mode === 'value' && <Badge tone="pink">本群覆盖</Badge>}
                <span className="ml-auto text-[10.5px] text-fg-4">{source}</span>
              </div>
              <p className="text-[11px] text-fg-3">当前有效值: {display(config.effective.policy[field.key]) || '空'}</p>
              <div className="grid gap-2 sm:grid-cols-[108px_1fr]">
                <KitSelect className="field !h-8 !text-[12px]" aria-label={`${field.label}状态`} value={current.mode}
                  onChange={(next) => change(field.key, { mode: next as FieldDraft['mode'], text: next === 'value' ? current.text || display(config.effective.policy[field.key]) : '' })}
                  disabled={historical || saving}>
                  <option value="inherit">继承</option><option value="value">显式设置</option>
                </KitSelect>
                {current.mode === 'value' && (field.kind === 'lines' ? (
                  <Textarea rows={2} className="!text-[12px] font-mono" aria-label={field.label} value={current.text}
                    placeholder={field.placeholder} onChange={(event) => change(field.key, { text: event.target.value })} disabled={historical || saving} />
                ) : field.kind === 'bool' ? (
                  <KitSelect className="field !h-8 !text-[12px]" aria-label={field.label} value={current.text}
                    onChange={(value) => change(field.key, { text: value })} disabled={historical || saving}>
                    <option value="true">开启</option><option value="false">关闭</option>
                  </KitSelect>
                ) : field.kind === 'select' ? (
                  <KitSelect className="field !h-8 !text-[12px]" aria-label={field.label} value={current.text}
                    onChange={(value) => change(field.key, { text: value })} disabled={historical || saving}>
                    <option value="">选择值</option>
                    {field.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    {field.offValue !== undefined && <option value={String(field.offValue)}>{field.offLabel || '显式关闭'}</option>}
                  </KitSelect>
                ) : (
                  <input className="field !h-8 !text-[12px]" aria-label={field.label} type="number" min={field.min} max={field.max}
                    step={field.integer ? 1 : 'any'} value={current.text} placeholder={field.placeholder}
                    onChange={(event) => change(field.key, { text: event.target.value })} disabled={historical || saving} />
                ))}
              </div>
              {field.offValue !== undefined && current.mode === 'value' && field.kind !== 'select' && (
                <button type="button" className="text-[11px] text-pink hover:underline" onClick={() => change(field.key, { text: display(field.offValue) })} disabled={historical || saving}>
                  {field.offLabel || '显式关闭'}
                </button>
              )}
            </div>;
          })}
        </div>
      </Card>

      <Card padding="lg">
        <SectionHead tint="pink" title="有效策略试算" icon={<FlaskConical className="h-4 w-4" />}
          description="用未保存的草稿和示例消息判断唤醒, 不发送消息或调用模型"
          actions={<Button size="sm" variant="tinted" tint="pink" icon={<Play className="h-3.5 w-3.5" />} loading={dryRunning} onClick={dryRun} disabled={!isRunning}>试算草稿</Button>} />
        <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
          <FieldBlock label="示例消息">
            <textarea className="field min-h-20 w-full" value={probeText} placeholder="例如 小萨, 这个订单什么时候发货?"
              onChange={(event) => setProbeText(event.target.value)} disabled={historical} />
          </FieldBlock>
          <div className="flex items-end pb-2">
            <label className="flex cursor-pointer items-center gap-2 text-[12.5px] text-fg-3">
              <input type="checkbox" className="accent-[rgb(var(--rgb-pink))]" checked={probeAtSelf} disabled={historical}
                onChange={(event) => setProbeAtSelf(event.target.checked)} />示例消息 @机器人
            </label>
          </div>
        </div>
        {dryRunResult && <div className="mt-3 rounded-xl border border-line bg-glass p-3 text-[12px] animate-pop" role="status">
          <p className="text-fg-2">{dryRunResult}</p>
        </div>}
      </Card>

      {!historical && <Card padding="md" className="flex flex-wrap items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-[12px] text-fg-3">
          {dirty ? <><Clock3 className="h-3.5 w-3.5 text-orange" />有未保存修改</>
            : config.apply_status === 'applied' ? <><CheckCircle2 className="h-3.5 w-3.5 text-green" />配置已生效</>
              : <><AlertTriangle className="h-3.5 w-3.5 text-orange" />配置已保存, 尚未生效</>}
        </span>
        <div className="flex gap-2">
          {config.apply_status !== 'applied' && <Button size="sm" variant="ghost" loading={applying} onClick={retryApply} disabled={!isRunning || dirty}>重试应用</Button>}
          <Button variant="primary" icon={<Save className="h-4 w-4" />} loading={saving} onClick={save} disabled={!dirty || saving}>保存并应用</Button>
        </div>
      </Card>}
    </div>
  );
}
