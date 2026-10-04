import { useEffect, useState } from 'react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupAction, type GroupMember } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useGroupContext } from './GroupLayout';

const memberActions = [
  ['set_group_card', '修改群昵称'], ['ban_group_member', '禁言或解除'],
  ['set_group_admin', '设置或取消管理员'], ['kick_group_member', '移出成员'],
] as const;

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '成员读取失败';
}

export function GroupMembers() {
  const { adapterId, groupId, account, historical, isRunning, config } = useGroupContext();
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<GroupMember[]>([]);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<{ member: GroupMember; action: string } | null>(null);
  const [types, setTypes] = useState<Awaited<ReturnType<typeof groupApi.actionTypes>>['items']>([]);
  const [duration, setDuration] = useState('1800');
  const [enable, setEnable] = useState('true');
  const [card, setCard] = useState('');
  const [reject, setReject] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<GroupAction | null>(null);
  const [operationId, setOperationId] = useState('');
  const [submitted, setSubmitted] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => { setDebounced(query); setPage(1); }, 300);
    return () => window.clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    if (!isRunning || historical || config.group.membership !== 'joined') return;
    let live = true;
    groupApi.actionTypes(adapterId, groupId, account).then((value) => {
      if (live) setTypes(value.items);
    }).catch((caught) => { if (live) setError(errorText(caught)); });
    return () => { live = false; };
  }, [adapterId, groupId, account, isRunning, historical, config.group.membership]);
  useEffect(() => {
    if (!isRunning || historical || config.group.membership !== 'joined') return;
    let live = true;
    setLoading(true);
    groupApi.members(adapterId, groupId, account, debounced, page).then((value) => {
      if (!live) return;
      setItems(value.items); setTotal(value.total_loaded); setTruncated(value.truncated); setError('');
    }).catch((caught) => { if (live) setError(errorText(caught)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [adapterId, groupId, account, isRunning, historical, config.group.membership, debounced, page, refreshKey]);
  const open = (member: GroupMember, action: string) => {
    if (result?.state === 'unknown' || result?.state === 'executing') return;
    setSelected({ member, action }); setResult(null); setCard(member.card || ''); setDuration('1800');
    setEnable('true'); setReject(false); setError(''); setOperationId(crypto.randomUUID()); setSubmitted(null);
  };
  const submit = async () => {
    if (!selected || busy) return;
    const params: Record<string, unknown> = { user_id: String(selected.member.user_id) };
    if (selected.action === 'set_group_card') params.card = card;
    if (selected.action === 'ban_group_member') params.duration = Number(duration);
    if (selected.action === 'set_group_admin') params.enable = enable === 'true';
    if (selected.action === 'kick_group_member') params.reject_add_request = reject;
    const payload = submitted || params;
    setSubmitted(payload);
    setBusy(true);
    try {
      const saved = await groupApi.submitAction(adapterId, groupId, {
        expected_self_id: account, action_id: operationId, action_type: selected.action, params: payload,
      });
      setResult(saved); setError('');
    } catch (caught) { setError(errorText(caught)); }
    finally { setBusy(false); }
  };
  const inspect = async () => {
    if (!operationId) return;
    try { setResult(await groupApi.action(adapterId, groupId, account, operationId)); setError(''); }
    catch (caught) { setError(errorText(caught)); }
  };
  const available = isRunning && !historical && config.group.membership === 'joined';

  return <div className="space-y-4">
    {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
    <Card className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">群成员</h2>
        <Button size="sm" variant="subtle" onClick={() => setRefreshKey((value) => value + 1)} disabled={!available || loading}>刷新成员</Button>
      </div>
      <p className="text-sm text-text-secondary">按需从平台读取, 只对本次取得的成员搜索与分页。角色和能力以平台实际返回为准</p>
      {!available && <p className="text-sm text-warning">此群当前无法读取成员, 历史目录仍可查看</p>}
      <input className="glass-input w-full max-w-md" value={query} placeholder="搜索账号昵称、群昵称或 QQ"
        disabled={!available} onChange={(event) => setQuery(event.target.value)} />
      {truncated && <p role="status" className="text-sm text-warning">平台成员列表超过 2048 条或达到安全上限, 本页仅展示已取得的集合</p>}
      {loading && <p role="status">读取成员中…</p>}
      {!loading && available && items.length === 0 && <p className="text-sm text-text-secondary">没有符合条件的成员</p>}
      <div className="space-y-2">
        {items.map((member) => <div key={String(member.user_id)} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-glass-border p-3 text-sm">
          <div><strong>{member.card || member.nickname || '未提供昵称'}</strong>
            <span className="ml-2 text-text-secondary">{member.user_id} · {member.role || '角色未知'}</span></div>
          {available && <div className="flex flex-wrap gap-1">
            {memberActions.map(([action, label]) => {
              const meta = types.find((item) => item.action_type === action);
              return <span key={action} className="flex flex-col items-center gap-1">
                <Button size="sm" variant="subtle" disabled={!meta?.available || result?.state === 'unknown' || result?.state === 'executing'}
                  onClick={() => open(member, action)}>{label}</Button>
                {!meta?.available && <span className="text-xs text-warning">{meta?.capability === 'unsupported' ? '平台不支持' : '当前不可用'}</span>}
              </span>;
            })}
          </div>}
        </div>)}
      </div>
      <div className="flex items-center gap-2 text-sm">
        <span>已加载匹配 {total} 人{truncated ? ', 总人数未知' : ''}</span>
        <Button size="sm" variant="subtle" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</Button>
        <span>第 {page} 页</span>
        <Button size="sm" variant="subtle" disabled={page * 25 >= total} onClick={() => setPage((value) => value + 1)}>下一页</Button>
      </div>
    </Card>
    {selected && <Card className="space-y-3">
      <h3 className="font-semibold">{memberActions.find(([action]) => action === selected.action)?.[1]}</h3>
      <p className="text-sm">目标群 {groupId} · 目标成员 {selected.member.user_id}</p>
      <p className="text-sm">审批: {types.find((item) => item.action_type === selected.action)?.approval_mode === 'approval_required' ? '提交后等待人工批准' : '提交后自动执行'}</p>
      {selected.action === 'ban_group_member' && <label className="block text-sm">时长, 秒 (0 为解除禁言)
        <input className="glass-input mt-1 w-full max-w-xs" type="number" min="0" max="2592000" value={duration} disabled={!!submitted} onChange={(event) => setDuration(event.target.value)} />
      </label>}
      {selected.action === 'set_group_admin' && <label className="block text-sm">操作
        <select className="glass-input mt-1 w-full max-w-xs" value={enable} disabled={!!submitted} onChange={(event) => setEnable(event.target.value)}>
          <option value="true">设置管理员</option><option value="false">取消管理员</option>
        </select>
      </label>}
      {selected.action === 'set_group_card' && <label className="block text-sm">新群昵称, 留空表示清除
        <input className="glass-input mt-1 w-full max-w-xs" value={card} maxLength={60} disabled={!!submitted} onChange={(event) => setCard(event.target.value)} />
      </label>}
      {selected.action === 'kick_group_member' && <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={reject} disabled={!!submitted} onChange={(event) => setReject(event.target.checked)} />拒绝后续加群请求
      </label>}
      <div className="flex flex-wrap gap-2"><Button onClick={submit} disabled={busy || !available || !!result}>{submitted ? '按原 ID 重试提交' : '提交动作'}</Button>
        <Button variant="subtle" onClick={inspect} disabled={!operationId}>按操作 ID 查询</Button>
        <Button variant="subtle" onClick={() => setSelected(null)} disabled={result?.state === 'unknown' || result?.state === 'executing'}>关闭</Button></div>
      <p className="break-all text-xs text-text-secondary">操作 ID: {operationId}</p>
      {result && <p role="status" className="text-sm">操作 {result.action_id}: {result.state}{result.state === 'unknown' ? ', 结果未知, 请先核实平台状态' : ''}</p>}
      {result?.state === 'unknown' && <Button variant="subtle" onClick={() => {
        setSelected(null); setResult(null); setSubmitted(null); setOperationId('');
      }}>我已核实平台状态, 可以创建新操作</Button>}
    </Card>}
  </div>;
}
