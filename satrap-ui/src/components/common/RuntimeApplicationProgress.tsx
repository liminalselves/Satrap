import { applicationPhaseLabel, type ApplicationSnapshot } from '@/api/runtimeApplication';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';

export function RuntimeApplicationProgress({ id, label, snapshot, resultOk, busy, error, onQuery, completionText = '应用已完成' }: {
  id: string;
  label?: string;
  snapshot: ApplicationSnapshot<unknown> | null;
  resultOk?: boolean;
  busy: boolean;
  error: string;
  onQuery: () => void;
  completionText?: string;
}) {
  return <Card>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h3 className="text-base font-semibold">运行时应用进度{label ? `: ${label}` : ''}</h3>
      <Button type="button" size="sm" variant="ghost" disabled={busy || !snapshot && resultOk !== undefined} onClick={onQuery}>查询应用状态</Button>
    </div>
    <p role="status" className="mt-2 text-sm text-text-secondary">
      {error || (snapshot?.status === 'completed' || resultOk === true ? completionText
        : snapshot?.status === 'failed' || resultOk === false ? '应用失败, 请查看实际结果'
          : snapshot?.status === 'cancelled' ? '服务已取消应用, 生效状态未确认'
            : snapshot ? applicationPhaseLabel(snapshot.instances.find((item) => item.status === 'running')?.phase || snapshot.stage.phase) : '正在提交应用请求')}
    </p>
    <p className="mt-2 break-all text-xs text-text-secondary">任务 ID: {id}</p>
    {snapshot?.instances_limited && <p className="mt-2 text-sm text-text-secondary">进度明细超过显示上限, 完整结果将在结束后返回</p>}
    {!!snapshot?.instances.length && <div className="mt-3 max-h-48 space-y-2 overflow-y-auto text-sm">
      {snapshot.instances.map((instance) => <div key={`${instance.platform_id}:${instance.session_id}`}>
        <span className="font-mono">{instance.platform_id}:{instance.session_id}</span>
        <span className="ml-2 text-text-secondary">{instance.status === 'completed' ? '已同步'
          : instance.status === 'failed' ? '同步失败' : instance.status === 'cancelled' ? '已取消' : applicationPhaseLabel(instance.phase)}</span>
      </div>)}
    </div>}
  </Card>;
}
