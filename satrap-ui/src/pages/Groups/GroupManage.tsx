import { createUuid } from '@/utils/uuid';
import { groupActionStateLabels as actionStateLabels } from './groupActionLabels';
import { Link } from 'react-router-dom';
import { approvalLink } from '@/api/platformActions';
import { useEffect, useState } from 'react';
import { ApiError } from '@/api/client';
import { Bot, Play, ShieldCheck, Wrench } from 'lucide-react';
import { groupApi, type GroupAction } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { useGroupContext } from './GroupLayout';
import { groupActionLabels } from './groupActionLabels';
import { errorText } from '@/utils/errorText';
import { FieldBlock, Note, SectionHead, capabilityLabels, approvalLabels } from './shared';
import { Select as KitSelect } from '@/components/kit/Select';

const fields: Record<string, string> = {
  message_id: '消息 ID', user_id: '成员 QQ', reject_add_request: '拒绝再次加群',
  duration: '秒数', enable: '开启', flag: '请求 flag', card: '群昵称', name: '群名',
  title: '头衔', dismiss: '解散群', sub_type: '请求类型', approve: '同意', reason: '理由',
};

type ApprovalMode = 'inherit' | 'approval_required' | 'auto_execute';

function approvalFrom(explicit: Record<string, { mode: string; value?: unknown }> | undefined): Record<string, ApprovalMode> {
  return Object.fromEntries(Object.entries(explicit || {}).map(([action, value]) => [action,
    value.mode === 'value' && (value.value === 'approval_required' || value.value === 'auto_execute')
      ? value.value : 'inherit']));
}

