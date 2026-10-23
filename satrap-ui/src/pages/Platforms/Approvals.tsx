import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { platformActionsApi, actionDescription, actionKey, actionSourceLabels, actionStateLabels, type ActionCatalog, type PlatformAction } from '@/api/platformActions';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfirm } from '@/hooks/useConfirm';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';
import { PageHeader } from '@/components/common';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { PlatformNavigation } from './PlatformNavigation';

export function Approvals() {
  const [search, setSearch] = useSearchParams();
  const scope = search.toString();
  const scopeRef = useRef(scope); scopeRef.current = scope;
  const isRunning = useBackendStore((state) => state.isRunning);
  const [catalog, setCatalog] = useState<ActionCatalog>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState('');
  const [targetDraft, setTargetDraft] = useState(search.get('target') || '');
  const [actionDraft, setActionDraft] = useState(search.get('action') || '');
  const busyRef = useRef(false);
  const request = useRef(0);
  const confirm = useConfirm(scope);
  const state = search.get('state') || 'pending';
  const platform = search.get('platform') || '';
  const reload = useCallback(async (signal?: AbortSignal) => {
    if (!isRunning) return;
    const current = ++request.current;
    setLoading(true);
    try {
      const query = new URLSearchParams(scope);
      const data = await platformActionsApi.list(Object.fromEntries(query), signal);
      if (current !== request.current || signal?.aborted || scope !== scopeRef.current) return;
      setCatalog(data); setError('');
    } catch (caught) { if (current === request.current && !signal?.aborted && scope === scopeRef.current) setError(errorText(caught)); }
    finally { if (current === request.current && !signal?.aborted) setLoading(false); }
  }, [scope, isRunning]);
  useEffect(() => {
    setCatalog(undefined); setError(''); setNotice('');
    const query = new URLSearchParams(scope);
    setTargetDraft(query.get('target') || ''); setActionDraft(query.get('action') || '');
    const controller = new AbortController();
    void reload(controller.signal);
    return () => { request.current++; controller.abort(); };
  }, [reload]);
  useEffect(() => {
    if (!isRunning || error) return;
    const started = Date.now();
    const poll = startVisiblePoll((signal) => reload(signal), {
      interval: () => state === 'executing' && Date.now() - started < 60000 ? 2000 : 10000, immediate: false,
    });
    return () => poll.stop();
  }, [state, reload, isRunning, error]);
  const choose = (key: string, value: string) => {
    const next = new URLSearchParams(search);
    value ? next.set(key, value) : next.delete(key);
    next.delete('cursor');
    if (key === 'platform') { next.delete('account'); next.delete('target'); next.delete('action'); }
    setSearch(next);
  };
  const decide = async (item: PlatformAction, approve: boolean) => {
    if (busyRef.current || !item.can_decide || !isRunning || error || (item.expires_at !== null && item.expires_at <= Date.now() / 1000)) return;
    busyRef.current = true;
    const currentScope = scope;
    try {
      const detail = item.action_type === 'delete_friend' ? '只解除好友关系, 历史对话和上下文保留'
        : ['handle_request', 'handle_group_request'].includes(item.action_type) ? '按原申请尝试处理一次, 平台结果以实际返回为准' : '';
      if (!(await confirm(`${approve ? '批准执行' : '驳回操作'}: ${actionDescription(item)}\n平台 ${item.platform_id} · 机器人 ${item.self_id}\n${item.group_id ? '目标群 ' + item.group_id : ''}\n${detail}`))) return;
      if (scopeRef.current !== currentScope) return;
      setBusy(actionKey(item)); setError(''); setNotice('');
      try {
        const result = await platformActionsApi.decide(item, approve);
        if (scopeRef.current === currentScope) setNotice(`${actionDescription(item)}: ${actionStateLabels[result.state] || result.state}`);
      } catch (caught) {
        if (scopeRef.current === currentScope) setNotice(`操作响应未确认: ${errorText(caught)}。正在按原动作读取状态, 请勿重新发起同一操作`);
      }
      try {
        const checked = await platformActionsApi.list({ platform: item.platform_id, account: item.self_id, source: item.source, action: item.action_id, state: 'all' });
        if (scopeRef.current === currentScope) {
          const action = checked.items.find((entry) => actionKey(entry) === actionKey(item));
          setNotice((previous) => `${previous}${previous ? ' · ' : ''}重新读取: ${action ? actionStateLabels[action.state] || action.state : '未找到原动作, 请在全部记录核查'}`);
        }
      } catch (caught) { if (scopeRef.current === currentScope) setNotice((previous) => `${previous} · 原动作读取失败: ${errorText(caught)}`); }
      if (scopeRef.current === currentScope) await reload();
    } finally { busyRef.current = false; setBusy(''); }
  };
  const accounts = catalog?.platforms.filter((item) => !platform || item.platform_id === platform).flatMap((item) => item.accounts) || [];
  const businessLink = (item: PlatformAction) => item.source === 'friend'
    ? `/platforms/${encodeURIComponent(item.platform_id)}/friends?account=${encodeURIComponent(item.self_id)}`
    : item.source === 'invitation' ? `/platforms/${encodeURIComponent(item.platform_id)}/groups?account=${encodeURIComponent(item.self_id)}&invites=1`
      : `/platforms/${encodeURIComponent(item.platform_id)}/groups/${encodeURIComponent(item.group_id)}/actions?account=${encodeURIComponent(item.self_id)}`;
  return <div className="space-y-5">
    <PageHeader title="审批中心" description="集中查看和批准群管理、机器人入群邀请及好友操作" />
    <PlatformNavigation pending={catalog?.pending_count} incomplete={catalog ? !catalog.complete : !isRunning} />
    <Card className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className="text-sm">平台<select aria-label="审批平台" className="glass-input mt-1 w-full" value={platform} onChange={(event) => choose('platform', event.target.value)}><option value="">全部平台</option>
          {platform && !catalog?.platforms.some((item) => item.platform_id === platform) && <option value={platform}>{platform}</option>}
          {catalog?.platforms.map((item) => <option key={item.platform_id} value={item.platform_id}>{item.platform_id}{item.online ? '' : ' (离线)'}</option>)}</select></label>
        <label className="text-sm">机器人账号<select aria-label="审批账号" className="glass-input mt-1 w-full" value={search.get('account') || 'current'} onChange={(event) => choose('account', event.target.value)}><option value="current">当前账号</option>
          {[...new Set([...accounts, ...(search.get('account') && search.get('account') !== 'current' ? [search.get('account')!] : [])])].map((account) => <option key={account} value={account}>{account}</option>)}</select></label>
        <label className="text-sm">来源<select aria-label="审批来源" className="glass-input mt-1 w-full" value={search.get('source') || 'all'} onChange={(event) => choose('source', event.target.value)}><option value="all">全部来源</option>{Object.entries(actionSourceLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label className="text-sm">状态<select aria-label="审批状态" className="glass-input mt-1 w-full" value={state} onChange={(event) => choose('state', event.target.value)}>{Object.entries({ pending: '待人工批准', executing: '执行中', all: '全部记录', ...actionStateLabels }).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label className="text-sm">目标群或用户<input aria-label="审批目标" className="glass-input mt-1 w-full" value={targetDraft} onChange={(event) => setTargetDraft(event.target.value)} /></label>
        <label className="text-sm">动作 ID<input aria-label="审批动作 ID" className="glass-input mt-1 w-full" value={actionDraft} onChange={(event) => setActionDraft(event.target.value)} /></label>
      </div>
      <div className="flex flex-wrap gap-3"><Button onClick={() => { const next = new URLSearchParams(search); targetDraft.trim() ? next.set('target', targetDraft.trim()) : next.delete('target'); actionDraft.trim() ? next.set('action', actionDraft.trim()) : next.delete('action'); next.delete('cursor'); setSearch(next); }}>应用目标筛选</Button><Button disabled={!isRunning || loading} onClick={() => void reload()}>刷新审批</Button><Button onClick={() => setSearch({})}>重置筛选</Button><span className="self-center text-sm text-text-secondary">已读取 {catalog?.total ?? '…'} 条匹配记录{catalog && !catalog.complete ? ', 部分范围未确认' : ''}</span></div>
      <p className="text-sm text-text-secondary">这里批准或驳回的是已提交的操作。原始好友申请和入群申请请进入对应业务页面处理。</p>
    </Card>
    {!isRunning && <Card role="status">后端未运行, 暂不可读取或审批</Card>}
    {error && <Card role="alert" className="text-error">{error}</Card>}
    {notice && <Card role="status">{notice}</Card>}
    {catalog?.warnings.map((warning) => <p key={warning} role="alert" className="text-warning">{warning}</p>)}
    {loading && <p role="status">正在读取审批记录…</p>}
    {isRunning && !loading && !error && catalog?.items.length === 0 && <Card>当前筛选范围没有动作记录</Card>}
    {catalog?.items.map((item) => <Card key={actionKey(item)} className="space-y-3">
      <div className="flex flex-wrap justify-between gap-2"><h2 className="font-semibold">{actionDescription(item)}</h2><span>{actionStateLabels[item.state] || item.state}</span></div>
      <p className="text-sm">平台 {item.platform_id} · 机器人 {item.self_id} · {actionSourceLabels[item.source]}{item.group_id ? ' · 群 ' + item.group_id : ''}</p>
      <p className="text-sm text-text-secondary">{item.actor_kind === 'model' ? '模型发起' : '面板发起'}{item.actor_id ? ' · ' + item.actor_id : ''} · 提交于 {formatTime(item.created_at)}{item.expires_at ? ' · 有效至 ' + formatTime(item.expires_at) : ''}</p>
      {item.result && <p className="whitespace-pre-wrap text-sm">结果: {item.result.message || item.result.reason || item.state}{item.result.verification ? ' · 核查: ' + item.result.verification : ''}</p>}
      {item.state === 'unknown' && <p className="text-warning text-sm">结果未确认, 请先核实平台状态, 不要重复提交</p>}
      <details><summary className="cursor-pointer text-sm text-text-secondary">操作详情 · {item.action_id}</summary><pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(item.params, null, 2)}</pre></details>
      <div className="flex flex-wrap items-center gap-2">
        {item.state === 'pending' && <><Button disabled={!!busy || !!error || !isRunning || !item.can_decide || !!(item.expires_at && item.expires_at <= Date.now() / 1000)} onClick={() => void decide(item, true)}>批准执行</Button><Button disabled={!!busy || !!error || !isRunning || !item.can_decide || !!(item.expires_at && item.expires_at <= Date.now() / 1000)} variant="subtle" onClick={() => void decide(item, false)}>驳回操作</Button></>}
        {item.state === 'pending' && item.can_decide && !!item.expires_at && item.expires_at <= Date.now() / 1000 && <span className="text-sm text-text-tertiary">动作已到期, 请刷新状态</span>}
        <Link className="text-sm text-accent" to={businessLink(item)}>{item.source === 'friend' ? '进入好友管理' : item.source === 'invitation' ? '进入入群邀请管理' : '查看原群动作记录'}</Link>
        {!item.can_decide && <span className="text-sm text-text-tertiary">{item.unavailable_reason}</span>}
      </div>
    </Card>)}
    <div className="flex gap-3"><Button disabled={!search.get('cursor') || loading} onClick={() => choose('cursor', '')}>回到首批</Button><Button disabled={!catalog?.has_more || loading || !isRunning} onClick={() => { const next = new URLSearchParams(search); next.set('cursor', catalog!.next_cursor!); setSearch(next); }}>下一批审批</Button></div>
  </div>;
}
