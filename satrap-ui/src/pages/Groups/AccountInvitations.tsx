import { useConfirm } from '@/hooks/useConfirm';
import { useCallback, useEffect, useRef, useState } from 'react';
import { accountRequestsApi, type GroupInvitation, type InvitationAction } from '@/api/accountRequests';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';

const states: Record<string, string> = { pending: '待人工批准', executing: '执行中', succeeded: '平台已执行',
  failed: '执行失败', unknown: '结果未知, 不要重复执行', rejected: '审批已拒绝', expired: '审批已过期' };

export function AccountInvitations({ adapterId, account, open, onClose }: {
  adapterId: string; account: string; open: boolean; onClose: () => void;
}) {
  const confirm = useConfirm(`${adapterId}:${account}:${open}`);
  const [view, setView] = useState('active');
  const [cursor, setCursor] = useState<string>();
  const [page, setPage] = useState(1);
  const [invitations, setInvitations] = useState<GroupInvitation[]>([]);
  const [actions, setActions] = useState<InvitationAction[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [unavailable, setUnavailable] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState('');
  const [refresh, setRefresh] = useState(0);
  const epoch = useRef(0);
  const operationIds = useRef(new Map<string, string>());

  const load = useCallback(async (signal?: AbortSignal) => {
    const request = ++epoch.current;
    setLoading(true);
    setError('');
    try {
      const [list, records] = await Promise.all([
        accountRequestsApi.list(adapterId, account, view, cursor, signal),
        accountRequestsApi.actions(adapterId, account, page, signal),
      ]);
      if (request !== epoch.current || signal?.aborted) return;
      setInvitations(list.items); setNextCursor(list.next_cursor); setUnavailable(list.unavailable_count);
      setActions(records.items); setTotal(records.total);
    } catch (caught) {
      if (request === epoch.current && !signal?.aborted) setError(errorText(caught));
    } finally { if (request === epoch.current && !signal?.aborted) setLoading(false); }
  }, [adapterId, account, view, cursor, page]);

  useEffect(() => {
    setView('active'); setCursor(undefined); setPage(1); setResult('');
    operationIds.current.clear();
  }, [adapterId, account]);

  useEffect(() => {
    const requests = epoch;
    requests.current++;
    setInvitations([]); setActions([]); setNextCursor(null); setTotal(0); setUnavailable(0);
    if (!open) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => { requests.current++; controller.abort(); };
  }, [open, load, refresh]);

  const operate = async (key: string, task: (actionId: string) => Promise<InvitationAction>) => {
    if (busy) return;
    const generation = epoch.current;
    setBusy(true); setError(''); setResult('');
    let actionId = operationIds.current.get(key);
    if (!actionId) { actionId = crypto.randomUUID(); operationIds.current.set(key, actionId); }
    try {
      const record = await task(actionId);
      if (generation !== epoch.current) return;
      setResult(`${states[record.state] || record.state} · ${record.action_id}`);
      setCursor(undefined); setPage(1); setRefresh((value) => value + 1);   // 处理后原游标可能移入归档, 从首批重新读取
    } catch (caught) {
      if (generation === epoch.current) setError(errorText(caught));
    } finally { setBusy(false); }
  };

  const handle = async (invitation: GroupInvitation, approve: boolean) => {
    if (!(await confirm(`确认${approve ? '接受' : '拒绝'}群 ${invitation.group_id} 对机器人的入群邀请?`))) return;
    void operate(`${invitation.request_id}:${invitation.revision}:${approve}`, (id) =>
      accountRequestsApi.handle(adapterId, account, invitation, approve, id));
  };

  return <Modal open={open} onClose={() => { if (!busy) onClose(); }} title={`机器人 ${account} 的入群邀请`} size="xl">
    <div className="space-y-4">
      <p className="text-sm text-text-secondary">这里处理邀请机器人加入新群的邀请。成员加入已有群的申请仍在群管理中处理。归档不代表平台申请失效</p>
      <div className="flex gap-3">
        <select aria-label="邀请记录范围" className="glass-input" value={view} disabled={busy} onChange={(event) => { setView(event.target.value); setCursor(undefined); }}>
          <option value="active">待处理邀请</option><option value="archived">归档邀请</option><option value="all">全部邀请</option>
        </select>
        <Button variant="subtle" disabled={busy || loading} onClick={() => void load()}>刷新</Button>
      </div>
      {error && <p role="alert" className="text-error">{error}</p>}
      {result && <p role="status">{result}</p>}
      {unavailable > 0 && <p className="text-warning">有 {unavailable} 条邀请缺少可读取的原记录, 无法处理</p>}
      {loading ? <p role="status">正在读取邀请...</p> : invitations.length === 0 && <p>当前范围没有可展示的邀请</p>}
      {invitations.map((invitation) => <div key={invitation.request_id} className="rounded-xl border border-glass-border p-3 space-y-2">
        <p>目标群: {invitation.group_id} · 邀请人: {invitation.user_id}</p>
        {invitation.comment && <p className="whitespace-pre-wrap">{invitation.comment}</p>}
        <p className="text-sm text-text-secondary">{invitation.archived ? '已归档' : '近期邀请'} · {formatTime(invitation.received_at)} · ID: {invitation.request_id}</p>
        <p className="text-sm text-text-secondary">{invitation.decision === 'accepted' ? '已接受邀请' : invitation.decision === 'rejected' ? '已拒绝邀请' : states[invitation.execution_state] || invitation.handling_reason}</p>
        <div className="flex gap-2">
          <Button disabled={busy || loading || !invitation.can_handle} onClick={() => handle(invitation, true)}>接受邀请</Button>
          <Button variant="subtle" disabled={busy || loading || !invitation.can_handle} onClick={() => handle(invitation, false)}>拒绝邀请</Button>
        </div>
      </div>)}
      <div className="flex gap-2">
        <Button variant="subtle" disabled={busy || loading || !cursor} onClick={() => setCursor(undefined)}>回到首批</Button>
        <Button variant="subtle" disabled={busy || loading || !nextCursor} onClick={() => setCursor(nextCursor || undefined)}>下一批邀请</Button>
      </div>
      <p className="font-medium">邀请审批与执行记录</p>
      {actions.length === 0 && !loading && <p>暂无邀请动作记录</p>}
      {actions.map((action) => <div key={action.action_id} className="rounded-xl border border-glass-border p-3 space-y-2">
        <p>{action.params.approve ? '接受' : '拒绝'}群 {action.group_id} 的邀请 · {states[action.state] || action.state}</p>
        <p className="text-sm text-text-secondary">ID: {action.action_id}{action.result?.reason ? ` · ${action.result.reason}` : ''}</p>
        {action.state === 'pending' && <div className="flex gap-2">
          <Button disabled={busy || loading} onClick={async () => {
            if ((await confirm(`确认批准机器人${action.params.approve ? '接受' : '拒绝'}群 ${action.group_id} 的邀请?`)))
              void operate(action.action_id, () => accountRequestsApi.decide(adapterId, account, action.action_id, true));
          }}>批准执行</Button>
          <Button variant="subtle" disabled={busy || loading} onClick={() => void operate(action.action_id, () => accountRequestsApi.decide(adapterId, account, action.action_id, false))}>拒绝执行</Button>
        </div>}
      </div>)}
      <div className="flex items-center gap-3">
        <Button variant="subtle" disabled={busy || loading || page === 1} onClick={() => setPage(page - 1)}>上一页记录</Button>
        <span>第 {page} 页 · 共 {total} 条</span>
        <Button variant="subtle" disabled={busy || loading || page * 25 >= total} onClick={() => setPage(page + 1)}>下一页记录</Button>
      </div>
    </div>
  </Modal>;
}