export function GroupManage() {
  const { adapterId, groupId, account, isRunning, historical, config, setConfig, reload } = useGroupContext();
  const [types, setTypes] = useState<Awaited<ReturnType<typeof groupApi.actionTypes>>['items']>([]);
  const [type, setType] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<GroupAction | null>(null);
  const [operationId, setOperationId] = useState('');
  const [submitted, setSubmitted] = useState<{ action_type: string; params: Record<string, unknown> } | null>(null);
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);
  const [approvalDraft, setApprovalDraft] = useState(() => approvalFrom(config.explicit.approval));
  const [approvalBaseline, setApprovalBaseline] = useState(() => JSON.stringify(approvalFrom(config.explicit.approval)));
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [approvalError, setApprovalError] = useState('');
  const [approvalConflict, setApprovalConflict] = useState(false);
  const approvalDirty = JSON.stringify(approvalDraft) !== approvalBaseline;
  useDirtyGuard(approvalDirty);
  useEffect(() => {
    if (approvalDirty) return;
    const fresh = approvalFrom(config.explicit.approval);
    setApprovalDraft(fresh);
    setApprovalBaseline(JSON.stringify(fresh));
  }, [config.revision, config.account, config.explicit.approval, approvalDirty]);
  useEffect(() => {
    if (!isRunning || !account) return;
    let live = true;
    groupApi.actionTypes(adapterId, groupId, account).then((data) => {
      if (!live) return;
      setTypes(data.items);
      setType((previous) => previous || data.items[0]?.action_type || '');
      setError('');
    }).catch((caught) => { if (live) setError(errorText(caught)); });
    return () => { live = false; };
  }, [adapterId, groupId, account, isRunning, config.revision]);
  useEffect(() => {
    if (!isRunning || historical || config.group.membership !== 'joined') return;
    let live = true;
    groupApi.info(adapterId, groupId, account).then((value) => { if (live) setInfo(value); })
      .catch(() => { if (live) setInfo(null); });
    return () => { live = false; };
  }, [adapterId, groupId, account, isRunning, historical, config.group.membership]);
  const selected = types.find((item) => item.action_type === type);
  const submit = async () => {
    if (!selected || busy || historical || !isRunning) return;
    const params: Record<string, unknown> = {};
    for (const [key, kind] of Object.entries(selected.schema)) {
      const raw = values[key] || '';
      if (!raw && !['card', 'title', 'reason'].includes(key)) continue;
      params[key] = kind === 'bool' ? raw === 'true' : kind === 'duration' ? Number(raw) : raw;
    }
    const id = operationId || createUuid();
    const payload = submitted || { action_type: type, params };
    setOperationId(id);
    setSubmitted(payload);
    setBusy(true);
    setError('');
    try {
      const saved = await groupApi.submitAction(adapterId, groupId, {
        expected_self_id: account, action_id: id, ...payload,
      });
      setResult(saved);
    } catch (caught) { setError(errorText(caught)); }
    finally { setBusy(false); }
  };
  const inspect = async () => {
    if (!operationId) return;
    try { setResult(await groupApi.action(adapterId, groupId, account, operationId)); setError(''); }
    catch (caught) { setError(errorText(caught)); }
  };
  const resetOperation = () => {
    setOperationId(''); setSubmitted(null); setResult(null); setValues({}); setError('');
  };
  const ready = isRunning && !historical && (config.group.membership === 'joined' || type === 'handle_group_request');
  const saveApproval = async () => {
    if (historical || approvalBusy || !approvalDirty) return;
    const values = Object.fromEntries(Object.entries(approvalDraft)
      .filter(([, mode]) => mode !== 'inherit')
      .map(([action, mode]) => [action, { mode: 'value' as const, value: mode }]));
    setApprovalBusy(true); setApprovalError('');
    try {
      const saved = await groupApi.saveConfig(adapterId, groupId, {
        expected_self_id: account, expected_revision: config.revision,
        base_revision: config.base_revision, section: 'approval', values,
      }, isRunning);
      setConfig(saved);
      const fresh = approvalFrom(saved.explicit.approval);
      setApprovalDraft(fresh); setApprovalBaseline(JSON.stringify(fresh)); setApprovalConflict(false);
    } catch (caught) {
      setApprovalError(errorText(caught));
      if (caught instanceof ApiError && caught.status === 409) setApprovalConflict(true);
    } finally { setApprovalBusy(false); }
  };

  return <div className="space-y-4 animate-fade">
    {error && <Note tone="red" title="操作失败">{error}</Note>}
    {approvalError && <Note tone="red" title="无法保存审批设置">{approvalError}</Note>}
    {approvalConflict && <Note tone="orange" title="审批设置已变化, 草稿仍保留" action={<div className="flex flex-wrap gap-1.5">
      <Button size="sm" onClick={async () => { await reload(); setApprovalConflict(false); }}>查看服务器最新值</Button>
      <Button size="sm" variant="ghost" onClick={() => { void navigator.clipboard?.writeText(JSON.stringify(approvalDraft, null, 2)); }}>复制我的草稿</Button>
      <Button size="sm" variant="ghost" onClick={async () => {
        const fresh = approvalFrom(config.explicit.approval);
        setApprovalDraft(fresh); setApprovalBaseline(JSON.stringify(fresh)); setApprovalConflict(false);
        await reload();
      }}>放弃草稿并重新加载</Button>
    </div>}>
      不会自动覆盖其他管理员的修改。
    </Note>}

    <Card padding="lg">
      <SectionHead tint="pink" title="平台群信息" icon={<Bot className="h-4 w-4" />} description="以平台实际返回的群资料为准" />
      <div className="grid gap-2 text-[12.5px] sm:grid-cols-3">
        <div className="rounded-xl border border-line bg-glass px-3 py-2"><div className="text-[11px] text-fg-4">群名</div><div className="font-medium text-fg">{String(info?.group_name || config.group.group_name || groupId)}</div></div>
        <div className="rounded-xl border border-line bg-glass px-3 py-2"><div className="text-[11px] text-fg-4">成员</div><div className="num font-medium text-fg">{String(info?.member_count ?? config.group.member_count ?? '未知')} / {String(info?.max_member_count ?? config.group.max_member_count ?? '未知')}</div></div>
        <div className="rounded-xl border border-line bg-glass px-3 py-2"><div className="text-[11px] text-fg-4">成员关系</div><div className="font-medium text-fg">{config.group.membership === 'joined' ? '已加入' : config.group.membership === 'left' ? '已离开' : config.group.membership === 'config_only' ? '仅保留配置' : '待确认'}</div></div>
      </div>
    </Card>

    <Card padding="lg">
      <SectionHead tint="pink" title="本群审批设置" icon={<ShieldCheck className="h-4 w-4" />}
        description="只影响以后提交的群目标动作; 已有待审批请求不会因切换为自动执行而运行"
        actions={<Button size="sm" variant="tinted" tint="pink" loading={approvalBusy} onClick={saveApproval} disabled={historical || !approvalDirty}>保存本群审批设置</Button>} />
      <div className="grid gap-2 sm:grid-cols-2">
        {config.capabilities.approval_actions.map((action) => {
          const metadata = types.find((item) => item.action_type === action);
          const effective = config.effective.approval[action];
          return <label key={action} className="block rounded-xl border border-line bg-glass p-3">
            <span className="text-[12.5px] font-medium text-fg">{groupActionLabels[action] || action}</span>
            <Badge tone={metadata?.risk === 'high' ? 'red' : 'neutral'} className="ml-1.5">{metadata ? metadata.risk === 'high' ? '高影响' : '普通' : '风险待确认'}</Badge>
            <span className="ml-1.5 text-[10.5px] text-fg-4">当前{approvalLabels[effective] || (effective === 'approval_required' ? '需审批' : '自动执行')} · 来源 {config.sources.approval[action] || '默认'}</span>
            <KitSelect className="field mt-2 !h-8 !text-[12px]" value={approvalDraft[action] || 'inherit'}
              disabled={historical || approvalBusy}
              onChange={(value) => setApprovalDraft((old) => ({ ...old, [action]: value as ApprovalMode }))}>
              <option value="inherit">继承平台默认</option><option value="approval_required">需审批</option><option value="auto_execute">自动执行</option>
            </KitSelect>
          </label>;
        })}
      </div>
      {config.capabilities.approval_actions.length === 0 && <p className="text-[12px] text-fg-3">当前平台未返回可配置的审批动作。</p>}
    </Card>

    <Card padding="lg">
      <SectionHead tint="pink" title="群管理动作" icon={<Wrench className="h-4 w-4" />}
        description={`目标固定为机器人 ${account} 的群 ${groupId}; 能力由平台执行时确认`} />
      {!ready && <Note tone="orange" className="mb-3">当前账号、连接或成员关系不满足管理条件。</Note>}
      <FieldBlock label="动作" className="max-w-md">
        <KitSelect className="field w-full" value={type} disabled={!ready || busy || !!operationId}
          onChange={(value) => { setType(value); setValues({}); setResult(null); }}>
          {types.map((item) => <option key={item.action_type} value={item.action_type}>{groupActionLabels[item.action_type] || item.action_type}</option>)}
        </KitSelect>
      </FieldBlock>
      {selected && <>
        <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[11.5px]">
          <Badge tone={selected.risk === 'high' ? 'red' : 'neutral'}>风险: {selected.risk === 'high' ? '高影响' : '普通'}</Badge>
          <Badge tone={selected.approval_mode === 'approval_required' ? 'orange' : 'green'}>审批: {approvalLabels[selected.approval_mode] || selected.approval_mode}</Badge>
          <Badge tone={selected.available ? 'green' : 'orange'}>能力: {capabilityLabels[selected.capability] || selected.capability}</Badge>
        </div>
        {!selected.available && <Note tone="orange" className="mt-2">此动作当前不可用: {capabilityLabels[selected.capability] || '机器人尚未确认加入目标群'}。</Note>}
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {Object.entries(selected.schema).map(([key, kind]) => <FieldBlock key={key} label={fields[key] || key}
            hint={kind === 'duration' ? '单位: 秒; 0 表示解除' : undefined}>
            {kind === 'bool' ? (
              <KitSelect className="field w-full" value={values[key] || ''} disabled={!ready || busy || !!operationId}
                onChange={(value) => setValues((previous) => ({ ...previous, [key]: value }))}>
                <option value="">未指定</option><option value="true">是</option><option value="false">否</option>
              </KitSelect>
            ) : (
              <input className="field w-full" value={values[key] || ''} disabled={!ready || busy || !!operationId}
                type={kind === 'duration' ? 'number' : 'text'} min={kind === 'duration' ? 0 : undefined}
                onChange={(event) => setValues((previous) => ({ ...previous, [key]: event.target.value }))} />
            )}
          </FieldBlock>)}
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button variant="primary" icon={<Play className="h-4 w-4" />} loading={busy}
            disabled={!ready || busy || !selected.available || !!result} onClick={submit}>
            {operationId ? '按原 ID 重试提交' : '提交管理动作'}
          </Button>
          {operationId && <Button variant="ghost" onClick={inspect} disabled={busy}>按原 ID 查询状态</Button>}
          {operationId && <Button variant="ghost" disabled={busy || result?.state === 'unknown' || result?.state === 'executing'} onClick={resetOperation}>开始新操作</Button>}
        </div>
        {result && (
          <div className="mt-3 rounded-xl border border-line bg-glass p-3 text-[12.5px] animate-pop" role="status">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={result.state === 'succeeded' ? 'green' : result.state === 'failed' || result.state === 'rejected' ? 'red' : 'orange'} dot>{actionStateLabels[result.state] || result.state}</Badge>
              <span className="num font-mono text-[10.5px] text-fg-4">{result.action_id}</span>
            </div>
            <p className="mt-1 text-fg-2">状态: {actionStateLabels[result.state] || result.state} · {result.result?.reason || '等待后续处理'}</p>
            {result.state === 'pending' && (
              <Link className="mt-1 inline-flex text-[12px] text-accent hover:underline"
                to={approvalLink({ platform: adapterId, account, source: 'group', target: groupId, action: result.action_id })}>
                已提交, 前往审批中心
              </Link>
            )}
            {result.state === 'unknown' && <p className="mt-1 text-orange">结果未知, 请先核实平台状态。</p>}
            {result.state === 'unknown' && <Button size="sm" variant="ghost" className="mt-1" onClick={resetOperation}>我已核实平台状态, 可以创建新操作</Button>}
          </div>
        )}
        {operationId && !result && (
          <div className="mt-3 rounded-xl border border-[rgb(var(--rgb-orange)/0.3)] bg-[rgb(var(--rgb-orange)/0.08)] p-3 text-[12px] text-fg-2" role="status">
            <p>操作 ID: <span className="num font-mono">{operationId}</span></p>
            <Button size="sm" variant="ghost" className="mt-1" onClick={inspect}>按操作 ID 查询状态</Button>
          </div>
        )}
      </>}
    </Card>
  </div>;
}
