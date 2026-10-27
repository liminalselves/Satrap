import { createUuid } from '@/utils/uuid';
import { Link } from 'react-router-dom';
import { approvalLink } from '@/api/platformActions';
import { useConfirm } from '@/hooks/useConfirm';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Inbox, Mail, RefreshCw } from 'lucide-react';
import { accountRequestsApi, type GroupInvitation, type InvitationAction } from '@/api/accountRequests';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { EmptyState } from '@/components/kit/Feedback';
import { Modal } from '@/components/kit/Modal';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';
import { Avatar, Pager, SectionHead, hueFor } from './shared';

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

  const operate = async (key: string, approve: boolean, task: (actionId: string) => Promise<InvitationAction>) => {
    if (busy) return;
    const generation = epoch.current;
    setBusy(true); setError(''); setResult('');
    let actionId = operationIds.current.get(key);
    if (!actionId) { actionId = createUuid(); operationIds.current.set(key, actionId); }
    try {
      const record = await task(actionId);
      if (generation !== epoch.current) return;
      setResult(`${approve ? '接受' : '拒绝'}群 ${record.group_id} 的邀请 · ${states[record.state] || record.state}`);
      setCursor(undefined); setPage(1); setRefresh((value) => value + 1);   // 处理后原游标可能移入归档, 从首批重新读取
    } catch (caught) {
      if (generation === epoch.current) setError(errorText(caught));
    } finally { setBusy(false); }
  };

  const handle = async (invitation: GroupInvitation, approve: boolean) => {
    if (!(await confirm(`确认${approve ? '接受' : '拒绝'}群 ${invitation.group_id} 对机器人的入群邀请?`))) return;
    void operate(`${invitation.request_id}:${invitation.revision}:${approve}`, approve, (id) =>
      accountRequestsApi.handle(adapterId, account, invitation, approve, id));
  };

  return <Modal open={open} onClose={() => { if (!busy) onClose(); }} title={`机器人 ${account} 的入群邀请`} size="xl"
    icon={<Mail className="h-[18px] w-[18px]" />} className="tint-pink"
    description="处理邀请机器人加入新群的邀请; 成员加入已有群的申请仍在群管理中处理">
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-[12px] text-fg-3">
          <span className="sr-only">邀请记录范围</span>
          <select aria-label="邀请记录范围" className="field !h-8 !w-[142px] !text-[12.5px]" value={view} disabled={busy}
            onChange={(event) => { setView(event.target.value); setCursor(undefined); setPage(1); }}>
            <option value="active">待处理邀请</option><option value="archived">归档邀请</option><option value="all">全部邀请</option>
          </select>
        </label>
        <Button variant="ghost" size="sm" icon={<RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin-slow' : ''}`} />}
          disabled={busy || loading} onClick={() => void load()}>刷新</Button>
        <span className="ml-auto text-[11.5px] text-fg-4">归档不代表平台申请失效</span>
      </div>
      {error && <p role="alert" className="rounded-xl border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.08)] px-3 py-2 text-[12.5px] text-red">{error}</p>}
      {result && <div role="status" className="rounded-xl border border-line bg-glass px-3 py-2 text-[12.5px] text-fg-2">
        <span className="break-all">{result}</span> <Link className="text-accent hover:underline" to={approvalLink({ platform: adapterId, account, source: 'invitation', state: 'all' })}>查看审批与执行结果</Link>
      </div>}
      {unavailable > 0 && <p className="text-[12.5px] text-orange">有 {unavailable} 条邀请缺少可读取的原记录, 无法处理</p>}
      {loading ? <p role="status" className="text-[12.5px] text-fg-3">正在读取邀请...</p>
        : invitations.length === 0 ? (
          <EmptyState tint="pink" icon={<Inbox className="h-6 w-6" />} title="当前范围没有可展示的邀请" description="切换范围查看历史邀请。" />
        ) : (
          <div className="space-y-2.5">
            {invitations.map((invitation) => (
              <div key={invitation.request_id} className="rounded-xl border border-line bg-glass p-3.5">
                <div className="flex flex-wrap items-start gap-3">
                  <Avatar name={`群 ${invitation.group_id}`} hue={hueFor(invitation.group_id)} square online />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[13px] font-medium text-fg">群 {invitation.group_id}</span>
                      {invitation.archived ? <Badge>已归档</Badge> : <Badge tone="green" dot>待处理</Badge>}
                      {invitation.decision === 'accepted' ? <Badge tone="green">已接受邀请</Badge>
                        : invitation.decision === 'rejected' ? <Badge tone="red">已拒绝邀请</Badge>
                        : <Badge tone={invitation.can_handle ? 'pink' : 'orange'}>{states[invitation.execution_state] || invitation.handling_reason}</Badge>}
                    </div>
                    <p className="mt-1 text-[12px] text-fg-3">目标群: {invitation.group_id} · 邀请人: {invitation.user_id}</p>
                    {invitation.comment && <p className="mt-1.5 whitespace-pre-wrap break-words text-[12.5px] leading-5 text-fg-2">“{invitation.comment}”</p>}
                    <p className="mt-1 text-[11.5px] text-fg-4">{formatTime(invitation.received_at)} · 修订 {invitation.revision} · <span className="num font-mono text-[10.5px]">{invitation.request_id}</span> · {invitation.handling_reason}</p>
                  </div>
                  <div className="flex flex-none items-center gap-1.5">
                    <Button size="sm" variant="tinted" tint="green" icon={<Check className="h-3.5 w-3.5" />}
                      disabled={busy || loading || !invitation.can_handle} onClick={() => handle(invitation, true)}>接受邀请</Button>
                    <Button size="sm" variant="ghost" disabled={busy || loading || !invitation.can_handle}
                      onClick={() => handle(invitation, false)}>拒绝邀请</Button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button size="sm" variant="ghost" disabled={busy || loading || !cursor} onClick={() => setCursor(undefined)}>回到首批</Button>
        <span className="text-[11.5px] text-fg-4">游标分页 · 每批由平台返回</span>
        <Button size="sm" variant="ghost" disabled={busy || loading || !nextCursor} onClick={() => setCursor(nextCursor || undefined)}>下一批邀请</Button>
      </div>

      <div className="border-t border-line pt-3">
        <SectionHead tint="pink" title="邀请审批与执行记录" description="待人工批准的动作统一在审批中心处理" className="mb-3"
          actions={<Badge tone="pink">{total} 条</Badge>} />
        {actions.length === 0 && !loading ? <p className="text-[12px] text-fg-3">暂无邀请动作记录</p> : (
          <div className="space-y-2">
            {actions.map((action) => (
              <div key={action.action_id} className="flex flex-wrap items-center gap-2 rounded-xl border border-line px-3 py-2 text-[12px]">
                <span className="text-fg-2">{action.params.approve ? '接受' : '拒绝'}群 {action.group_id} 的邀请</span>
                <Badge tone={action.state === 'succeeded' ? 'green' : action.state === 'failed' ? 'red' : action.state === 'pending' ? 'orange' : 'neutral'}>
                  {states[action.state] || action.state}
                </Badge>
                <span className="num font-mono text-[10.5px] text-fg-4">{action.action_id}</span>
                {action.result?.reason && <span className="text-fg-3">· {action.result.reason}</span>}
                {action.state === 'pending' && (
                  <Link className="ml-auto text-[12px] text-accent hover:underline"
                    to={approvalLink({ platform: adapterId, account, source: 'invitation', action: action.action_id })}>前往审批中心</Link>
                )}
              </div>
            ))}
          </div>
        )}
        <div className="mt-2">
          <Pager page={page} totalPages={Math.max(1, Math.ceil(total / 25))} onChange={setPage} disabled={busy || loading}
            label={<span>第 {page} 页 · 共 {total} 条</span>} />
        </div>
      </div>
    </div>
  </Modal>;
}
