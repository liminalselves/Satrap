import { useEffect, useRef, useState } from 'react';
import { chatApi, type AgentRun } from '@/api/chat';

const labels: Record<string, string> = {
  running: '执行中或上次执行中断', interrupted: '已中断', failed: '执行失败',
  needs_attention: '需要确认', completed: '已完成', cancelled: '已取消',
};

export function RunRecovery({ conversation, generating }: { conversation: string; generating: boolean }) {
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [unfinished, setUnfinished] = useState(false);
  const view = useRef(0);
  useEffect(() => {
    view.current += 1;
    let current = true;
    setRuns([]);
    setCursor(null);
    setBusy(false);
    setError('');
    if (!generating) {
      chatApi.listRuns(conversation, undefined, unfinished).then(result => {
        if (current) { setRuns(result.runs); setCursor(result.next_cursor); }
      }).catch(err => { if (current) setError(String(err)); });
    }
    return () => { current = false; view.current += 1; };
  }, [conversation, generating, unfinished]);

  async function loadMore() {
    if (!cursor || busy) return;
    const version = view.current;
    setBusy(true);
    setError('');
    try {
      const result = await chatApi.listRuns(conversation, cursor, unfinished);
      if (version !== view.current) return;
      setRuns(previous => [...new Map([...previous, ...result.runs].map(run => [run.id, run])).values()]);
      setCursor(result.next_cursor);
    } catch (err) {
      if (version === view.current) setError(String(err));
    } finally {
      if (version === view.current) setBusy(false);
    }
  }

  async function operate(run: AgentRun, action: string, step?: string) {
    const version = view.current;
    setBusy(true);
    setError('');
    try {
      const result = await chatApi.manageRun(conversation, run.id, action, step);
      if (!result.ok) throw new Error(result.error || '操作失败');
      const page = await chatApi.listRuns(conversation, undefined, unfinished);
      if (version === view.current) { setRuns(page.runs); setCursor(page.next_cursor); }
    } catch (err) {
      if (version === view.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (version === view.current) setBusy(false);
    }
  }

  return <details className="border-b border-glass-border px-5 py-2 text-xs">
    <summary className="cursor-pointer text-text-secondary">执行记录与恢复</summary>
    <label className="flex gap-2 mt-2"><input type="checkbox" checked={unfinished} onChange={event => setUnfinished(event.target.checked)} />仅显示未完成任务</label>
    {error && <p role="alert" className="mt-2 text-error">{error}</p>}
    <div className="max-h-48 overflow-y-auto space-y-2 mt-2">
      {!runs.length && <p className="text-text-tertiary">暂无执行记录</p>}
      {runs.map(run => <div key={run.id} className="rounded border border-glass-border p-2 space-y-1">
        <p>{new Date(run.created_at * 1000).toLocaleString()} · {labels[run.status] || run.status}</p>
        {run.error && <p className="text-text-tertiary break-words">{run.error}</p>}
        {run.status === 'completed' && <button disabled={busy || generating} className="text-accent disabled:opacity-40"
          onClick={() => operate(run, 'resume')}>回填已完成结果</button>}
        {!['completed', 'cancelled'].includes(run.status) && <div className="flex gap-3">
          <button disabled={busy || generating} onClick={() => operate(run, 'resume')} className="text-accent disabled:opacity-40">继续执行</button>
          <button disabled={busy || generating} onClick={() => operate(run, 'abort')} className="text-text-secondary disabled:opacity-40">终止任务</button>
        </div>}
        {run.status === 'needs_attention' && run.steps.filter(step => step.kind === 'tool' && step.status === 'running' && step.recovery_policy === 'manual').map(step => <div key={step.step_key}>
          <p>步骤 {step.step_key} 可能已经产生效果, 重试可能重复操作</p>
          <button disabled={busy || generating} className="text-accent disabled:opacity-40" onClick={() => {
            if (window.confirm('此工具可能已经执行成功。确认允许再次执行？')) operate(run, 'authorize_retry', step.step_key);
          }}>确认允许重试此步骤</button>
        </div>)}
      </div>)}
      {cursor && <button disabled={busy || generating} onClick={loadMore} className="text-accent disabled:opacity-40">加载更多</button>}
    </div>
  </details>;
}
