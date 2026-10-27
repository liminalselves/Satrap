import { createUuid } from '@/utils/uuid';
import { approvalLink } from '@/api/platformActions';
import { useConfirm } from '@/hooks/useConfirm';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  AlertTriangle, ArrowLeft, Ban, Check, CheckCircle2, Clock3, Inbox, MailOpen, RefreshCw, ScanLine,
  Search, Shield, ShieldCheck, Trash2, UserCheck, UserMinus, UserPlus,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, Card, EmptyState, Field, Input, Modal, PageHeader, Textarea } from '@/components/kit';
import { toast } from '@/components/ui/Toast';
import { friendApi, type Friend, type FriendRequest, type FriendAction, type FriendInfo, type RequestPolicy } from '@/api/friends';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';
import { Avatar, Note, Pager, SectionHead } from '../Platforms/shared';

const states: Record<string, string> = { ready: '准备执行', pending: '等待人工批准', executing: '正在执行',
  succeeded: '平台返回成功', rejected: '已拒绝', expired: '已过期', failed: '执行失败', unknown: '结果未确认' };
const stateTone = (state: string): 'green' | 'red' | 'orange' | 'neutral' => (
  state === 'succeeded' ? 'green' : state === 'failed' || state === 'rejected' ? 'red'
    : state === 'pending' || state === 'unknown' || state === 'executing' || state === 'ready' ? 'orange' : 'neutral'
);
const outcome = (action: FriendAction) => {
  const verification = action.result?.verification;
  return [states[action.state] || action.state, action.result?.message,
    verification === 'confirmed' ? '已核查: 好友关系已删除' : verification === 'still_present' ? '好友列表仍显示此人, 请稍后刷新核查'
      : action.state === 'succeeded' && action.action_type === 'delete_friend' ? '尚未核查好友关系是否消失' : ''].filter(Boolean).join(' · ');
};
const actionTitle = (action: FriendAction) => action.action_type === 'send_request' ? '发送好友申请'
  : action.action_type === 'delete_friend' ? '删除好友' : action.params.approve ? '同意好友申请' : '拒绝好友申请';
const platformStateLabels: Record<string, string> = { pending: '待处理', processed: '已处理', invalid: '已失效', unknown: '待确认' };
const executionStateLabels: Record<string, string> = { not_started: '未执行', ...states };
type Selection = { kind: 'friend'; friend: Friend } | { kind: 'request'; request: FriendRequest; approve: boolean } | { kind: 'send' };
type FriendListPage = Awaited<ReturnType<typeof friendApi.list>> & { total?: number | null; total_loaded?: number };

