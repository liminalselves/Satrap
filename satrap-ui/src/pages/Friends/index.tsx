import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { friendApi, type Friend, type FriendRequest, type FriendAction, type FriendInfo, type RequestPolicy } from '@/api/friends';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { toast } from '@/components/ui/Toast';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';

const states: Record<string, string> = { ready: '准备执行', pending: '等待人工批准', executing: '正在执行',
  succeeded: '平台返回成功', rejected: '已拒绝', expired: '已过期', failed: '执行失败', unknown: '结果未确认' };
const outcome = (action: FriendAction) => {
  const verification = action.result?.verification;
  return [states[action.state] || action.state, action.result?.message,
    verification === 'confirmed' ? '已核查: 好友关系已删除' : verification === 'still_present' ? '好友列表仍显示此人, 请稍后刷新核查'
      : action.state === 'succeeded' && action.action_type === 'delete_friend' ? '尚未核查好友关系是否消失' : ''].filter(Boolean).join(' · ');
};
type Selection = { kind: 'friend'; friend: Friend } | { kind: 'request'; request: FriendRequest; approve: boolean } | { kind: 'send' };

export function Friends() {
  const { adapterId = '' } = useParams();
  const [info, setInfo] = useState<FriendInfo | null>(null);
  const [tab, setTab] = useState<'friends' | 'requests' | 'actions' | 'protection'>('friends');
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [friends, setFriends] = useState<Friend[]>([]);
  const [requests, setRequests] = useState<FriendRequest[]>([]);
  const [requestView, setRequestView] = useState<'active' | 'archived' | 'all'>('active');
  const [requestCategory, setRequestCategory] = useState<'all' | 'normal' | 'suspicious'>('all');
  const [requestCoverage, setRequestCoverage] = useState('');
  const [requestUnavailableReasons, setRequestUnavailableReasons] = useState<string[]>([]);
  const [requestPolicy, setRequestPolicy] = useState<RequestPolicy | null>(null);
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
    friendApi.info(adapterId).then((value) => { if (live) { setInfo(value); setProtectedText(value.protected_friend_ids.join('\n')); } })
      .catch((caught) => { if (live) setError(errorText(caught)); });
    return () => { live = false; };
  }, [adapterId]);

  const refreshInfo = useCallback(async () => {
    const value = await friendApi.info(adapterId);
    if (scopeRef.current !== scope) return;
    if (value.current_account !== account) { setSelected(null); setFriends([]); setRequests([]); setActions([]); setCursors([]); setPage(1);
      setRequestCoverage(''); setRequestUnavailableReasons([]); setUnavailableCount(0); }
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
      if (tab === 'friends') { const data = value as Awaited<ReturnType<typeof friendApi.list>>; setFriends(data.items); setNext(data.next_cursor); setIncomplete(data.coverage?.complete === false); }
      if (tab === 'requests') { const data = value as Awaited<ReturnType<typeof friendApi.requests>>; setRequests(data.items); setNext(data.next_cursor); setUnavailableCount(data.unavailable_count || 0);
        const source = data.coverage?.suspicious; setRequestCoverage(source?.reason || (source?.truncated ? '平台可疑申请查询达到数量上限, 结果不完整' : ''));
        setRequestUnavailableReasons((source?.unavailable_reasons || []).map((item) => `有 ${item.count} 条可疑申请未能读取: ${item.message}`)); }
      if (tab === 'actions') { const data = value as Awaited<ReturnType<typeof friendApi.actions>>; setActions(data.items); setMoreActions(data.has_more); }
    }).catch((caught) => { if (live) setError(errorText(caught)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [adapterId, account, tab, search, currentCursor, page, refresh, requestView, requestCategory, info?.capabilities]);

  const open = (selection: Selection) => {
    setSelected(selection); setRemark(''); setSubmitted(false); setResult(null); setOperationId(crypto.randomUUID()); setError('');
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
  const decide = async (action: FriendAction, approve: boolean) => {
    if (busyRef.current) return;
    if (approve && !window.confirm(action.action_type === 'delete_friend'
      ? `机器人 ${account}\n删除好友 ${action.target?.nickname || ''} (${action.params.user_id})\n备注: ${action.target?.remark || '无'}\n确认批准此申请? 历史对话和上下文保留`
      : `机器人 ${account}\n${action.params.approve ? '同意' : '拒绝'}归档好友申请 ${action.params.request_id}\n平台状态可能尚未确认, 是否批准一次处理尝试?`)) return;
    busyRef.current = true; setBusy(true);
    try { const data = await friendApi.decide(adapterId, account, action.action_id, approve); if (scopeRef.current === scope) { toast(data.state === 'failed' || data.state === 'unknown' ? 'error' : 'success', outcome(data)); setRefresh((value) => value + 1); } }
    catch (caught) { if (scopeRef.current === scope) setError(`${errorText(caught)}; 请刷新操作记录核查结果`); }
    finally { busyRef.current = false; if (scopeRef.current === scope) setBusy(false); }
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
    if (remove && !window.confirm('仅删除本地归档记录, 不处理平台申请, 也不删除防重复执行记录. 确认删除?')) return;
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

  return <div className="space-y-4">
    <Link to="/platforms" className="text-blue-500">返回平台管理</Link>
    <div className="flex flex-wrap justify-between gap-3"><div><h1 className="text-2xl font-bold">好友管理</h1><p>平台: {adapterId} · 机器人账号: {account || '尚未确认'}</p></div>
      <Button disabled={busy || loading} onClick={() => { setCursors([]); setPage(1); refreshInfo().then(() => setRefresh((value) => value + 1)).catch((caught) => setError(errorText(caught))); }}>刷新</Button></div>
    <p className="text-sm text-gray-500">管理此机器人账号的好友关系与申请. 人工操作独立于模型插件开关</p>
    {error && <div role="alert" className="rounded p-3 bg-red-500/10 text-red-600">{error}</div>}
    {!account && <Card><p>请先启动平台并等待机器人账号确认. 不支持好友操作的平台可以查看能力说明</p></Card>}
    <div className="flex flex-wrap gap-2">{([['friends', '好友列表'], ['requests', '好友申请'], ['actions', '操作记录与审批'], ['protection', '删除保护']] as const).map(([key, label]) =>
      <Button key={key} disabled={busy} variant={tab === key ? 'primary' : 'default'} onClick={() => { setTab(key); setCursors([]); setPage(1); setNext(null); setError(''); }}>{label}</Button>)}</div>
    {tab === 'friends' && <Card>
      <div className="mb-4 space-y-2"><Button disabled={busy || !account || !available('send_request')} onClick={() => { setRequestTarget(''); setRequestMessage(''); open({ kind: 'send' }); }}>发送好友申请</Button>
        <p className="text-sm">{cap('send_request')?.reason || '当前平台尚未提供主动好友申请能力'}</p></div>
      <form className="flex gap-2 mb-4" onSubmit={(event) => { event.preventDefault(); setSearch(query.trim()); setCursors([]); setRefresh((value) => value + 1); }}>
        <input className="glass-input flex-1" aria-label="搜索好友" placeholder="输入好友 ID, 昵称或备注" maxLength={256} value={query} onChange={(event) => setQuery(event.target.value)} /><Button type="submit" disabled={loading || !account}>搜索</Button>
      </form>
      {!available('list_friends') ? <p>{cap('list_friends')?.reason || '当前平台不支持好友列表'}</p> : <>
        {incomplete && <p role="status" className="text-amber-600">目录覆盖不完整, 不能据此判断不存在其它好友或同名候选</p>}
        {loading && <p role="status">正在读取好友…</p>}
        <div className="overflow-x-auto"><table className="w-full text-left"><thead><tr><th>昵称</th><th>备注</th><th>好友 ID</th><th>操作</th></tr></thead><tbody>{friends.map((friend) =>
          <tr key={friend.user_id}><td className="py-3">{friend.nickname || '未提供'}</td><td>{friend.remark || '—'}</td><td>{friend.user_id}</td><td><Button size="sm" disabled={busy || loading} onClick={() => open({ kind: 'friend', friend })}>查看详情</Button></td></tr>)}</tbody></table></div>
        {!loading && !error && friends.length === 0 && <p>没有查询到好友</p>}
      </>}
    </Card>}
    {tab === 'requests' && <Card>
      <div className="flex gap-2 mb-3">{([['active', '待处理'], ['archived', '已归档'], ['all', '全部记录']] as const).map(([value, label]) =>
        <Button key={value} disabled={busy} variant={requestView === value ? 'primary' : 'default'} onClick={() => { setRequests([]); setRequestView(value); setCursors([]); setNext(null); }}>{label}</Button>)}</div>
      <div className="flex gap-2 mb-3" aria-label="好友申请类别">{([['all', '全部类型'], ['normal', '普通申请'], ['suspicious', '可疑申请']] as const).map(([value, label]) =>
        <Button key={value} disabled={busy} variant={requestCategory === value ? 'primary' : 'default'} onClick={() => { setRequests([]); setRequestCategory(value); setRequestCoverage(''); setCursors([]); setNext(null); }}>{label}</Button>)}</div>
      <p className="mb-3 text-sm text-gray-500">普通申请来自后端接收记录, 可疑申请会向平台补取. 本地归档不代表平台申请失效; 保留凭据的归档申请仍可确认尝试处理</p>
      {requestCoverage && <p role="status" className="mb-3">可疑申请查询: {requestCoverage}</p>}
      {requestPolicy && <details className="mb-3"><summary>申请保留设置</summary><div className="flex flex-wrap gap-3 my-3">
        <label>未处理凭据保留天数<input type="number" min={1} max={3650} aria-label="申请凭据保留天数" className="glass-input block" value={requestPolicy.credential_days} onChange={(event) => setRequestPolicy({ ...requestPolicy, credential_days: Number(event.target.value) })} /></label>
        <label>历史记录保留天数<input type="number" min={requestPolicy.credential_days} max={3650} aria-label="申请历史保留天数" className="glass-input block" value={requestPolicy.history_days} onChange={(event) => setRequestPolicy({ ...requestPolicy, history_days: Number(event.target.value) })} /></label>
        <Button disabled={busy} onClick={saveRequestPolicy}>保存保留设置</Button></div>
        <p className="text-sm">历史期限不能短于凭据期限. 已清除的凭据不会因延长期限恢复, 已处理的申请会立即清除凭据</p></details>}
      {!available('list_requests') && <p>{cap('list_requests')?.reason || '当前平台不支持好友申请'}</p>}
      {unavailableCount > 0 && <p>有 {unavailableCount} 条历史申请缺少详情, 无法查询或处理. 刷新不会自动恢复, 平台重新上报未处理申请时可补回详情</p>}
      {requestUnavailableReasons.map((message) => <p key={message} role="status" className="text-amber-600">{message}</p>)}
      {loading && <p role="status">正在读取申请…</p>}
      {requests.map((request) => <div key={request.request_id} className="border-b py-3 space-y-2"><p>申请人: {request.nickname || '昵称未提供'} · ID: {request.user_id || '平台未确认'}</p><p className="whitespace-pre-wrap break-words">验证信息: {request.comment || '无'}</p>
        {request.request_category === 'suspicious' && <p>平台标记为可疑 · 原因: {request.suspicious_reason || '未提供'}</p>}
        {request.request_source && <p>申请来源: {request.request_source}</p>}
        {request.requested_at && <p>原始申请时间: {formatTime(request.requested_at)}</p>}
        <p className="text-sm">收到: {formatTime(request.received_at)} · 本地核验期限: {formatTime(request.expires_at)} · 修订号: {request.revision}</p>
        <p>平台状态: {({ pending: '待处理', processed: '已处理', invalid: '已失效', unknown: '待确认' } as Record<string, string>)[request.platform_state] || '待确认'} · 执行状态: {states[request.execution_state] || (request.execution_state === 'not_started' ? '未执行' : request.execution_state)}</p>
        <p>{request.handling_reason}</p><div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={busy || !available('handle_request') || request.can_handle === false} onClick={() => open({ kind: 'request', request, approve: true })}>同意</Button>
          <Button size="sm" disabled={busy || !available('handle_request') || request.can_handle === false} onClick={() => open({ kind: 'request', request, approve: false })}>拒绝</Button>
          <Button size="sm" disabled={busy} onClick={() => requestOperation(request, false)}>重新核验</Button>
          {request.archived && <Button size="sm" disabled={busy} onClick={() => requestOperation(request, true)}>删除历史</Button>}</div></div>)}
      {!loading && !error && available('list_requests') && requests.length === 0 && <p>当前筛选下没有可展示的申请记录</p>}
      {!available('handle_request') && <p>{cap('handle_request')?.reason || '当前平台不支持处理申请'}</p>}
    </Card>}
    {tab === 'actions' && <Card>
      <p className="mb-3">模型的删除与归档申请处理在此批准; 人工确认的操作直接执行</p>
      {loading && <p role="status">正在读取记录…</p>}
      {actions.map((action) => <div key={action.action_id} className="border-b py-3 space-y-2"><p className="font-medium">{action.action_type === 'send_request' ? '发送好友申请' : action.action_type === 'delete_friend' ? '删除好友' : action.params.approve ? '同意好友申请' : '拒绝好友申请'} · {action.actor_kind === 'model' ? '模型申请' : '人工操作'} · {states[action.state] || action.state}</p>
        <p className="text-sm">发起者: {action.actor_kind === 'panel' ? '后台登录用户' : action.actor_id || '模型工具调用者'}</p>
        <p>目标: {action.target?.nickname || ''} {action.params.user_id || action.params.request_id} {action.target?.remark ? `· 备注: ${action.target.remark}` : ''}</p><p className="text-sm break-all">动作 ID: {action.action_id} · {formatTime(action.created_at)}</p>
        {action.result && <p>{outcome(action)}</p>}
        {action.state === 'pending' && <div className="flex gap-2"><Button size="sm" disabled={busy || action.expires_at <= Date.now() / 1000} onClick={() => decide(action, true)}>{action.action_type === 'delete_friend' ? '批准删除' : '批准处理'}</Button><Button size="sm" disabled={busy} onClick={() => decide(action, false)}>拒绝申请</Button><span>有效至 {formatTime(action.expires_at)}</span></div>}
        {action.state === 'unknown' && <p>结果未确认, 请刷新好友列表核查, 不要重复提交</p>}
      </div>)}
      {!loading && !error && actions.length === 0 && <p>暂无好友操作记录</p>}
    </Card>}
    {tab === 'protection' && <Card><h2 className="font-medium mb-2">禁止删除的好友</h2><p className="text-sm mb-2">每行填写一个好友 ID. 此设置同时约束人工操作和模型申请</p>
      <textarea className="glass-input w-full" rows={6} aria-label="额外保护好友 ID" value={protectedText} onChange={(event) => setProtectedText(event.target.value)} />
      <p className="my-2">自动保护的管理者: {info?.manager_ids.join(', ') || '暂无'}</p><Button disabled={busy || !account} onClick={saveProtection}>保存保护名单</Button></Card>}
    {account && tab !== 'protection' && <div className="flex items-center gap-3"><Button disabled={loading || busy || (tab === 'actions' ? page <= 1 : cursors.length === 0)} onClick={() => { if (tab === 'actions') setPage((value) => value - 1); else setCursors((values) => values.slice(0, -1)); }}>上一页</Button>
      <span>第 {tab === 'actions' ? page : cursors.length + 1} 页</span><Button disabled={loading || busy || (tab === 'actions' ? !moreActions : !next)} onClick={() => { if (tab === 'actions') setPage((value) => value + 1); else if (next) setCursors((values) => [...values, next]); }}>下一页</Button></div>}
    <Modal open={selected !== null} onClose={() => { if (!busy) setSelected(null); }} title={selected?.kind === 'send' ? '发送好友申请' : selected?.kind === 'friend' ? '好友详情与删除' : selected?.kind === 'request' && selected.approve ? '同意好友申请' : '拒绝好友申请'}>
      {selected && <div className="space-y-3"><p>机器人账号: {account}</p>
        {selected.kind === 'friend' ? <><p>好友昵称: {selected.friend.nickname || '未提供'}</p><p>好友备注: {selected.friend.remark || '无'}</p><p>好友 ID: {selected.friend.user_id}</p>
          <Button size="sm" onClick={() => navigator.clipboard.writeText(selected.friend.user_id).then(() => toast('success', 'ID 已复制')).catch(() => toast('error', '复制失败'))}>复制 ID</Button>
          <p>删除只解除好友关系, 不拉黑, 不删除历史对话或模型上下文</p>
          {protectedIds.includes(selected.friend.user_id) && <p>此账号受保护, 不允许删除</p>}
          <p className="text-sm">{cap('delete_friend')?.reason}</p>
        </> : selected.kind === 'send' ? <>
          <label className="block">目标账号 ID<input className="glass-input w-full" aria-label="好友申请目标账号" disabled={submitted} maxLength={256} value={requestTarget} onChange={(event) => setRequestTarget(event.target.value)} /></label>
          <label className="block">验证文字 (可选)<textarea className="glass-input w-full" aria-label="好友申请验证文字" disabled={submitted} maxLength={200} value={requestMessage} onChange={(event) => setRequestMessage(event.target.value)} /></label>
          <p>提交成功表示申请已发出, 不代表对方已同意或已经成为好友</p>
        </> : <><p>申请人 ID: {selected.request.user_id}</p><p className="whitespace-pre-wrap break-words">验证信息: {selected.request.comment || '无'}</p>
          {selected.request.archived && <p role="status">此申请已在本地归档, 平台状态可能未确认. 点击确认后只尝试处理一次, 结果未知时请核查操作记录</p>}
          {selected.request.request_category === 'suspicious' && <p>平台标记为可疑 · 原因: {selected.request.suspicious_reason || '未提供'}; 此申请不支持填写好友备注</p>}
          {selected.approve && selected.request.request_category !== 'suspicious' && <label className="block">好友备注 (可选)<input className="glass-input w-full" aria-label="好友备注" maxLength={60} disabled={submitted} value={remark} onChange={(event) => setRemark(event.target.value)} /></label>}</>}
        {result && <div role="status">{outcome(result)}</div>}
        {submitted && !result && !busy && <Button onClick={() => friendApi.action(adapterId, account, operationId).then((value) => { if (scopeRef.current === scope) setResult(value); }).catch((caught) => setError(errorText(caught)))}>核查此动作</Button>}
        <div className="flex justify-end gap-2"><Button disabled={busy} onClick={() => setSelected(null)}>{submitted ? '关闭' : '取消'}</Button>
          <Button disabled={busy || submitted || (selected.kind === 'friend' ? !available('delete_friend') || protectedIds.includes(selected.friend.user_id) : selected.kind === 'send' ? !available('send_request') || !requestTarget.trim() : !available('handle_request') || selected.request.can_handle === false)} onClick={run}>
            {busy ? '正在提交…' : selected.kind === 'send' ? '确认发送' : selected.kind === 'friend' ? '确认删除' : selected.approve ? '确认同意' : '确认拒绝'}</Button></div>
      </div>}
    </Modal>
  </div>;
}
