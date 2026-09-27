import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupAction, type GroupPolicyValue } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { useGroupContext } from './GroupLayout';

const labels: Record<string, string> = {
  recall_message: '撤回消息', kick_group_member: '移出成员', ban_group_member: '禁言成员',
  set_group_whole_ban: '全员禁言', ban_anonymous: '禁言匿名成员', set_group_admin: '设置管理员',
  set_group_anonymous: '匿名聊天', set_group_card: '设置名片', set_group_name: '修改群名',
  set_group_special_title: '设置头衔', leave_group: '退出或解散群', handle_group_request: '处理加群请求',
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '操作失败';
}

export function GroupActions() {
  const { adapterId, groupId, account, historical, isRunning, config, setConfig, reload } = useGroupContext();
  const approval = config.explicit.approval;
  const initial = useCallback(() => Object.fromEntries(Object.entries(approval || {}).map(([key, item]) =>
    [key, item.mode === 'value' ? String(item.value) : 'inherit'])), [approval]);
  const [draft, setDraft] = useState<Record<string, string>>(initial);
  const [baseline, setBaseline] = useState(JSON.stringify(initial()));
  const [state, setState] = useState('pending');
  const [page, setPage] = useState(1);
  const [actions, setActions] = useState<GroupAction[]>([]);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const request = useRef(0);
  const dirty = JSON.stringify(draft) !== baseline;
  useDirtyGuard(dirty);
  useEffect(() => {
    if (dirty) return;
    const fresh = initial();
    setDraft(fresh);
    setBaseline(JSON.stringify(fresh));
  }, [config.revision, config.account, dirty, initial]);
  const refresh = useCallback(async () => {
    if (!isRunning) return;
    const current = ++request.current;
    try {
      const result = await groupApi.actions(adapterId, groupId, account, state, page);
      if (current !== request.current) return;
      setActions(result.items); setTotal(result.total); setError('');
    } catch (caught) { if (current === request.current) setError(errorText(caught)); }
  }, [adapterId, groupId, account, isRunning, state, page]);
  useEffect(() => {
    void refresh();
    return () => { request.current += 1; };
  }, [refresh]);
  useEffect(() => {
    if (!isRunning || error || !['pending', 'executing', 'all'].includes(state)) return;
    const startedAt = Date.now();
    const interval = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      if (state === 'executing' && Date.now() - startedAt >= 60_000) {
        window.clearInterval(interval);
        return;
      }
      void refresh();
    }, state === 'executing' ? 2_000 : 10_000);
    return () => window.clearInterval(interval);
  }, [isRunning, error, state, refresh]);
  const save = async () => {
    if (busy || historical) return;
    const values: Record<string, GroupPolicyValue> = {};
    for (const [key, mode] of Object.entries(draft)) {
      if (mode !== 'inherit') values[key] = { mode: 'value', value: mode };
    }
    setBusy('save'); setError('');
    try {
      const saved = await groupApi.saveConfig(adapterId, groupId, {
        expected_self_id: account, expected_revision: config.revision,
        base_revision: config.base_revision, section: 'approval', values,
      }, isRunning);
      setConfig(saved);
      const fresh = Object.fromEntries(Object.entries(saved.explicit.approval || {}).map(([key, item]) =>
        [key, item.mode === 'value' ? String(item.value) : 'inherit']));
      setDraft(fresh); setBaseline(JSON.stringify(fresh)); setConflict(false);
    } catch (caught) {
      setError(errorText(caught));
      if (caught instanceof ApiError && caught.status === 409) setConflict(true);
    } finally { setBusy(''); }
  };
  const decide = async (id: string, approve: boolean) => {
    if (busy || historical || !isRunning) return;
    setBusy(id); setError('');
    try {
      await groupApi.decideAction(adapterId, groupId, id, account, approve);
      await refresh();
    } catch (caught) { setError(errorText(caught)); await refresh(); }
    finally { setBusy(''); }
  };
  return <div className="space-y-4">
    {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
    {conflict && <Card role="alert" className="border border-warning">审批设置已变化, 草稿仍保留。
      <Button size="sm" onClick={async () => { await reload(); setConflict(false); }}>查看最新值</Button>
    </Card>}
    <Card className="space-y-4">
      <h2 className="text-lg font-semibold">逐群审批设置</h2>
      <p className="text-sm text-text-secondary">群覆盖优先于账号默认。高影响动作默认需要批准。自动执行仍受原有授权和平台能力限制</p>
      <div className="grid gap-3 md:grid-cols-2">
        {config.capabilities.approval_actions.map((action) => <label key={action} className="block rounded-lg border border-glass-border p-3 text-sm">
          <span className="font-medium">{labels[action] || action}</span>
          <span className="ml-2 text-text-secondary">当前: {config.effective.approval[action] === 'approval_required' ? '需审批' : '自动执行'} · {config.sources.approval[action]}</span>
          <select className="glass-input mt-2 w-full" value={draft[action] || 'inherit'} disabled={historical || !!busy}
            onChange={(event) => setDraft((previous) => ({ ...previous, [action]: event.target.value }))}>
            <option value="inherit">继承账号默认</option>
            <option value="approval_required">需人工审批</option>
            <option value="auto_execute">自动执行</option>
          </select>
        </label>)}
      </div>
      <Button onClick={save} disabled={historical || !!busy || !dirty}>保存审批设置</Button>
    </Card>
    <Card className="space-y-3">
      <h2 className="text-lg font-semibold">审批与动作记录</h2>
      <div className="flex flex-wrap gap-2">
        <select className="glass-input" value={state} onChange={(event) => { setState(event.target.value); setPage(1); }}>
          {['pending', 'all', 'executing', 'succeeded', 'failed', 'unknown', 'rejected', 'expired'].map((value) =>
            <option key={value} value={value}>{value}</option>)}
        </select>
        <Button size="sm" variant="subtle" onClick={refresh} disabled={!isRunning}>刷新</Button>
        <span className="text-sm text-text-secondary">共 {total} 条</span>
      </div>
      {!isRunning && <p className="text-sm text-text-secondary">后端离线, 审批记录暂不可读取</p>}
      {actions.map((item) => <div key={item.action_id} className="rounded-lg border border-glass-border p-3 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div><strong>{labels[item.action_type] || item.action_type}</strong> · {item.state} · {item.actor_kind === 'model' ? '模型' : '面板'}</div>
          <time>{new Date(item.created_at * 1000).toLocaleString()}</time>
        </div>
        <p className="break-all text-text-secondary">ID: {item.action_id}</p>
        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(item.params, null, 2)}</pre>
        {item.result?.reason && <p>结果: {item.result.reason}</p>}
        {item.state === 'pending' && !historical && isRunning && <div className="mt-2 flex gap-2">
          <Button size="sm" onClick={() => decide(item.action_id, true)} disabled={!!busy}>批准</Button>
          <Button size="sm" variant="subtle" onClick={() => decide(item.action_id, false)} disabled={!!busy}>拒绝</Button>
        </div>}
      </div>)}
      <div className="flex gap-2">
        <Button size="sm" variant="subtle" onClick={() => setPage((value) => value - 1)} disabled={page <= 1}>上一页</Button>
        <span className="text-sm">第 {page} 页</span>
        <Button size="sm" variant="subtle" onClick={() => setPage((value) => value + 1)} disabled={page * 25 >= total}>下一页</Button>
      </div>
    </Card>
  </div>;
}
