import { useEffect, useState } from 'react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupAction } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useGroupContext } from './GroupLayout';

const labels: Record<string, string> = {
  recall_message: '撤回消息', kick_group_member: '移出成员', ban_group_member: '禁言成员',
  set_group_whole_ban: '全员禁言', ban_anonymous: '禁言匿名成员', set_group_admin: '设置管理员',
  set_group_anonymous: '匿名聊天', set_group_card: '设置名片', set_group_name: '修改群名',
  set_group_special_title: '设置头衔', leave_group: '退出或解散群', handle_group_request: '处理加群请求',
};
const fields: Record<string, string> = {
  message_id: '消息 ID', user_id: '成员 QQ', reject_add_request: '拒绝再次加群',
  duration: '秒数', enable: '开启', flag: '请求 flag', card: '群名片', name: '群名',
  title: '头衔', dismiss: '解散群', sub_type: '请求类型', approve: '同意', reason: '理由',
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '操作失败';
}

export function GroupManage() {
  const { adapterId, groupId, account, isRunning, historical, config } = useGroupContext();
  const [types, setTypes] = useState<Awaited<ReturnType<typeof groupApi.actionTypes>>['items']>([]);
  const [type, setType] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<GroupAction | null>(null);
  const [operationId, setOperationId] = useState('');
  const [submitted, setSubmitted] = useState<{ action_type: string; params: Record<string, unknown> } | null>(null);
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);
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
  const ready = isRunning && !historical && (config.group.membership === 'joined' || type === 'handle_group_request');

  return <div className="space-y-4">
    {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
    {info && <Card className="space-y-2 text-sm"><h2 className="text-lg font-semibold">平台群信息</h2>
      <p>群名: {String(info.group_name || config.group.group_name || groupId)}</p>
      <p>成员: {String(info.member_count ?? '未知')} / {String(info.max_member_count ?? '未知')}</p>
    </Card>}
    <Card className="space-y-3">
      <h2 className="text-lg font-semibold">群管理动作</h2>
      <p className="text-sm text-text-secondary">目标固定为机器人 {account} 的群 {groupId}。机器人能力由平台执行时确认</p>
      {!ready && <p className="text-sm text-warning">当前账号、连接或成员关系不满足管理条件</p>}
      <label className="block text-sm">动作
        <select className="glass-input mt-1 w-full max-w-md" value={type} disabled={!ready || busy || !!operationId}
          onChange={(event) => { setType(event.target.value); setValues({}); setResult(null); }}>
          {types.map((item) => <option key={item.action_type} value={item.action_type}>{labels[item.action_type] || item.action_type}</option>)}
        </select>
      </label>
      {selected && <>
        <p className="text-sm">风险: {selected.risk === 'high' ? '高影响' : '普通'} · 审批: {selected.approval_mode === 'approval_required' ? '需要人工批准' : '自动执行'} · 能力: {selected.capability === 'unknown' ? '待平台确认' : selected.capability}</p>
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
          {operationId && <Button variant="subtle" onClick={() => { setOperationId(''); setSubmitted(null); setResult(null); setValues({}); setError(''); }} disabled={busy || result?.state === 'unknown' || result?.state === 'executing'}>开始新操作</Button>}
        </div>
      </>}
      {result && <div className="rounded-lg border border-glass-border p-3 text-sm" aria-live="polite">
        <p>操作 ID: <code>{result.action_id}</code></p>
        <p>状态: {result.state} · {result.result?.reason || '等待后续处理'}</p>
        <Button size="sm" variant="subtle" onClick={inspect}>按操作 ID 查询状态</Button>
      </div>}
      {operationId && !result && <div className="rounded-lg border border-warning p-3 text-sm">
        <p>操作 ID: <code>{operationId}</code></p>
        <Button size="sm" variant="subtle" onClick={inspect}>按原 ID 查询状态</Button>
      </div>}
    </Card>
  </div>;
}
