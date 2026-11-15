import { useEffect, useState } from 'react';
import axios from 'axios';
import { Activity, Archive, Gauge, Loader2, Puzzle, Timer } from 'lucide-react';
import { edictumApi } from '@/api/edictum';
import type { FlowRuntimeStatus } from '@/api/types';
import { Badge } from '@/components/kit';
import { cn } from '@/utils/cn';
import { errorText } from '@/utils/errorText';
import { startVisiblePoll } from '@/utils/visiblePoll';

/** 读取结果: 未运行与读取失败分开显示 */
type RuntimeView =
  | { state: 'loading' }
  | { state: 'ready'; data: FlowRuntimeStatus }
  | { state: 'stopped'; message: string }
  | { state: 'error'; message: string };

const time = (seconds: number) => seconds ? new Date(seconds * 1000).toLocaleString() : '—';

function Block({ icon, title, children, right }: { icon: React.ReactNode; title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="space-y-2 rounded-2xl border border-line bg-glass p-3.5">
      <div className="flex items-center gap-2">
        <span className="text-fg-3">{icon}</span>
        <h4 className="text-[12.5px] font-semibold text-fg">{title}</h4>
        {right && <span className="ml-auto">{right}</span>}
      </div>
      {children}
    </section>
  );
}

/**
 * 流程会话运行状态: 页面可见时每 5 秒读取一次
 *
 * 只读; 状态槽不返回内容, 这里也不显示内容
 */
