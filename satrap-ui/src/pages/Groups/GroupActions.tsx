import { Link } from 'react-router-dom';
import { approvalLink } from '@/api/platformActions';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, ListChecks, RefreshCw } from 'lucide-react';
import { groupApi, GROUP_PAGE_SIZE, type GroupAction } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { EmptyState } from '@/components/kit/Feedback';
import { toast } from '@/components/kit/Toast';
import { useGroupContext } from './GroupLayout';
import { groupActionLabels, groupActionStateLabels } from './groupActionLabels';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';
import { VersionedConfigRead } from '@/api/configVersions';
import { getApiBaseUrl } from '@/utils/constants';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { Note, Pager } from './shared';
import { Select as KitSelect } from '@/components/kit/Select';

// 后端动作状态英文码到中文的映射, all 为前端筛选项, 未覆盖的值回退原样展示
const actionStateLabels: Record<string, string> = { all: '全部', ...groupActionStateLabels };
const stateOptions = ['pending', 'all', 'executing', 'succeeded', 'failed', 'unknown', 'rejected', 'expired'] as const;

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
  const pending = state === 'pending' ? total : undefined;
  const totalPages = Math.max(1, Math.ceil(total / GROUP_PAGE_SIZE));

  return <div className="space-y-4 animate-fade">
    {error && <Note tone="red" title="读取失败">{error}</Note>}
    <Card padding="none">
      <div className="flex flex-wrap items-center gap-3 border-b border-line p-4">
        <div>
          <h3 className="text-[14px] font-semibold text-fg">动作记录</h3>
          <p className="text-[11.5px] text-fg-3">待人工批准的操作统一在审批中心处理; 此处保留业务操作记录</p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {!!pending && <Badge tone="orange" dot>待处理 {pending}</Badge>}
          <KitSelect className="field !h-8 !w-[128px] !text-[12px]" aria-label="动作状态" value={state}
            onChange={(value) => { setState(value); setPage(1); }}>
            {stateOptions.map((value) => <option key={value} value={value}>{actionStateLabels[value] || value}</option>)}
          </KitSelect>
          <Button size="sm" variant="ghost" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={() => { void refresh(); toast.success('动作记录已刷新'); }} disabled={!isRunning}>刷新</Button>
          <span className="text-[11.5px] text-fg-4">共 {total} 条</span>
        </div>
      </div>
      {!isRunning && <div className="p-4"><Note tone="orange">后端离线, 审批记录暂不可读取。</Note></div>}
      {actions.length === 0 ? (
        <EmptyState tint="pink" icon={<ListChecks className="h-6 w-6" />} title="没有符合状态的动作" description="切换状态筛选可查看其他记录。" />
      ) : (
        <div className="divide-y divide-[var(--glass-border)]">
          {actions.map((action) => (
            <div key={action.action_id} className="p-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] font-medium text-fg">{groupActionLabels[action.action_type] || action.action_type}</span>
                <Badge tone={action.state === 'succeeded' ? 'green' : action.state === 'failed' || action.state === 'rejected' ? 'red' : action.state === 'pending' || action.state === 'unknown' || action.state === 'executing' ? 'orange' : 'neutral'} dot>
                  {actionStateLabels[action.state] || action.state}
                </Badge>
                <Badge tone={action.actor_kind === 'model' ? 'purple' : 'pink'}>{action.actor_kind === 'model' ? '模型' : '面板'}</Badge>
                <span className="num ml-auto font-mono text-[10.5px] text-fg-4">{formatTime(action.created_at)}</span>
              </div>
              <p className="num mt-1 break-all font-mono text-[10.5px] text-fg-4">ID: {action.action_id}{action.expires_at ? ` · 到期 ${formatTime(action.expires_at)}` : ''}</p>
              {Object.keys(action.params).length > 0 && <div className="mt-1.5 flex flex-wrap gap-1.5">
                {Object.entries(action.params).map(([key, value]) => (
                  <span key={key} className="rounded-md border border-line bg-glass px-1.5 py-0.5 font-mono text-[10.5px] text-fg-3">{key}={String(value)}</span>
                ))}
              </div>}
              {(action.result?.message || action.result?.reason) && (
                <p className="mt-1.5 text-[12px] text-fg-2">结果: {action.result.message || action.result.reason}</p>
              )}
              {action.state === 'pending' && !historical && isRunning && (
                <Link className="mt-1 inline-flex items-center gap-1 text-[12px] text-accent hover:underline"
                  to={approvalLink({ platform: adapterId, account, source: 'group', target: groupId, action: action.action_id })}>
                  前往审批中心 <ArrowRight className="h-3 w-3" />
                </Link>
              )}
              {action.state === 'unknown' && <p className="mt-1 text-[11.5px] text-orange">结果未确认, 请先核实平台状态, 不要重复提交。</p>}
            </div>
          ))}
        </div>
      )}
      <div className="border-t border-line px-4 py-2.5">
        <Pager page={page} totalPages={totalPages} onChange={setPage} disabled={!isRunning}
          label={<span>第 {page} 页 · 共 {total} 条 · 每页 {GROUP_PAGE_SIZE}</span>} />
      </div>
    </Card>
  </div>;
}