export function Friends() {
  const { adapterId = '' } = useParams();
  const [info, setInfo] = useState<FriendInfo | null>(null);
  const [tab, setTab] = useState<'friends' | 'requests' | 'actions' | 'protection'>('friends');
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [friends, setFriends] = useState<Friend[]>([]);
  const [friendTotal, setFriendTotal] = useState<number | null | undefined>(undefined);
  const [friendTotalLoaded, setFriendTotalLoaded] = useState(0);
  const [requests, setRequests] = useState<FriendRequest[]>([]);
  const [requestView, setRequestView] = useState<'active' | 'archived' | 'all'>('active');
  const [requestCategory, setRequestCategory] = useState<'all' | 'normal' | 'suspicious'>('all');
  const [requestCoverage, setRequestCoverage] = useState('');
  const [requestUnavailableReasons, setRequestUnavailableReasons] = useState<string[]>([]);
  const [requestPolicy, setRequestPolicy] = useState<RequestPolicy | null>(null);
  const [policyOpen, setPolicyOpen] = useState(false);
  const [actions, setActions] = useState<FriendAction[]>([]);
  const [cursors, setCursors] = useState<string[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [moreActions, setMoreActions] = useState(false);
  const [incomplete, setIncomplete] = useState(false);
  const [unavailableCount, setUnavailableCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [remark, setRemark] = useState('');
  const [requestTarget, setRequestTarget] = useState('');
  const [requestMessage, setRequestMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [operationId, setOperationId] = useState('');
  const [result, setResult] = useState<FriendAction | null>(null);
  const [protectedText, setProtectedText] = useState('');
  const account = info?.current_account || '';
  const scope = `${adapterId}\0${account}`;
  const confirm = useConfirm(scope);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const busyRef = useRef(false);
  const currentCursor = cursors[cursors.length - 1];
  const cap = (name: string) => info?.capabilities[name];
  const available = (name: string) => ['supported', 'unknown'].includes(cap(name)?.state || '');
  const protectedIds = [...(info?.manager_ids || []), ...(info?.protected_friend_ids || [])];

  useEffect(() => {
    let live = true;
    setInfo(null); setSelected(null); setFriends([]); setRequests([]); setActions([]); setCursors([]); setPage(1); setError('');
    setRequestCoverage(''); setRequestUnavailableReasons([]); setUnavailableCount(0);
    setFriendTotal(undefined); setFriendTotalLoaded(0);
    friendApi.info(adapterId).then((value) => { if (live) { setInfo(value); setProtectedText(value.protected_friend_ids.join('\n')); } })
      .catch((caught) => { if (live) setError(errorText(caught)); });
    return () => { live = false; };
  }, [adapterId]);

  const refreshInfo = useCallback(async () => {
    const value = await friendApi.info(adapterId);
    if (scopeRef.current !== scope) return;
    if (value.current_account !== account) { setSelected(null); setFriends([]); setRequests([]); setActions([]); setCursors([]); setPage(1);
      setRequestCoverage(''); setRequestUnavailableReasons([]); setUnavailableCount(0); setFriendTotal(undefined); setFriendTotalLoaded(0); }
    setInfo(value); setProtectedText(value.protected_friend_ids.join('\n'));
  }, [adapterId, account, scope]);

  useEffect(() => {
    if (!account || tab !== 'requests' || !['supported', 'unknown'].includes(info?.capabilities.list_requests?.state || '')) return;
    let live = true;
    setRequestPolicy(null);
    friendApi.requestPolicy(adapterId, account).then((value) => { if (live) setRequestPolicy(value); })
      .catch((caught) => { if (live) setError(errorText(caught)); });
    return () => { live = false; };
  }, [adapterId, account, tab, info?.capabilities.list_requests?.state]);

  useEffect(() => {
    if (!account || tab === 'protection') return;
    const capability = tab === 'friends' ? 'list_friends' : tab === 'requests' ? 'list_requests' : null;
    if (capability && !['supported', 'unknown'].includes(info?.capabilities[capability]?.state || '')) return;
    let live = true;
    setLoading(true); setError('');
    if (tab === 'requests') { setRequestCoverage(''); setRequestUnavailableReasons([]); setUnavailableCount(0); }
    const request = tab === 'friends' ? friendApi.list(adapterId, account, search, currentCursor)
      : tab === 'requests' ? friendApi.requests(adapterId, account, currentCursor, requestView, requestCategory) : friendApi.actions(adapterId, account, page);
    request.then((value) => {
      if (!live) return;
      if (tab === 'friends') {
        const data = value as FriendListPage;
        setFriends(data.items); setNext(data.next_cursor); setIncomplete(data.coverage?.complete === false);
        // 覆盖完整时 total 才是平台总数; 否则 total 为 null, 只展示已载入数量
        setFriendTotal(typeof data.total === 'number' ? data.total : data.total === null ? null : undefined);
        setFriendTotalLoaded(typeof data.total_loaded === 'number' ? data.total_loaded : data.items.length);
      }
      if (tab === 'requests') { const data = value as Awaited<ReturnType<typeof friendApi.requests>>; setRequests(data.items); setNext(data.next_cursor); setUnavailableCount(data.unavailable_count || 0);
        const source = data.coverage?.suspicious; setRequestCoverage(source?.reason || (source?.truncated ? '平台可疑申请查询达到数量上限, 结果不完整' : ''));
        setRequestUnavailableReasons((source?.unavailable_reasons || []).map((item) => `有 ${item.count} 条可疑申请未能读取: ${item.message}`)); }
      if (tab === 'actions') { const data = value as Awaited<ReturnType<typeof friendApi.actions>>; setActions(data.items); setMoreActions(data.has_more); }
    }).catch((caught) => { if (live) setError(errorText(caught)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [adapterId, account, tab, search, currentCursor, page, refresh, requestView, requestCategory, info?.capabilities]);

  const open = (selection: Selection) => {
    setSelected(selection); setRemark(''); setSubmitted(false); setResult(null); setOperationId(createUuid()); setError('');
  };
  const run = async () => {
    if (!selected || busyRef.current || submitted) return;
    busyRef.current = true; setBusy(true); setSubmitted(true); setError('');
    const requestScope = scope;
    try {
      const data = selected.kind === 'friend'
        ? await friendApi.submit(adapterId, account, operationId, 'delete_friend', { user_id: selected.friend.user_id })
        : selected.kind === 'send'
          ? await friendApi.submit(adapterId, account, operationId, 'send_request', { user_id: requestTarget.trim(), message: requestMessage })
          : await friendApi.submit(adapterId, account, operationId, 'handle_request', { request_id: selected.request.request_id, approve: selected.approve, remark: selected.approve && selected.request.request_category !== 'suspicious' ? remark : '', expected_revision: selected.request.revision });
      if (scopeRef.current !== requestScope) return;
      setResult(data); setRefresh((value) => value + 1);
      await refreshInfo();
    } catch (caught) {
      if (scopeRef.current === requestScope) setError(`${errorText(caught)}; 请在操作记录核查, 不要重复提交. 动作 ID: ${operationId}`);
    } finally { busyRef.current = false; if (scopeRef.current === requestScope) setBusy(false); }
  };
  const saveProtection = async () => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true);
    try { const value = await friendApi.policy(adapterId, account, protectedText.split(/\r?\n/).map((id) => id.trim()).filter(Boolean)); if (scopeRef.current === scope) { setInfo((old) => old ? { ...old, ...value } : old); toast('success', '保护名单已保存'); } }
    catch (caught) { if (scopeRef.current === scope) setError(errorText(caught)); }
    finally { busyRef.current = false; if (scopeRef.current === scope) setBusy(false); }
  };
  const requestOperation = async (request: FriendRequest, remove: boolean) => {
    if (busyRef.current) return;
    if (remove && !(await confirm('仅删除本地归档记录, 不处理平台申请, 也不删除防重复执行记录. 确认删除?'))) return;
    busyRef.current = true; setBusy(true);
    try {
      if (remove) await friendApi.deleteRequest(adapterId, account, request.request_id, request.revision);
      else {
        const data = await friendApi.recheckRequest(adapterId, account, request.request_id);
        if (scopeRef.current === scope) toast('info', data.verification === 'platform_pending' ? '平台查询确认这条可疑申请仍在待处理列表中'
          : data.platform_query_supported ? '本次平台查询未确认申请仍待处理, 不能据此判定已处理或失效' : '仅完成本地核验, 当前平台无法查询申请是否仍有效');
      }
      if (scopeRef.current === scope) setRefresh((value) => value + 1);
    } catch (caught) { if (scopeRef.current === scope) setError(errorText(caught)); }
    finally { busyRef.current = false; if (scopeRef.current === scope) setBusy(false); }
  };
  const saveRequestPolicy = async () => {
    if (!requestPolicy || busyRef.current) return;
    busyRef.current = true; setBusy(true);
    try {
      const value = await friendApi.saveRequestPolicy(adapterId, account, requestPolicy);
      if (scopeRef.current === scope) { setRequestPolicy(value); setCursors([]); setRefresh((value) => value + 1); toast('success', '申请保留设置已保存'); }
    } catch (caught) { if (scopeRef.current === scope) setError(errorText(caught)); }
    finally { busyRef.current = false; if (scopeRef.current === scope) setBusy(false); }
  };

  const refreshAll = () => {
    setCursors([]); setPage(1);
    refreshInfo().then(() => setRefresh((value) => value + 1)).catch((caught) => setError(errorText(caught)));
  };

  const totalText = friendTotal === undefined
    ? `已载入 ${friendTotalLoaded || friends.length} 人 · 总数未知`
    : friendTotal === null
      ? `已载入 ${friendTotalLoaded || friends.length} 人 · 平台总数未知`
      : `共 ${friendTotal} 人`;

  const tabs = [
    { key: 'friends' as const, label: '好友列表', aria: '好友列表', icon: <UserCheck className="h-3.5 w-3.5" />, count: friends.length },
    { key: 'requests' as const, label: '好友申请', aria: '好友申请', icon: <UserPlus className="h-3.5 w-3.5" />, count: requests.filter((request) => !request.archived).length },
    { key: 'actions' as const, label: '操作记录', aria: '操作记录与审批', icon: <Clock3 className="h-3.5 w-3.5" />, count: actions.length },
    { key: 'protection' as const, label: '删除保护', aria: '删除保护', icon: <Shield className="h-3.5 w-3.5" />, count: protectedIds.length },
  ];

  const cursorPage = cursors.length + 1;
  const cursorPages = cursorPage + (next ? 1 : 0);
  const changeCursorPage = (target: number) => {
    if (target > cursorPage) { if (next) setCursors((values) => [...values, next]); }
    else if (target < cursorPage) setCursors((values) => values.slice(0, -1));
  };

  // 平台明确不支持好友能力时, 不展示空列表冒充"没有好友"
  if (info && !available('list_friends') && info.capabilities.list_friends?.state === 'unsupported') {
    return (
      <div className="space-y-5">
        <PageHeader title="好友管理" description={<>平台: <span className="font-mono text-fg-2">{adapterId}</span></>}
          eyebrow={<Link to="/platforms" className="inline-flex items-center gap-1.5 text-[12px] text-fg-3 transition hover:text-pink"><ArrowLeft className="h-3.5 w-3.5" />平台状态 / 好友</Link>} />
        <Card padding="none">
          <EmptyState tint="pink" icon={<UserCheck className="h-6 w-6" />} title="此平台不提供好友管理"
            description={info.capabilities.list_friends?.reason || '当前适配器类型没有好友关系或好友申请能力。可以返回平台状态查看连接信息。'}
            actions={<>
              <Link to="/platforms" className="ctl">返回平台状态</Link>
              <Button variant="ghost" onClick={refreshAll} disabled={loading}>重试连接</Button>
            </>} />
        </Card>
        <Note tone="neutral" icon={<Shield className="h-4 w-4" />}>好友能力因平台而异; 不支持时不会展示空列表或假数据。</Note>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="好友管理"
        eyebrow={<Link to="/platforms" className="inline-flex items-center gap-1.5 text-[12px] text-fg-3 transition hover:text-pink"><ArrowLeft className="h-3.5 w-3.5" />平台状态 / 好友</Link>}
        description={<>平台: <span className="font-mono text-fg-2">{adapterId}</span> · 机器人账号: <span className="font-mono text-fg-2">{account || '尚未确认'}</span></>}
        actions={<>
          <span className="hidden items-center gap-2 rounded-full border border-line bg-glass px-2.5 py-1 sm:flex">
            <Avatar name={account || '?'} size="sm" />
            <span className="max-w-[160px] truncate text-[12px] text-fg-2">{account || '账号未确认'}</span>
            <Badge tone="neutral">{account ? '机器人账号' : '等待确认'}</Badge>
          </span>
          <Button variant="ghost" icon={<RefreshCw className={cn('h-4 w-4', (loading || busy) && 'animate-spin-slow')} />} onClick={refreshAll} disabled={busy || loading}>刷新</Button>
        </>}
      />

      <p className="text-[12px] text-fg-4">管理此机器人账号的好友关系与申请。人工操作独立于模型插件开关。</p>

      <div className="flex flex-wrap items-center gap-2 animate-rise">
        <div className="flex max-w-full flex-wrap items-center gap-1 rounded-[12px] border border-line bg-glass p-1" role="group" aria-label="好友分区">
          {tabs.map((item) => (
            <button key={item.key} type="button" aria-label={item.aria} aria-pressed={tab === item.key}
              onClick={() => { setTab(item.key); setCursors([]); setPage(1); setNext(null); setError(''); }}
              className={cn('inline-flex h-8 items-center gap-2 rounded-[9px] px-3 text-[13px] font-medium transition',
                tab === item.key ? 'bg-[rgb(var(--rgb-pink)/0.16)] text-pink' : 'text-fg-3 hover:text-fg')}>
              {item.icon}{item.label}
              <span className={cn('num rounded-full px-1.5 text-[11px] leading-[18px]', tab === item.key ? 'bg-[rgb(var(--rgb-pink)/0.2)]' : 'bg-glass-hover text-fg-3')}>{item.count}</span>
            </button>
          ))}
        </div>
        <span className="ml-auto hidden items-center gap-1.5 text-[11.5px] text-fg-4 md:flex">
          <ShieldCheck className="h-3.5 w-3.5" />保护名单同时约束人工与模型申请
        </span>
      </div>

      {error && <div role="alert" className="rounded-xl border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.1)] px-3.5 py-2.5 text-[13px] text-red">{error}</div>}
      {!account && <Note tone="neutral" title="尚未确认机器人账号">请先启动平台并等待机器人账号确认; 不支持好友操作的平台可以在下方查看能力说明。</Note>}

      {tab === 'friends' && (
        <div className="animate-fade">
          <Card padding="none">
            <div className="flex flex-wrap items-center gap-3 border-b border-line p-4">
              <form className="relative w-full max-w-sm" onSubmit={(event) => { event.preventDefault(); setSearch(query.trim()); setCursors([]); setRefresh((value) => value + 1); }}>
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-4" />
                <input className="field w-full !pl-9" aria-label="搜索好友" placeholder="输入好友 ID, 昵称或备注" maxLength={256}
                  value={query} onChange={(event) => setQuery(event.target.value)} />
                <Button type="submit" size="sm" variant="ghost" className="absolute right-1 top-1/2 -translate-y-1/2" disabled={loading || !account}>搜索</Button>
              </form>
              <span className="text-[12px] text-fg-3">{totalText}</span>
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <Button variant="primary" size="sm" icon={<UserPlus className="h-3.5 w-3.5" />}
                  disabled={busy || !account || !available('send_request')}
                  onClick={() => { setRequestTarget(''); setRequestMessage(''); open({ kind: 'send' }); }}>发送好友申请</Button>
                {!available('send_request') && <span className="text-[11.5px] text-orange">{cap('send_request')?.reason || '当前平台尚未提供主动好友申请能力'}</span>}
              </div>
            </div>

            {!available('list_friends') ? (
              <EmptyState tint="pink" icon={<UserCheck className="h-6 w-6" />} title="当前平台不支持好友列表"
                description={cap('list_friends')?.reason || '当前平台不支持好友列表'} />
            ) : loading ? (
              <div className="space-y-1 p-3">
                {[0, 1, 2].map((index) => <div key={index} className="flex items-center gap-3 px-2 py-3"><div className="skeleton h-10 w-10 rounded-xl" /><div className="flex-1 space-y-2"><div className="skeleton h-3 w-1/3" /><div className="skeleton h-3 w-1/5" /></div></div>)}
              </div>
            ) : friends.length === 0 ? (
              <EmptyState tint="pink" icon={<UserCheck className="h-6 w-6" />}
                title={search ? `没有匹配 “${search}” 的好友` : '没有查询到好友'}
                description={search ? '搜索只覆盖本次取得的目录; 换个昵称、备注或 ID 试试。' : '当前账号的好友列表为空, 或平台尚未返回目录。'}
                actions={search ? <Button onClick={() => { setQuery(''); setSearch(''); setRefresh((value) => value + 1); }}>清除搜索</Button> : <Button onClick={refreshAll}>重新读取</Button>} />
            ) : (
              <>
                {incomplete && <p role="status" className="border-b border-line bg-[rgb(var(--rgb-orange)/0.08)] px-4 py-2 text-[12px] text-orange">目录覆盖不完整, 不能据此判断不存在其它好友或同名候选</p>}
                <div className="hidden overflow-x-auto md:block">
                  <table className="w-full text-left text-[13px]">
                    <thead>
                      <tr className="border-b border-line text-[11px] text-fg-4">
                        <th className="px-4 py-2.5 font-medium">好友</th>
                        <th className="px-4 py-2.5 font-medium">好友 ID</th>
                        <th className="px-4 py-2.5 font-medium">匹配依据</th>
                        <th className="px-4 py-2.5 text-right font-medium">操作</th>
                      </tr>
                    </thead>
                    <tbody className="stagger">
                      {friends.map((friend, index) => (
                        <tr key={friend.user_id} className="border-b border-line transition-colors last:border-0 hover:bg-glass-hover" style={{ ['--i' as string]: index }}>
                          <td className="px-4 py-2.5">
                            <div className="flex items-center gap-3">
                              <Avatar name={friend.nickname || friend.user_id} size="md" />
                              <div className="min-w-0">
                                <div className="flex items-center gap-1.5">
                                  <span className="truncate font-medium text-fg">{friend.nickname || '未提供'}</span>
                                  {protectedIds.includes(friend.user_id) && <Shield className="h-3 w-3 flex-none text-orange" aria-label="受保护" />}
                                </div>
                                <div className="truncate text-[11.5px] text-fg-3">{friend.remark || '无备注'}</div>
                              </div>
                            </div>
                          </td>
                          <td className="num px-4 py-2.5 font-mono text-[12px] text-fg-2">{friend.user_id}</td>
                          <td className="px-4 py-2.5">
                            <div className="flex flex-wrap gap-1">{(friend.matched_by || []).length
                              ? (friend.matched_by || []).map((item) => <Badge key={item} tone={friend.exact ? 'green' : 'orange'}>{item}{friend.exact ? '' : '·模糊'}</Badge>)
                              : <span className="text-[11.5px] text-fg-4">未记录</span>}</div>
                          </td>
                          <td className="px-4 py-2.5 text-right">
                            <Button size="sm" variant="ghost" disabled={busy || loading} onClick={() => open({ kind: 'friend', friend })}>查看详情</Button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="space-y-2 p-3 md:hidden">
                  {friends.map((friend) => (
                    <button key={friend.user_id} type="button" onClick={() => open({ kind: 'friend', friend })}
                      className="flex w-full items-center gap-3 rounded-xl border border-line bg-glass px-3 py-2.5 text-left transition hover:bg-glass-hover">
                      <Avatar name={friend.nickname || friend.user_id} size="md" />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="truncate text-[13px] font-medium text-fg">{friend.nickname || '未提供'}</span>
                          {protectedIds.includes(friend.user_id) && <Shield className="h-3 w-3 flex-none text-orange" aria-label="受保护" />}
                        </div>
                        <div className="truncate text-[11.5px] text-fg-3">{friend.remark || '无备注'} · {friend.user_id}</div>
                      </div>
                      {(friend.matched_by || []).length > 0 && (
                        <Badge tone={friend.exact ? 'green' : 'orange'}>{(friend.matched_by || [])[0]}</Badge>
                      )}
                    </button>
                  ))}
                </div>
                {!loading && !error && friends.length === 0 && <p className="px-4 py-6 text-center text-[13px] text-fg-3">没有查询到好友</p>}
              </>
            )}
            <div className="border-t border-line px-4 py-2.5">
              <Pager page={cursorPage} totalPages={cursorPages} onChange={changeCursorPage} disabled={loading || busy}
                label={<span>第 {cursorPage} 页 · {totalText}{incomplete ? ' · 目录不完整' : ''}</span>} />
            </div>
          </Card>
        </div>
      )}

      {tab === 'requests' && (
        <div className="space-y-4 animate-fade">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1 rounded-[10px] border border-line bg-glass p-0.5">
              {([['active', '待处理'], ['archived', '已归档'], ['all', '全部记录']] as const).map(([value, label]) => (
                <button key={value} type="button" aria-pressed={requestView === value} disabled={busy}
                  onClick={() => { setRequests([]); setRequestView(value); setCursors([]); setNext(null); }}
                  className={cn('h-7 rounded-lg px-2.5 text-[12px] transition', requestView === value ? 'bg-[rgb(var(--rgb-pink)/0.16)] font-medium text-pink' : 'text-fg-3 hover:text-fg')}>
                  {label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1 rounded-[10px] border border-line bg-glass p-0.5" aria-label="好友申请类别">
              {([['all', '全部类型'], ['normal', '普通申请'], ['suspicious', '可疑申请']] as const).map(([value, label]) => (
                <button key={value} type="button" aria-pressed={requestCategory === value} disabled={busy}
                  onClick={() => { setRequests([]); setRequestCategory(value); setRequestCoverage(''); setCursors([]); setNext(null); }}
                  className={cn('h-7 rounded-lg px-2.5 text-[12px] transition', requestCategory === value ? 'bg-[rgb(var(--rgb-pink)/0.16)] font-medium text-pink' : 'text-fg-3 hover:text-fg')}>
                  {label}
                </button>
              ))}
            </div>
            <span className="text-[11.5px] text-fg-4">普通申请来自后端接收记录, 可疑申请会向平台补取</span>
            <Button size="sm" variant="ghost" className="ml-auto" icon={<Clock3 className="h-3.5 w-3.5" />} onClick={() => setPolicyOpen((value) => !value)}>申请保留设置</Button>
          </div>

          {policyOpen && requestPolicy && (
            <Card padding="md" className="animate-pop">
              <SectionHead tint="pink" title="申请保留设置" description="历史期限不能短于凭据期限; 已清除的凭据不会因延长期限恢复"
                actions={<Button size="sm" variant="tinted" tint="pink" disabled={busy} onClick={saveRequestPolicy}>保存保留设置</Button>} />
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="未处理凭据保留天数" hint="1–3650 天">
                  <Input type="number" min={1} max={3650} aria-label="申请凭据保留天数" value={requestPolicy.credential_days}
                    onChange={(event) => setRequestPolicy({ ...requestPolicy, credential_days: Number(event.target.value) })} />
                </Field>
                <Field label="历史记录保留天数" hint={`不能短于 ${requestPolicy.credential_days} 天`}>
                  <Input type="number" min={requestPolicy.credential_days} max={3650} aria-label="申请历史保留天数" value={requestPolicy.history_days}
                    onChange={(event) => setRequestPolicy({ ...requestPolicy, history_days: Number(event.target.value) })} />
                </Field>
              </div>
              <p className="mt-3 text-[11.5px] text-fg-4">已处理的申请会立即清除凭据; 保留凭据的归档申请仍可确认尝试处理。</p>
            </Card>
          )}

          <Note tone="neutral" icon={<MailOpen className="h-4 w-4" />}>
            普通申请来自后端接收记录, 可疑申请会向平台补取。本地归档不代表平台申请失效; 保留凭据的归档申请仍可确认尝试处理。
          </Note>
          {requestCoverage && <p role="status" className="text-[12px] text-orange">可疑申请查询: {requestCoverage}</p>}
          {requestUnavailableReasons.map((message) => <p key={message} role="status" className="text-[12px] text-orange">{message}</p>)}
          {!available('list_requests') && <Note tone="orange">{cap('list_requests')?.reason || '当前平台不支持好友申请'}</Note>}
          {unavailableCount > 0 && <p className="text-[12px] text-orange">有 {unavailableCount} 条历史申请缺少详情, 无法查询或处理. 刷新不会自动恢复, 平台重新上报未处理申请时可补回详情</p>}

          {loading ? (
            <div className="space-y-2">{[0, 1].map((index) => <div key={index} className="surface h-28 animate-pulse rounded-xl" />)}</div>
          ) : requests.length === 0 ? (
            <Card padding="none"><EmptyState tint="pink" icon={<Inbox className="h-6 w-6" />} title="当前筛选下没有可展示的申请记录"
              description="本地归档不代表平台申请失效; 切换范围可查看历史记录。" /></Card>
          ) : (
            <div className="stagger space-y-2.5">
              {requests.map((request, index) => (
                <Card key={request.request_id} padding="none" tint={request.request_category === 'suspicious' ? 'orange' : 'pink'}
                  className="overflow-hidden" style={{ ['--i' as string]: index }}>
                  <div className="flex flex-wrap items-start gap-3 p-4 pb-3">
                    <Avatar name={request.nickname || request.user_id || '?'} size="lg" />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[14px] font-semibold text-fg">{request.nickname || '昵称未提供'}</span>
                        <span className="num font-mono text-[11px] text-fg-4">{request.user_id || '平台未确认'}</span>
                        {request.request_category === 'suspicious' ? <Badge tone="orange" dot>可疑</Badge> : <Badge tone="pink">普通</Badge>}
                        {request.archived && <Badge>已归档</Badge>}
                        <Badge tone={request.platform_state === 'pending' ? 'green' : request.platform_state === 'invalid' ? 'red' : 'neutral'} dot>
                          平台: {platformStateLabels[request.platform_state] || '待确认'}
                        </Badge>
                        <Badge tone={stateTone(request.execution_state)}>{executionStateLabels[request.execution_state] || request.execution_state}</Badge>
                      </div>
                      <p className="mt-1.5 whitespace-pre-wrap break-words text-[13px] leading-5 text-fg-2">申请人: {request.nickname || '昵称未提供'} · ID: {request.user_id || '平台未确认'}</p>
                      <p className="mt-1 whitespace-pre-wrap break-words text-[13px] leading-5 text-fg-2">验证信息: {request.comment || '无'}</p>
                      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11.5px] text-fg-3">
                        <span>收到: {formatTime(request.received_at)}</span>
                        <span>本地核验期限: {formatTime(request.expires_at)}</span>
                        <span className="num">修订号: {request.revision}</span>
                        {request.request_source && <span>申请来源: {request.request_source}</span>}
                        {request.requested_at && <span>原始申请时间: {formatTime(request.requested_at)}</span>}
                        {request.last_checked_at && <span>上次核验: {formatTime(request.last_checked_at)}</span>}
                      </div>
                      {request.request_category === 'suspicious' && (
                        <p className="mt-1.5 flex items-start gap-1.5 text-[12px] text-orange"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />平台标记为可疑 · 原因: {request.suspicious_reason || '未提供'}</p>
                      )}
                      <p className="mt-1.5 text-[12px] text-fg-3">{request.handling_reason}</p>
                      <p className="mt-1 text-[11.5px] text-fg-4">{`平台状态: ${platformStateLabels[request.platform_state] || '待确认'} · 执行状态: ${executionStateLabels[request.execution_state] || (request.execution_state === 'not_started' ? '未执行' : request.execution_state)}`}</p>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Button size="sm" variant="tinted" tint="green" icon={<Check className="h-3.5 w-3.5" />}
                        disabled={busy || !available('handle_request') || request.can_handle === false}
                        onClick={() => open({ kind: 'request', request, approve: true })}>同意</Button>
                      <Button size="sm" variant="ghost" icon={<Ban className="h-3.5 w-3.5" />}
                        disabled={busy || !available('handle_request') || request.can_handle === false}
                        onClick={() => open({ kind: 'request', request, approve: false })}>拒绝</Button>
                      <Button size="sm" variant="ghost" icon={<ScanLine className="h-3.5 w-3.5" />} disabled={busy} onClick={() => void requestOperation(request, false)}>重新核验</Button>
                      {request.archived && <Button size="sm" variant="ghost" className="!text-red" icon={<Trash2 className="h-3.5 w-3.5" />} disabled={busy}
                        onClick={() => void requestOperation(request, true)}>删除历史</Button>}
                    </div>
                  </div>
                  {!request.can_handle && <p className="border-t border-line bg-[rgb(0_0_0/0.06)] px-4 py-1.5 text-[11.5px] text-fg-4 [[data-theme=light]_&]:bg-white/30">当前不可处理: {request.handling_reason}</p>}
                </Card>
              ))}
            </div>
          )}
          {!available('handle_request') && <p className="text-[12px] text-orange">{cap('handle_request')?.reason || '当前平台不支持处理申请'}</p>}
          <div className="flex flex-wrap items-center justify-between gap-2 text-[12px] text-fg-3">
            <span>共 {requests.length} 条申请{next ? ' · 还有更多' : ''}</span>
            <Pager page={cursorPage} totalPages={cursorPages} onChange={changeCursorPage} disabled={loading || busy}
              label={<span>第 {cursorPage} 页</span>} />
          </div>
        </div>
      )}

      {tab === 'actions' && (
        <div className="animate-fade">
          <Card padding="none">
            <div className="border-b border-line p-4">
              <SectionHead className="mb-0" tint="pink" title="操作记录" description="待人工批准的操作统一在审批中心处理; 此处保留业务操作记录"
                actions={<Badge tone="pink">{actions.length} 条</Badge>} />
            </div>
            {loading ? (
              <div className="space-y-2 p-4">{[0, 1].map((index) => <div key={index} className="skeleton h-16 rounded-xl" />)}</div>
            ) : actions.length === 0 ? (
              <EmptyState tint="pink" icon={<Clock3 className="h-6 w-6" />} title="暂无好友操作记录" description="删除好友、处理申请或发送申请后会在此留下记录。" />
            ) : (
              <ol className="stagger divide-y divide-[var(--glass-border)]">
                {actions.map((action, index) => (
                  <li key={action.action_id} className="flex gap-3 p-4" style={{ ['--i' as string]: index }}>
                    <div className={cn('mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint',
                      action.actor_kind === 'model' ? 'tint-purple' : 'tint-pink')}>
                      {action.actor_kind === 'model' ? <UserPlus className="h-4 w-4" /> : <UserCheck className="h-4 w-4" />}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="text-[13px] font-medium text-fg">{actionTitle(action)}</span>
                        <Badge tone={action.actor_kind === 'model' ? 'purple' : 'pink'}>{action.actor_kind === 'model' ? '模型申请' : '人工操作'}</Badge>
                        <Badge tone={stateTone(action.state)} dot>{states[action.state] || action.state}</Badge>
                        <span className="num ml-auto text-[11px] text-fg-4">{formatTime(action.created_at)}</span>
                      </div>
                      <p className="mt-1 text-[12px] text-fg-3">
                        发起者: {action.actor_kind === 'panel' ? '后台登录用户' : action.actor_id || '模型工具调用者'}
                        <span className="ml-2">目标: {action.target?.nickname || ''} {action.params.user_id || action.params.request_id}{action.target?.remark ? ` · 备注: ${action.target.remark}` : ''}</span>
                      </p>
                      <p className="num mt-0.5 break-all font-mono text-[11px] text-fg-4">动作 ID: {action.action_id}{action.expires_at ? ` · 到期 ${formatTime(action.expires_at)}` : ''}</p>
                      {action.result && <p className="mt-1 text-[12px] text-fg-2">{outcome(action)}</p>}
                      {action.state === 'pending' && (
                        <Link className="mt-1 inline-flex items-center gap-1 text-[12px] text-accent hover:underline"
                          to={approvalLink({ platform: adapterId, account, source: 'friend', action: action.action_id })}>前往审批中心</Link>
                      )}
                      {action.state === 'unknown' && <p className="mt-1 text-[12px] text-orange">结果未确认, 请刷新好友列表核查, 不要重复提交</p>}
                    </div>
                  </li>
                ))}
              </ol>
            )}
            <div className="border-t border-line px-4 py-2.5">
              <Pager page={page} totalPages={page + (moreActions ? 1 : 0)} onChange={setPage} disabled={loading || busy}
                label={<span>第 {page} 页 · 共 {actions.length} 条</span>} />
            </div>
          </Card>
        </div>
      )}

      {tab === 'protection' && (
        <div className="grid gap-4 animate-fade lg:grid-cols-[1.4fr_1fr]">
          <Card padding="lg">
            <SectionHead tint="pink" title="禁止删除的好友" icon={<Shield className="h-4 w-4" />}
              description="每行填写一个好友 ID; 同时约束人工操作和模型申请" />
            <Field label="额外保护好友 ID" hint="留空表示仅保护自动识别的管理者">
              <Textarea rows={7} className="font-mono !text-[12.5px]" value={protectedText} aria-label="额外保护好友 ID"
                onChange={(event) => setProtectedText(event.target.value)} />
            </Field>
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <span className="text-[12px] text-fg-3">当前保护 <span className="num font-medium text-fg-2">{protectedIds.length}</span> 个账号</span>
              <Button variant="primary" icon={<ShieldCheck className="h-4 w-4" />} disabled={busy || !account} onClick={() => void saveProtection()}>保存保护名单</Button>
            </div>
          </Card>
          <Card padding="lg">
            <SectionHead tint="orange" title="自动保护的管理者" description="来自平台管理员身份, 不可在此移除" />
            <div className="flex flex-wrap gap-2">
              {(info?.manager_ids || []).length === 0 && <span className="text-[12px] text-fg-3">暂无</span>}
              {(info?.manager_ids || []).map((id) => (
                <span key={id} className="flex items-center gap-2 rounded-full border border-line bg-glass py-1 pl-1 pr-2.5">
                  <Avatar name={id} size="sm" />
                  <span className="num font-mono text-[10.5px] text-fg-4">{id}</span>
                </span>
              ))}
            </div>
            <div className="mt-4 space-y-2 rounded-xl border border-line bg-glass p-3 text-[12px] leading-5 text-fg-3">
              <p className="flex items-start gap-2"><CheckCircle2 className="mt-0.5 h-3.5 w-3.5 flex-none text-green" />删除受保护账号会被拒绝, 模型工具调用同样受限。</p>
              <p className="flex items-start gap-2"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-orange" />保护名单只阻止删除, 不阻止发送消息或处理申请。</p>
            </div>
          </Card>
        </div>
      )}

      <Modal open={selected !== null} onClose={() => { if (!busy) setSelected(null); }} size="lg"
        title={selected?.kind === 'send' ? '发送好友申请' : selected?.kind === 'friend' ? '好友详情与删除' : selected?.kind === 'request' && selected.approve ? '同意好友申请' : '拒绝好友申请'}
        icon={selected?.kind === 'friend' ? <UserMinus className="h-[18px] w-[18px]" /> : selected?.kind === 'send' ? <UserPlus className="h-[18px] w-[18px]" /> : <MailOpen className="h-[18px] w-[18px]" />}
        description={selected?.kind === 'friend' ? '删除只解除好友关系, 不拉黑, 不删除历史对话或模型上下文' : selected?.kind === 'send' ? '申请发出后需对方同意; 不代表已经成为好友' : '处理结果会写入操作记录'}
        className={selected?.kind === 'request' && !selected.approve ? 'tint-red' : 'tint-pink'}
        footer={<>
          <Button variant="ghost" disabled={busy} onClick={() => setSelected(null)}>{submitted ? '关闭' : '取消'}</Button>
          {submitted && !result && !busy && (
            <Button onClick={() => friendApi.action(adapterId, account, operationId).then((value) => { if (scopeRef.current === scope) setResult(value); }).catch((caught) => setError(errorText(caught)))}>核查此动作</Button>
          )}
          <Button variant="primary" disabled={busy || submitted || (selected?.kind === 'friend' ? !available('delete_friend') || protectedIds.includes(selected.friend.user_id)
            : selected?.kind === 'send' ? !available('send_request') || !requestTarget.trim()
              : selected ? !available('handle_request') || selected.request.can_handle === false : true)} onClick={() => void run()}>
            {busy ? '正在提交…' : selected?.kind === 'send' ? '确认发送' : selected?.kind === 'friend' ? '确认删除' : selected?.kind === 'request' && selected.approve ? '确认同意' : '确认拒绝'}
          </Button>
        </>}>
        {selected && <div className="space-y-4">
          <div className="flex items-center gap-3 rounded-xl border border-line bg-glass p-3">
            <Avatar name={selected.kind === 'friend' ? (selected.friend.nickname || selected.friend.user_id) : selected.kind === 'send' ? (requestTarget || '新申请') : (selected.request.nickname || selected.request.user_id || '申请人')} size="lg" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2 text-[13px] font-medium text-fg">
                {selected.kind === 'friend' ? (selected.friend.nickname || '未提供') : selected.kind === 'send' ? '发送好友申请' : (selected.request.nickname || '申请人')}
                {selected.kind === 'friend' && <Badge tone={selected.friend.exact ? 'green' : 'orange'}>{selected.friend.exact ? '精确匹配' : '模糊匹配'}</Badge>}
              </div>
              <p className="num mt-0.5 font-mono text-[11.5px] text-fg-4">机器人账号 {account} · 平台 {adapterId}</p>
            </div>
          </div>

          {selected.kind === 'friend' && <>
            <div className="divide-y divide-[var(--glass-border)] rounded-xl border border-line px-3.5">
              <p className="py-2 text-[13px]">好友 ID: {selected.friend.user_id}</p>
              <p className="py-2 text-[13px]">好友备注: {selected.friend.remark || '无'}</p>
              <p className="py-2 text-[13px]">昵称: {selected.friend.nickname || '未提供'}</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="ghost" onClick={() => navigator.clipboard.writeText(selected.friend.user_id).then(() => toast('success', 'ID 已复制')).catch(() => toast('error', '复制失败'))}>复制 ID</Button>
              {protectedIds.includes(selected.friend.user_id) && <Badge tone="orange" dot>受保护, 不允许删除</Badge>}
            </div>
            <Note tone="orange" title="删除说明">删除只解除好友关系, 不拉黑, 不删除历史对话或模型上下文。结果未知时请先核查, 不要重复提交。</Note>
            {cap('delete_friend')?.reason && <p className="text-[12px] text-fg-3">{cap('delete_friend')?.reason}</p>}
          </>}

          {selected.kind === 'send' && <>
            <Field label="目标账号 ID" required hint="平台账号 ID, 不是昵称">
              <Input aria-label="好友申请目标账号" disabled={submitted} maxLength={256} value={requestTarget}
                placeholder="例如 2410323775" onChange={(event) => setRequestTarget(event.target.value)} />
            </Field>
            <Field label="验证文字 (可选)" hint={`${requestMessage.length} / 200 字符`}>
              <Textarea aria-label="好友申请验证文字" rows={3} maxLength={200} disabled={submitted} value={requestMessage}
                placeholder="例如 你好, 我是萨特普客服" onChange={(event) => setRequestMessage(event.target.value)} />
            </Field>
            <Note tone="pink" title="提交成功 ≠ 成为好友">提交成功只表示申请已发出, 不代表对方已同意或已经成为好友。</Note>
          </>}

          {selected.kind === 'request' && <>
            <div className="divide-y divide-[var(--glass-border)] rounded-xl border border-line px-3.5">
              <p className="py-2 text-[13px]">申请人 ID: {selected.request.user_id || '平台未确认'}</p>
              <p className="py-2 text-[13px]">平台状态: {platformStateLabels[selected.request.platform_state] || '待确认'}</p>
              <p className="py-2 text-[13px]">修订号: {selected.request.revision}</p>
            </div>
            <div className="rounded-xl border border-line bg-glass px-3.5 py-2.5 text-[13px] leading-5 text-fg-2">
              <div className="mb-1 text-[11.5px] text-fg-4">验证信息</div>
              <p className="whitespace-pre-wrap break-words">{selected.request.comment || '无'}</p>
            </div>
            {selected.request.archived && <p role="status" className="rounded-xl border border-[rgb(var(--rgb-orange)/0.3)] bg-[rgb(var(--rgb-orange)/0.08)] px-3.5 py-2.5 text-[12px] text-orange">此申请已在本地归档, 平台状态可能未确认. 点击确认后只尝试处理一次, 结果未知时请核查操作记录</p>}
            {selected.request.request_category === 'suspicious' && (
              <Note tone="orange" title="平台标记为可疑" icon={<AlertTriangle className="h-4 w-4" />}>
                {selected.request.suspicious_reason || '未提供'}; 此申请不支持填写好友备注, 同意操作需人工审批。
              </Note>
            )}
            {selected.approve && selected.request.request_category !== 'suspicious' && (
              <Field label="好友备注 (可选)" hint="最多 60 字符">
                <Input aria-label="好友备注" maxLength={60} disabled={submitted} value={remark} placeholder="例如 店铺运营" onChange={(event) => setRemark(event.target.value)} />
              </Field>
            )}
          </>}

          {result && (
            <div className="rounded-xl border border-line bg-glass p-3 text-[13px] animate-pop" role="status">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={stateTone(result.state)} dot>{states[result.state] || result.state}</Badge>
                <span className="num font-mono text-[11px] text-fg-4">{result.action_id}</span>
              </div>
              <p className="mt-1.5 text-fg-2">{outcome(result)}</p>
              {result.state === 'pending' && (
                <Link className="mt-2 inline-flex text-[12px] text-accent hover:underline"
                  to={approvalLink({ platform: adapterId, account, source: 'friend', action: result.action_id })}>已提交, 前往审批中心</Link>
              )}
              {result.state === 'unknown' && <p className="mt-1 text-[12px] text-orange">结果未确认, 请刷新好友列表核查, 不要重复提交。</p>}
            </div>
          )}
          {submitted && result && <p className="text-[11.5px] text-fg-4">动作提交后可在「操作记录」核查执行结果。</p>}
          {error && <p className="text-[12px] text-red">{error}</p>}
        </div>}
      </Modal>
    </div>
  );
}
