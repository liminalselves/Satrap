import { useState } from 'react';
import { isAxiosError } from 'axios';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { controlApi } from '@/api/control';
import type { WakeDryRunDecision, WakeDryRunResult } from '@/api/control';

interface WakeDryRunPanelProps {
  // 当前表单草稿归一化后的完整平台策略; 构造失败时传 undefined 并禁用试算
  settings?: Record<string, unknown>;
}

function DecisionBadge({ decision }: { decision: WakeDryRunDecision }) {
  if (decision.triggered === null) return <Badge variant="warning">无法判断</Badge>;
  return decision.triggered ? <Badge variant="success">触发</Badge> : <Badge variant="default">不触发</Badge>;
}

function DecisionLine({ label, decision }: { label: string; decision: WakeDryRunDecision }) {
  return (
    <div className="flex items-start gap-2 py-0.5 text-xs">
      <span className="w-24 shrink-0 text-text-secondary">{label}</span>
      <DecisionBadge decision={decision} />
      <span className="text-text-primary break-all">
        {decision.rule}: {decision.reason}
        {decision.score !== null && decision.score !== undefined ? ` (得分 ${decision.score.toFixed(2)})` : ''}
      </span>
    </div>
  );
}

export function WakeDryRunPanel({ settings }: WakeDryRunPanelProps) {
  const [groupId, setGroupId] = useState('');
  const [localTime, setLocalTime] = useState('');
  const [samples, setSamples] = useState('');
  const [probe, setProbe] = useState('');
  const [atSelf, setAtSelf] = useState(false);
  const [submitOnce, setSubmitOnce] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<WakeDryRunResult | null>(null);

  const run = async () => {
    if (!settings) {
      toast('error', '当前草稿存在无法解析的配置, 请先修正后再试算');
      return;
    }
    setLoading(true);
    try {
      const steps: Array<{ text?: string; submit?: boolean }> = samples
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
        .map((text) => ({ text }));
      if (submitOnce && steps.length > 0) steps.push({ submit: true });
      const response = await controlApi.dryRunWake({
        settings,
        group_id: groupId.trim() || undefined,
        local_time: localTime || undefined,
        steps,
        probe: probe.trim() || atSelf ? { text: probe, at_self: atSelf } : undefined,
      });
      setResult(response);
    } catch (error) {
      setResult(null);
      toast('error', '试算失败: ' + (isAxiosError(error) && error.response?.data?.error ? String(error.response.data.error) : error instanceof Error ? error.message : '请求失败'));
    } finally {
      setLoading(false);
    }
  };

  const resolved = result?.resolved;
  const automatic = result?.automatic;

  return (
    <div className="space-y-3 rounded-sm bg-glass p-3" data-testid="wake-dry-run-panel">
      <div className="flex flex-wrap items-center gap-2">
        <input
          aria-label="试算群号"
          className="glass-input w-32 text-xs"
          value={groupId}
          placeholder="群号 (可选)"
          onChange={(event) => setGroupId(event.target.value)}
        />
        <input
          aria-label="试算本地时间"
          type="time"
          className="glass-input w-28 text-xs"
          value={localTime}
          onChange={(event) => setLocalTime(event.target.value)}
        />
        <label className="flex items-center gap-1 text-xs text-text-secondary">
          <input type="checkbox" className="h-3 w-3 accent-accent" checked={submitOnce} onChange={(event) => setSubmitOnce(event.target.checked)} />
          末尾模拟一次模型提交 (观察冷却)
        </label>
      </div>
      <textarea
        aria-label="样例消息"
        rows={3}
        className="glass-input w-full font-mono text-xs resize-none"
        value={samples}
        placeholder="样例群消息, 每行一条, 按顺序进入隔离窗口"
        onChange={(event) => setSamples(event.target.value)}
      />
      <div className="flex flex-wrap items-center gap-2">
        <input
          aria-label="探测消息"
          className="glass-input w-64 text-xs"
          value={probe}
          placeholder="探测消息 (评估显式唤醒, 可选)"
          onChange={(event) => setProbe(event.target.value)}
        />
        <label className="flex items-center gap-1 text-xs text-text-secondary">
          <input type="checkbox" className="h-3 w-3 accent-accent" checked={atSelf} onChange={(event) => setAtSelf(event.target.checked)} />
          @机器人
        </label>
        <Button type="button" variant="default" onClick={run} disabled={loading || !settings} className="ml-auto text-xs">
          {loading ? '试算中...' : '运行试算'}
        </Button>
      </div>
      {result && (
        <div className="space-y-1 border-t border-border/50 pt-2">
          {resolved && (
            <div className="flex flex-wrap gap-1 pb-1">
              {['wake_mode', 'wake_talk_value', 'wake_message_threshold', 'wake_cooldown', 'wake_score_threshold', 'wake_max_wait']
                .filter((key) => resolved[key] !== undefined)
                .map((key) => (
                  <Badge key={key} variant="info">{`${key}=${String(resolved[key])}`}</Badge>
                ))}
            </div>
          )}
          {result.explicit && <DecisionLine label="显式唤醒" decision={result.explicit} />}
          {automatic && (
            <>
              <DecisionLine label="自动参与" decision={automatic.decision} />
              <DecisionLine label="到期复查" decision={automatic.deadline_decision} />
              <div className="text-xs text-text-secondary">
                窗口有效正文 {automatic.observed} 条 · 冷却剩余 {automatic.cooldown_remaining.toFixed(0)} 秒
              </div>
            </>
          )}
        </div>
      )}
      <p className="text-xs text-text-secondary">试算使用隔离窗口与当前草稿, 不触碰线上状态, 不调用模型</p>
    </div>
  );
}
