import { useEffect, useState } from 'react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupAction } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { useGroupContext } from './GroupLayout';
import { groupActionLabels } from './groupActionLabels';

const fields: Record<string, string> = {
  message_id: '消息 ID', user_id: '成员 QQ', reject_add_request: '拒绝再次加群',
  duration: '秒数', enable: '开启', flag: '请求 flag', card: '群名片', name: '群名',
  title: '头衔', dismiss: '解散群', sub_type: '请求类型', approve: '同意', reason: '理由',
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '操作失败';
}

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
    const id = operationId || crypto.randomUUID();
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

  return <div className="space-y-4">
    {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
    {approvalError && <Card role="alert" className="border border-error text-error">{approvalError}</Card>}
    {approvalConflict && <Card role="alert" className="space-y-2 border border-warning">
      <p>审批设置已变化。草稿仍保留, 不会自动覆盖其他修改</p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={async () => { await reload(); setApprovalConflict(false); }}>查看服务器最新值</Button>
        <Button size="sm" variant="subtle" onClick={() => navigator.clipboard.writeText(JSON.stringify(approvalDraft, null, 2))}>复制我的草稿</Button>
        <Button size="sm" variant="subtle" onClick={async () => {
          const fresh = approvalFrom(config.explicit.approval);
          setApprovalDraft(fresh); setApprovalBaseline(JSON.stringify(fresh)); setApprovalConflict(false);
          await reload();
        }}>放弃草稿并重新加载</Button>
      </div>
    </Card>}
    {info && <Card className="space-y-2 text-sm"><h2 className="text-lg font-semibold">平台群信息</h2>
      <p>群名: {String(info.group_name || config.group.group_name || groupId)}</p>
      <p>成员: {String(info.member_count ?? '未知')} / {String(info.max_member_count ?? '未知')}</p>
    </Card>}
    <Card className="space-y-3">
      <h2 className="text-lg font-semibold">本群审批设置</h2>
      <p className="text-sm text-text-secondary">只影响以后提交的群目标动作。已有待审批请求不会因切换为自动执行而运行</p>
      <div className="grid gap-3 md:grid-cols-2">
        {config.capabilities.approval_actions.map((action) => {
          const metadata = types.find((item) => item.action_type === action);
          const effective = config.effective.approval[action];
          return <label key={action} className="block rounded-lg border border-glass-border p-3 text-sm">
            <span className="font-medium">{groupActionLabels[action] || action}</span>
            <span className="ml-2 text-text-secondary">{metadata?.risk === 'high' ? '高影响' : '普通'} · 当前{effective === 'approval_required' ? '需审批' : '自动执行'} · {config.sources.approval[action] || '默认'}</span>
            <select className="glass-input mt-2 w-full" value={approvalDraft[action] || 'inherit'}
              disabled={historical || approvalBusy}
              onChange={(event) => setApprovalDraft((old) => ({ ...old, [action]: event.target.value as ApprovalMode }))}>
              <option value="inherit">继承平台默认</option><option value="approval_required">需审批</option><option value="auto_execute">自动执行</option>
            </select>
          </label>;
        })}
      </div>
      <Button onClick={saveApproval} disabled={historical || approvalBusy || !approvalDirty}>保存本群审批设置</Button>
    </Card>
    <Card className="space-y-3">
      <h2 className="text-lg font-semibold">群管理动作</h2>
      <p className="text-sm text-text-secondary">目标固定为机器人 {account} 的群 {groupId}。机器人能力由平台执行时确认</p>
      {!ready && <p className="text-sm text-warning">当前账号、连接或成员关系不满足管理条件</p>}
      <label className="block text-sm">动作
        <select className="glass-input mt-1 w-full max-w-md" value={type} disabled={!ready || busy || !!operationId}
          onChange={(event) => { setType(event.target.value); setValues({}); setResult(null); }}>
          {types.map((item) => <option key={item.action_type} value={item.action_type}>{groupActionLabels[item.action_type] || item.action_type}</option>)}
        </select>
      </label>
      {selected && <>
        <p className="text-sm">风险: {selected.risk === 'high' ? '高影响' : '普通'} · 审批: {selected.approval_mode === 'approval_required' ? '需要人工批准' : '自动执行'} · 能力: {selected.capability === 'unknown' ? '待平台确认' : selected.capability}</p>
        {!selected.available && <p className="text-sm text-warning">此动作当前不可用: {selected.capability === 'unsupported' ? '平台已确认不支持' : selected.capability === 'unavailable' ? '平台连接不可用' : '机器人尚未确认加入目标群'}</p>}
        <div className="grid gap-3 md:grid-cols-2">
          {Object.entries(selected.schema).map(([key, kind]) => <label key={key} className="block text-sm">{fields[key] || key}
            {kind === 'bool' ? <select className="glass-input mt-1 w-full" value={values[key] || ''} disabled={!ready || busy || !!operationId}
              onChange={(event) => setValues((previous) => ({ ...previous, [key]: event.target.value }))}>
              <option value="">未指定</option><option value="true">是</option><option value="false">否</option>
            </select> : <input className="glass-input mt-1 w-full" value={values[key] || ''} disabled={!ready || busy || !!operationId}
              type={kind === 'duration' ? 'number' : 'text'}
              onChange={(event) => setValues((previous) => ({ ...previous, [key]: event.target.value }))} />}
          </label>)}
        </div>
        <div className="flex gap-2"><Button onClick={submit} disabled={!ready || busy || !selected.available || !!result}>{busy ? '提交中…' : operationId ? '按原 ID 重试提交' : '提交管理动作'}</Button>
          {operationId && <Button variant="subtle" onClick={resetOperation} disabled={busy || result?.state === 'unknown' || result?.state === 'executing'}>开始新操作</Button>}
        </div>
      </>}
      {result && <div className="rounded-lg border border-glass-border p-3 text-sm" aria-live="polite">
        <p>操作 ID: <code>{result.action_id}</code></p>
        <p>状态: {result.state} · {result.result?.reason || '等待后续处理'}</p>
        {result.state === 'unknown' && <p className="text-warning">结果未知, 请先核实平台状态</p>}
        <Button size="sm" variant="subtle" onClick={inspect}>按操作 ID 查询状态</Button>
        {result.state === 'unknown' && <Button size="sm" variant="subtle" onClick={resetOperation}>我已核实平台状态, 可以创建新操作</Button>}
      </div>}
      {operationId && !result && <div className="rounded-lg border border-warning p-3 text-sm">
        <p>操作 ID: <code>{operationId}</code></p>
        <Button size="sm" variant="subtle" onClick={inspect}>按原 ID 查询状态</Button>
      </div>}
    </Card>
  </div>;
}
