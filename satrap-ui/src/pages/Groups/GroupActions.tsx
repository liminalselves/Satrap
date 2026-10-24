import { Link } from 'react-router-dom';
import { approvalLink } from '@/api/platformActions';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { groupApi, GROUP_PAGE_SIZE, type GroupAction } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useGroupContext } from './GroupLayout';
import { groupActionLabels, groupActionStateLabels } from './groupActionLabels';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';
import { VersionedConfigRead } from '@/api/configVersions';
import { getApiBaseUrl } from '@/utils/constants';
import { startVisiblePoll } from '@/utils/visiblePoll';

// 后端动作状态英文码到中文的映射, all 为前端筛选项, 未覆盖的值回退原样展示
const actionStateLabels: Record<string, string> = { all: '全部', ...groupActionStateLabels };

export function GroupActions() {
  const { adapterId, groupId, account, historical, isRunning } = useGroupContext();
  const [state, setState] = useState('pending');
  const [page, setPage] = useState(1);
  const [actions, setActions] = useState<GroupAction[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState('');
  const request = useRef(0);
  const actionRead = useMemo(() => new VersionedConfigRead([], (signal) =>
    groupApi.actions(adapterId, groupId, account, state, page, signal), getApiBaseUrl), [adapterId, groupId, account, state, page]);
  const currentRead = useRef(actionRead);
  currentRead.current = actionRead;
  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!isRunning || actionRead !== currentRead.current) return;
    const current = ++request.current;
    try {
      const result = await actionRead.read({ signal });
      if (current !== request.current || signal?.aborted) return;
      setActions(result.items); setTotal(result.total); setError('');
    } catch (caught) { if (current === request.current && !signal?.aborted) setError(errorText(caught)); }
  }, [actionRead, isRunning]);
  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => { request.current += 1; controller.abort(); };
  }, [refresh]);
  useEffect(() => {
    if (!isRunning || error || !['pending', 'executing', 'all'].includes(state)) return;
    const startedAt = Date.now();
    const poll = startVisiblePoll(refresh, {
      interval: state === 'executing' ? 2000 : 10000, immediate: false,
      shouldContinue: () => state !== 'executing' || Date.now() - startedAt < 60000,
    });
    return () => poll.stop();
  }, [isRunning, error, state, refresh]);
  return <div className="space-y-4">
    {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
    <Card className="space-y-3">
      <h2 className="text-lg font-semibold">动作记录</h2>
      <div className="flex flex-wrap gap-2">
        <select className="glass-input" value={state} onChange={(event) => { setState(event.target.value); setPage(1); }}>
          {['pending', 'all', 'executing', 'succeeded', 'failed', 'unknown', 'rejected', 'expired'].map((value) =>
            <option key={value} value={value}>{actionStateLabels[value] || value}</option>)}
        </select>
        <Button size="sm" variant="subtle" onClick={() => void refresh()} disabled={!isRunning}>刷新</Button>
        <span className="text-sm text-text-secondary">共 {total} 条</span>
      </div>
      {!isRunning && <p className="text-sm text-text-secondary">后端离线, 审批记录暂不可读取</p>}
      {actions.map((item) => <div key={item.action_id} className="rounded-lg border border-glass-border p-3 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div><strong>{groupActionLabels[item.action_type] || item.action_type}</strong> · {actionStateLabels[item.state] || item.state} · {item.actor_kind === 'model' ? '模型' : '面板'}</div>
          <time>{formatTime(item.created_at)}</time>
        </div>
        <p className="break-all text-text-secondary">ID: {item.action_id}</p>
        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(item.params, null, 2)}</pre>
        {(item.result?.message || item.result?.reason) && <p>结果: {item.result.message || item.result.reason}</p>}
        {item.state === 'pending' && !historical && isRunning && <div className="mt-2 flex gap-2">
          <Link className="text-accent" to={approvalLink({ platform: adapterId, account, source: 'group', target: groupId, action: item.action_id })}>前往审批中心</Link>
        </div>}
      </div>)}
      <div className="flex gap-2">
        <Button size="sm" variant="subtle" onClick={() => setPage((value) => value - 1)} disabled={page <= 1}>上一页</Button>
        <span className="text-sm">第 {page} 页</span>
        <Button size="sm" variant="subtle" onClick={() => setPage((value) => value + 1)} disabled={page * GROUP_PAGE_SIZE >= total}>下一页</Button>
      </div>
    </Card>
  </div>;
}
