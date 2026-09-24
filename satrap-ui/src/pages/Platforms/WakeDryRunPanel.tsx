import { useState } from 'react';
import { isAxiosError } from 'axios';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { controlApi } from '@/api/control';
import type { WakeDryRunDecision, WakeDryRunResult, WakePolicySource } from '@/api/control';

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

const SOURCE_LABELS: Record<WakePolicySource['source'], string> = {
  platform: '平台设置',
  time_rule: '时段规则',
  group: '群覆盖',
  builtin_default: '默认值',
};

// 每字段生效值与来源, 让"被时段/群覆盖"和"使用默认值"在界面上可区分
function SourceTable({ sources }: { sources: Record<string, WakePolicySource> }) {
  const keys = Object.keys(sources).filter((key) => key.startsWith('wake_') || key.startsWith('input_') || key === 'message_text_limit');
  if (!keys.length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-text-secondary">
          <tr>
            <th className="py-0.5 pr-2 font-normal">字段</th>
            <th className="py-0.5 pr-2 font-normal">生效值</th>
            <th className="py-0.5 font-normal">来源</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((key) => {
            const entry = sources[key];
            const value = entry.value === undefined || entry.value === null ? '未设置' : String(entry.value);
            return (
              <tr key={key} className="text-text-primary">
                <td className="py-0.5 pr-2 font-mono">{key}</td>
                <td className="py-0.5 pr-2">{value}</td>
                <td className="py-0.5 text-text-secondary">
                  {SOURCE_LABELS[entry.source]}{entry.source === 'builtin_default' ? '' : ` · ${entry.source_label}`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
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
          {automatic?.threshold && (
            <div className="flex items-start gap-2 py-0.5 text-xs" data-testid="wake-threshold-preview">
              <span className="w-24 shrink-0 text-text-secondary">有效条数阈值</span>
              <span className="text-text-primary break-all">
                {automatic.threshold.value === null ? '不触发' : automatic.threshold.value}
                {` · ${automatic.threshold.label}`}
              </span>
              {automatic.threshold.overridden && <Badge variant="warning">被显式阈值覆盖</Badge>}
              {automatic.threshold.closed && <Badge variant="default">自动参与已关闭</Badge>}
            </div>
          )}
          {automatic?.threshold?.hint && (
            <div className="text-xs text-text-secondary">{automatic.threshold.hint}</div>
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
          {result.sources && (
            <details className="pt-1 text-xs" data-testid="wake-source-table">
              <summary className="cursor-pointer text-text-secondary">策略来源明细</summary>
              <div className="pt-1">
                <SourceTable sources={result.sources} />
              </div>
            </details>
          )}
        </div>
      )}
      <p className="text-xs text-text-secondary">试算使用隔离窗口与当前草稿, 不触碰线上状态, 不调用模型</p>
    </div>
  );
}
