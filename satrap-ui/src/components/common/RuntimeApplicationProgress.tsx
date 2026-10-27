import { CheckCircle2, Loader2, RefreshCw, Rocket, XCircle } from 'lucide-react';
import { applicationPhaseLabel, type ApplicationSnapshot } from '@/api/runtimeApplication';
import { Badge, Button, Card, CardHeader } from '@/components/kit';
import { cn } from '@/utils/cn';

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
  const running = snapshot?.instances.find((item) => item.status === 'running');
  const failed = snapshot?.status === 'failed' || resultOk === false;
  const completed = snapshot?.status === 'completed' || resultOk === true;
  const statusText = error
    || (completed ? completionText
      : failed ? '应用失败, 请查看实际结果'
        : snapshot?.status === 'cancelled' ? '服务已取消应用, 生效状态未确认'
          : snapshot ? applicationPhaseLabel(running?.phase || snapshot.stage.phase) : '正在提交应用请求');
  return (
    <Card padding="lg" tint={failed ? 'red' : completed ? 'green' : 'teal'} className="animate-rise">
      <CardHeader
        icon={<Rocket className="h-4 w-4" />}
        title={label ? `运行时应用进度: ${label}` : '运行时应用进度'}
        description={busy ? '正在协调运行实例，不会重复提交配置' : '保存与应用结果分开返回，可随时查询状态。'}
        actions={(
          <Button
            type="button"
            size="sm"
            variant="ghost"
            icon={<RefreshCw className={cn('h-3.5 w-3.5', busy && 'animate-spin-slow')} />}
            disabled={busy || !snapshot && resultOk !== undefined}
            onClick={onQuery}
          >
            查询应用状态
          </Button>
        )}
      />

      <p role="status" className="flex items-start gap-2 rounded-xl border border-line bg-glass px-3.5 py-2.5 text-[12.5px] text-fg-2">
        {busy ? <Loader2 className="mt-0.5 h-3.5 w-3.5 flex-none animate-spin-slow text-teal" />
          : failed ? <XCircle className="mt-0.5 h-3.5 w-3.5 flex-none text-red" />
            : completed ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 flex-none text-green" />
              : <Rocket className="mt-0.5 h-3.5 w-3.5 flex-none text-fg-3" />}
        <span className="min-w-0 break-words">{statusText}</span>
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Badge tone="neutral" className="font-mono">任务 ID {id}</Badge>
        {snapshot && <Badge tone={failed ? 'red' : completed ? 'green' : 'teal'} dot live={!completed && !failed}>{snapshot.status === 'running' ? '进行中' : completed ? '已完成' : failed ? '失败' : '已取消'}</Badge>}
      </div>

      {snapshot?.instances_limited && <p className="mt-2 text-[12px] text-fg-3">进度明细超过显示上限, 完整结果将在结束后返回</p>}
      {!!snapshot?.instances.length && (
        <div className="scroll mt-3 max-h-52 space-y-2">
          {snapshot.instances.map((instance) => (
            <div key={`${instance.platform_id}:${instance.session_id}`} className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-glass px-3 py-1.5 text-[12px]">
              <span className="font-mono text-fg-2">{instance.platform_id}:{instance.session_id}</span>
              <span className={cn('ml-auto',
                instance.status === 'completed' ? 'text-green' : instance.status === 'failed' ? 'text-red' : instance.status === 'cancelled' ? 'text-fg-3' : 'text-fg-2')}>
                {instance.status === 'completed' ? '已同步'
                  : instance.status === 'failed' ? '同步失败' : instance.status === 'cancelled' ? '已取消' : applicationPhaseLabel(instance.phase)}
              </span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
