import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupAction } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useGroupContext } from './GroupLayout';
import { groupActionLabels } from './groupActionLabels';

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '操作失败';
}

export function GroupActions() {
  const { adapterId, groupId, account, historical, isRunning } = useGroupContext();
  const [state, setState] = useState('pending');
  const [page, setPage] = useState(1);
  const [actions, setActions] = useState<GroupAction[]>([]);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const request = useRef(0);
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
          <div><strong>{groupActionLabels[item.action_type] || item.action_type}</strong> · {item.state} · {item.actor_kind === 'model' ? '模型' : '面板'}</div>
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