export function FlowRuntimePanel({ platformId, sessionId, interval = 5000 }: { platformId: string; sessionId: string; interval?: number }) {
  const [view, setView] = useState<RuntimeView>({ state: 'loading' });
  useEffect(() => {
    setView({ state: 'loading' });
    const poll = startVisiblePoll(async (signal) => {
      try {
        const data = await edictumApi.getFlowRuntime(platformId, sessionId, signal);
        if (!signal.aborted) setView({ state: 'ready', data });
      } catch (error) {
        if (signal.aborted) return;
        if (axios.isAxiosError(error) && error.response?.status === 404) {
          setView({ state: 'stopped', message: errorText(error) });
          return;
        }
        setView({ state: 'error', message: errorText(error, '读取运行状态失败') });
      }
    }, { interval });
    return () => poll.stop();
  }, [interval, platformId, sessionId]);

  if (view.state === 'loading') return <p className="flex items-center gap-2 text-[12.5px] text-fg-3"><Loader2 className="h-3.5 w-3.5 animate-spin-slow" />正在读取流程运行状态…</p>;
  if (view.state === 'stopped') {
    return (
      <div role="status" className="rounded-xl border border-dashed border-[var(--glass-border-strong)] px-3.5 py-3 text-[12.5px] text-fg-3">
        会话未运行。会话激活后这里会显示阈值积压、状态槽版本和最近事件。<span className="block text-[11px] text-fg-4">{view.message}</span>
      </div>
    );
  }
  if (view.state === 'error') return <p role="alert" className="text-[12.5px] text-red">{view.message}</p>;

  const { data } = view;
  const tiles = Object.entries(data.tiles);
  const states = Object.entries(data.states);
  const nodes = Object.entries(data.node_status);
  return (
    <div className="space-y-3" aria-label="流程运行状态">
      <div className="flex flex-wrap items-center gap-2 text-[11.5px] text-fg-3">
        <span className="font-mono text-fg-2">{data.flow_name}</span>
        <span>档案起点 {time(data.history_start)}</span>
        {data.running_wfs.length > 0
          ? <Badge tone="purple" dot>运行中 {data.running_wfs.join(', ')}</Badge>
          : <Badge tone="neutral">空闲</Badge>}
      </div>

      {data.budget && (
        <Block icon={<Gauge className="h-3.5 w-3.5" />} title="预算" right={<span className="num font-mono text-[12px] text-fg-2">{data.budget.used} / {data.budget.max_calls}</span>}>
          <div className="h-1.5 overflow-hidden rounded-full bg-glass-hover">
            <div className={cn('h-full rounded-full', data.budget.used >= data.budget.max_calls ? 'bg-red' : data.budget.used / data.budget.max_calls > 0.8 ? 'bg-orange' : 'bg-green')}
              style={{ width: `${Math.min(100, (data.budget.used / Math.max(1, data.budget.max_calls)) * 100)}%` }} />
          </div>
          <p className="text-[11px] text-fg-4">最近 {data.budget.window_minutes} 分钟 · {data.budget.count === 'wf' ? '只算 WF' : 'WF 和插件节点'}</p>
        </Block>
      )}

      <Block icon={<Timer className="h-3.5 w-3.5" />} title="阈值磁贴">
        {tiles.length === 0 ? <p className="text-[12px] text-fg-3">没有阈值磁贴</p> : tiles.map(([id, tile]) => (
          <div key={id} className="space-y-1">
            <div className="text-[12px] text-fg-2">{tile.title || id} <span className="font-mono text-[10.5px] text-fg-4">{id}</span></div>
            {Object.entries(tile.wfs).map(([wf, item]) => (
              <div key={wf} className="flex items-center gap-2 pl-3 text-[11.5px] text-fg-3">
                <span className="font-mono">{wf}</span>
                <span>已提交到 <span className="num text-fg-2">{item.committed}</span></span>
                <span className={cn(item.backlog > 0 && 'text-orange')}>积压 <span className="num">{item.backlog}</span> 条</span>
              </div>
            ))}
          </div>
        ))}
      </Block>

      <Block icon={<Archive className="h-3.5 w-3.5" />} title="状态槽">
        {states.length === 0 ? <p className="text-[12px] text-fg-3">没有状态槽</p> : (
          <table className="w-full text-left text-[11.5px]">
            <thead className="text-fg-4"><tr><th className="font-normal">槽</th><th className="font-normal">版本</th><th className="font-normal">覆盖到</th><th className="font-normal">提交时间</th><th className="font-normal">run</th></tr></thead>
            <tbody className="text-fg-2">
              {states.map(([id, item]) => (
                <tr key={id}>
                  <td className="font-mono">{id}</td>
                  <td className="num">v{item.version}</td>
                  <td className="num">{item.covers_to}</td>
                  <td>{time(item.committed_at)}</td>
                  <td className="max-w-[120px] truncate font-mono text-[10.5px] text-fg-4" title={item.run_id}>{item.run_id || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Block>

      {nodes.length > 0 && (
        <Block icon={<Puzzle className="h-3.5 w-3.5" />} title="插件节点">
          {nodes.map(([id, item]) => (
            <div key={id} className="flex items-start gap-2 text-[11.5px]">
              <span className="font-mono text-fg-2">{id}</span>
              {item.ready ? <Badge tone="green">就绪</Badge> : <span className="text-red">{item.error || '未就绪'}</span>}
            </div>
          ))}
        </Block>
      )}

      <Block icon={<Activity className="h-3.5 w-3.5" />} title="最近事件">
        {data.events_source === 'memory_fallback' && <p className="text-[11px] text-orange">诊断未开启, 仅显示进程内记录</p>}
        {data.events.length === 0 ? <p className="text-[12px] text-fg-3">暂无事件</p> : (
          <ul className="space-y-1">
            {[...data.events].sort((a, b) => b.recorded_at - a.recorded_at).slice(0, 30).map((event, index) => (
              <li key={`${event.event_id}-${index}`} className="flex flex-wrap items-center gap-2 text-[11.5px]">
                <span className="num w-[140px] flex-none text-fg-4">{time(event.recorded_at)}</span>
                <Badge tone={event.status === 'error' || event.phase === 'blocked' ? 'red' : 'neutral'}>{event.phase}</Badge>
                <span className="text-fg-3">{event.status}</span>
                {typeof event.metadata.node === 'string' && <span className="font-mono text-fg-2">{event.metadata.node}</span>}
                {typeof event.metadata.reason === 'string' && <span className="text-fg-3">{event.metadata.reason}</span>}
              </li>
            ))}
          </ul>
        )}
      </Block>
    </div>
  );
}
