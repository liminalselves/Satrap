import { useEffect, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, ArrowUpRight, Check, CheckCircle2, Clock, Copy, Globe2, Inbox, Info, Loader2, MessageSquare,
  RefreshCw, ShieldCheck, User, Users, X, XCircle,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, IconButton, Textarea, toast } from '@/components/kit';
import { Avatar, DecisionChip, ExpiryText, JsonBlock, RiskChip, SourceChip, StateChip } from './bits';
import {
  actionDescription, actionKey, actorLabel, businessLabel, businessLink, decidedByLabel, decisionLabel, decisionLabels,
  formatDateTime,
  groupLabel, isActionable, isExpired, relativeTime, requestComment, requesterLabel, riskLabel, riskTone, sourceLabels,
  stateLabels, stateTone, type DecisionOutput, type UnifiedAction,
} from './data';

function SectionTitle({ icon, children, right }: { icon: ReactNode; children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-2 flex items-center gap-1.5">
      <span className="text-fg-3">{icon}</span>
      <h3 className="text-[12px] font-semibold uppercase tracking-[0.06em] text-fg-3">{children}</h3>
      {right && <span className="ml-auto">{right}</span>}
    </div>
  );
}

function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <dt className="flex-none text-[11.5px] text-fg-4">{label}</dt>
      <dd className="min-w-0 text-right text-[12px] text-fg-2">{children}</dd>
    </div>
  );
}

function StateBanner({ item, now, onRefresh }: { item: UnifiedAction; now: number; onRefresh: () => void }) {
  const expired = isExpired(item, now);
  if ((item.state === 'pending' || item.state === 'ready') && !expired && item.can_decide) return null;

  let tone = stateTone[item.state] ?? 'pink';
  let icon = <Info className="h-4 w-4" />;
  let title: string = stateLabels[item.state] ?? item.state;
  let body = item.unavailable_reason;
  if (expired) {
    tone = 'red';
    icon = <Clock className="h-4 w-4" />;
    title = '审批已到期';
    body = '动作已超过有效窗口，请刷新后按最新状态重新发起。';
  } else if (item.state === 'executing') {
    tone = 'accent';
    icon = <Loader2 className="h-4 w-4 animate-spin-slow" />;
    title = '正在执行';
    body = item.unavailable_reason || '已提交平台，正在等待执行结果。此页面会自动刷新。';
  } else if (item.state === 'succeeded') {
    tone = 'green';
    icon = <CheckCircle2 className="h-4 w-4" />;
    title = '平台已执行';
    body = item.result?.message || item.result?.reason || '平台已返回成功。';
  } else if (item.state === 'failed') {
    tone = 'red';
    icon = <XCircle className="h-4 w-4" />;
    title = '执行失败';
    body = item.result?.message || item.result?.reason || '平台返回失败，需要人工处理。';
  } else if (item.state === 'unknown') {
    tone = 'orange';
    icon = <AlertTriangle className="h-4 w-4" />;
    title = '结果未确认';
    body = item.result?.verification || '平台没有返回明确结果，请先在业务页核实，不要重复提交。';
  } else if (item.state === 'expired') {
    tone = 'orange';
    icon = <Clock className="h-4 w-4" />;
    title = '审批已过期';
    body = item.result?.reason || item.unavailable_reason || '超过有效窗口，动作未提交到平台。';
  } else if (item.state === 'rejected') {
    tone = 'pink';
    icon = <XCircle className="h-4 w-4" />;
    title = '审批已拒绝';
    body = item.result?.reason || item.unavailable_reason || '该动作不会提交到平台。';
  } else if (item.state === 'pending' || item.state === 'ready') {
    tone = 'orange';
    icon = <AlertTriangle className="h-4 w-4" />;
    title = '暂不可处理';
    body = item.unavailable_reason || '当前动作不可审批。';
  }

  return (
    <div className={cn('flex gap-2.5 rounded-xl border px-3.5 py-3', `tint-${tone === 'neutral' ? 'pink' : tone}`, 'border-[rgb(var(--tint)/0.28)] bg-[rgb(var(--tint)/0.08)]')}>
      <span className="mt-0.5 flex-none text-tint">{icon}</span>
      <div className="min-w-0 flex-1">
        <p className="text-[12.5px] font-semibold text-fg">{title}</p>
        <p className="mt-0.5 text-[11.5px] leading-5 text-fg-3">{body}</p>
        {item.result?.verification && item.state !== 'unknown' && (
          <p className="mt-1 text-[11px] text-fg-4">核查：{item.result.verification}</p>
        )}
      </div>
      {expired && (
        <Button size="sm" className="flex-none self-center" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={onRefresh}>刷新</Button>
      )}
    </div>
  );
}

function RequestContext({ item, now }: { item: UnifiedAction; now: number }) {
  if (!item.request && !item.requester) return null;
  const comment = requestComment(item);
  const name = item.requester?.name || item.requester?.id;
  return (
    <section className="rounded-xl border border-line bg-glass p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <MessageSquare className="h-3.5 w-3.5 text-fg-3" />
        <h3 className="text-[12.5px] font-semibold text-fg">原始请求</h3>
        {item.request?.action_type && <Badge>{item.request.action_type}</Badge>}
        {item.source_state && item.source_state !== item.state && <Badge>{item.source_state}</Badge>}
        {item.decision && <Badge tone={item.decision === 'approved' ? 'green' : 'red'}>{decisionLabel(item)}</Badge>}
      </div>
      {name && (
        <div className="mt-2.5 flex items-center gap-2.5">
          <span className="grid h-7 w-7 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-[11px] font-semibold text-tint">
            {Array.from(name)[0] ?? '?'}
          </span>
          <div className="min-w-0">
            <p className="truncate text-[12.5px] font-medium text-fg">{name}</p>
            <p className="font-mono text-[10.5px] text-fg-4">{requesterLabel(item)}</p>
          </div>
        </div>
      )}
      {comment && (
        <blockquote className="mt-2.5 rounded-lg border border-line bg-glass-hover px-3 py-2 text-[12.5px] leading-5 text-fg-2">
          “{comment}”
        </blockquote>
      )}
      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10.5px] text-fg-4">
        <span>{relativeTime(item.created_at, now)}收到</span>
        {item.group_id && <span className="font-mono">会话 {item.group_id}</span>}
      </div>
      {item.request?.params && Object.keys(item.request.params).length > 0 && (
        <div className="mt-2">
          <JsonBlock value={item.request.params} title="原始请求参数" />
        </div>
      )}
    </section>
  );
}

function Timeline({ item, now }: { item: UnifiedAction; now: number }) {
  const expired = item.state === 'expired' || isExpired(item, now);
  const rejected = item.state === 'rejected';
  const decided = !expired && !rejected && item.state !== 'pending' && item.state !== 'ready';
  const finished = item.state === 'succeeded' || item.state === 'failed' || item.state === 'unknown' || expired || rejected;
  const problem = item.state === 'failed' || item.state === 'unknown' || expired;

  const steps = [
    { label: '提交审批', detail: `${actorLabel(item)} · ${formatDateTime(item.created_at)}`, done: true, tone: 'accent' },
    {
      label: expired ? '审批到期' : rejected ? '审批驳回' : decided ? '审批通过' : '等待人工审批',
      detail: expired
        ? '超过有效窗口，未提交平台'
        : item.state === 'pending' || item.state === 'ready'
          ? '等待审批人操作'
          : item.decided_by ? `处理人 ${decidedByLabel(item)}` : '已完成审批',
      done: decided || expired || rejected,
      tone: expired || rejected ? 'red' : 'pink',
    },
    {
      label: expired || rejected ? '未提交平台' : item.state === 'failed' ? '执行失败' : item.state === 'unknown' ? '执行未确认' : '平台执行',
      detail: expired
        ? '审批到期，动作未提交'
        : rejected
          ? '审批拒绝，动作未提交'
          : item.state === 'succeeded'
            ? '平台返回成功'
            : item.state === 'executing'
              ? '正在等待平台结果'
              : item.state === 'pending' || item.state === 'ready' ? '尚未开始' : '需要人工核实',
      done: finished,
      tone: rejected ? 'red' : problem ? 'orange' : 'green',
    },
  ] as const;

  return (
    <ol className="relative space-y-3 pl-4 before:absolute before:bottom-2 before:left-[5px] before:top-2 before:w-px before:bg-[var(--glass-border-strong)]">
      {steps.map((step) => (
        <li key={step.label} className="relative flex gap-2.5">
          <span className={cn(
            'absolute -left-4 top-1 grid h-[11px] w-[11px] place-items-center rounded-full border-2',
            `tint-${step.tone}`,
            step.done ? 'border-[rgb(var(--tint)/0.7)] bg-tint shadow-[0_0_8px_rgb(var(--tint)/0.6)]' : 'border-[var(--glass-border-strong)] bg-[var(--bg-base)]',
          )} />
          <div className="min-w-0">
            <p className={cn('text-[12px] font-medium', step.done ? 'text-fg-2' : 'text-fg-4')}>{step.label}</p>
            <p className="mt-0.5 text-[10.5px] text-fg-4">{step.detail}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}

export interface DetailPanelProps {
  item: UnifiedAction | null;
  now: number;
  busy: boolean;
  output?: DecisionOutput;
  onApprove: (item: UnifiedAction, note: string) => void;
  onReject: (item: UnifiedAction, note: string) => void;
  onClose?: () => void;
  onRefresh: () => void;
}

export function DetailPanel({ item, now, busy, output, onApprove, onReject, onClose, onRefresh }: DetailPanelProps) {
  const [note, setNote] = useState('');
  const itemKey = item ? actionKey(item) : '';

  useEffect(() => { setNote(''); }, [itemKey]);

  if (!item) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6 py-12 text-center tint-pink animate-fade">
        <div className="relative mb-5">
          <div className="absolute inset-0 -m-6 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.25),transparent_70%)] blur-md" />
          <div className="relative grid h-14 w-14 place-items-center rounded-2xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.12)] text-tint">
            <Inbox className="h-6 w-6" />
          </div>
        </div>
        <h3 className="text-[15px] font-semibold text-fg">选择一条审批</h3>
        <p className="mt-1.5 max-w-[260px] text-[12.5px] leading-5 text-fg-3">
          从左侧列表选择记录，查看完整上下文、原始申请与执行结果。
        </p>
        <div className="mt-5 flex flex-wrap items-center justify-center gap-x-3 gap-y-2 text-[11px] text-fg-4">
          <span className="flex items-center gap-1.5"><kbd>J</kbd><kbd>K</kbd>移动</span>
          <span className="flex items-center gap-1.5"><kbd>A</kbd>通过</span>
          <span className="flex items-center gap-1.5"><kbd>R</kbd>拒绝</span>
          <span className="flex items-center gap-1.5"><kbd>?</kbd>快捷键</span>
        </div>
      </div>
    );
  }

  const actionable = isActionable(item, now);
  const expired = isExpired(item, now);
  const riskKey = (item.risk || 'unknown').toLowerCase();

  const copyId = () => {
    void navigator.clipboard?.writeText(item.action_id);
    toast.success('动作 ID 已复制', item.action_id);
  };

  return (
    <div className="flex h-full min-h-0 flex-col animate-fade">
      <div className={cn('relative flex-none overflow-hidden rounded-t-[inherit] px-5 pb-4 pt-5', `tint-${item.source === 'group' ? 'pink' : item.source === 'invitation' ? 'purple' : item.source === 'memory' ? 'green' : 'teal'}`)}>
        <div aria-hidden className="pointer-events-none absolute -right-10 -top-16 h-48 w-48 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.3),transparent_65%)] blur-xl" />
        <div className="relative flex items-start gap-3">
          <Avatar item={item} size="lg" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <SourceChip source={item.source} />
              <StateChip item={item} now={now} />
              <RiskChip risk={item.risk} />
              {output && <DecisionChip output={output} />}
            </div>
            <h2 className="mt-2 text-[17px] font-semibold leading-6 text-fg">{actionDescription(item)}</h2>
            <p className="mt-0.5 truncate text-[11.5px] text-fg-3">
              {sourceLabels[item.source]} · 修订 {item.revision}{item.group_id ? ` · ${groupLabel(item)}` : ''}
            </p>
          </div>
          {onClose && <IconButton label="关闭详情" shortcut="Esc" size="sm" onClick={onClose} className="-mr-1.5 -mt-1"><X className="h-4 w-4" /></IconButton>}
        </div>

        <div className="relative mt-3.5 grid grid-cols-2 gap-2">
          <div className="rounded-xl border border-line bg-glass px-3 py-2">
            <div className="text-[10.5px] text-fg-4">平台</div>
            <div className="mt-0.5 flex items-center gap-1.5 text-[12px] font-medium text-fg">
              <span className={cn('h-1.5 w-1.5 rounded-full', item.can_decide ? 'bg-green shadow-[0_0_8px_rgb(var(--rgb-green)/0.7)]' : 'bg-orange')} />
              <span className="truncate">{item.platform_id}</span>
            </div>
          </div>
          <div className="rounded-xl border border-line bg-glass px-3 py-2">
            <div className="text-[10.5px] text-fg-4">机器人账号</div>
            <div className="num mt-0.5 truncate font-mono text-[12px] font-medium text-fg">{item.self_id}</div>
          </div>
          <div className="rounded-xl border border-line bg-glass px-3 py-2">
            <div className="text-[10.5px] text-fg-4">{item.source === 'friend' ? '目标' : '目标群'}</div>
            <div className="mt-0.5 truncate text-[12px] font-medium text-fg">{item.source === 'friend' && !item.group_id ? '好友关系' : groupLabel(item)}</div>
          </div>
          <div className="rounded-xl border border-line bg-glass px-3 py-2">
            <div className="text-[10.5px] text-fg-4">有效窗口</div>
            <div className="mt-0.5 truncate text-[12px] font-medium text-fg">
              {item.expires_at === null ? '无期限' : <ExpiryText item={item} now={now} />}
            </div>
          </div>
        </div>
      </div>

      <div className="scroll min-h-0 flex-1 space-y-4 px-5 py-4">
        <StateBanner item={item} now={now} onRefresh={onRefresh} />

        {output && (
          <div className={cn('rounded-xl border px-3.5 py-2.5', `tint-${output.result === 'applied' ? 'green' : output.result === 'failed' ? 'red' : 'orange'}`, 'border-[rgb(var(--tint)/0.24)] bg-[rgb(var(--tint)/0.07)]')}>
            <p className="text-[12px] font-semibold text-fg">本次提交结果：{decisionLabels[output.result] ?? output.result}</p>
            <p className="mt-1 text-[11.5px] leading-5 text-fg-2">
              {output.result === 'applied'
                ? `决策已写入；平台结果：${output.action ? (stateLabels[output.action.state] ?? output.action.state) : '未知'}`
                : output.error?.message || '未返回可读原因'}
            </p>
            {output.error?.code && <p className="mt-0.5 font-mono text-[10.5px] text-fg-4">{output.error.code}</p>}
            {output.result === 'unknown' && <p className="mt-1 text-[11px] text-orange">结果未确认，页面已重新读取原动作，不会自动重发。</p>}
          </div>
        )}

        <section>
          <SectionTitle icon={<Globe2 className="h-3.5 w-3.5" />} right={
            <button type="button" onClick={copyId} className="inline-flex items-center gap-1 text-[10.5px] text-fg-4 transition hover:text-fg">
              <Copy className="h-3 w-3" />复制 ID
            </button>
          }>上下文</SectionTitle>
          <dl className="divide-y divide-[var(--glass-border)] rounded-xl border border-line bg-glass px-3">
            <MetaRow label="动作 ID"><span className="break-all font-mono text-[11px]">{item.action_id}</span></MetaRow>
            <MetaRow label="来源">{sourceLabels[item.source]}</MetaRow>
            <MetaRow label="状态修订"><span className="num font-mono text-[11px]">{item.revision}</span></MetaRow>
            <MetaRow label="发起者">{actorLabel(item)}</MetaRow>
            <MetaRow label="请求方">{requesterLabel(item)}</MetaRow>
            {item.group_id && (
              <MetaRow label="目标群">
                <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
                  <Users className="h-3 w-3 text-fg-4" />
                  {groupLabel(item)}
                  <span className="font-mono text-[10.5px] text-fg-4">{item.group_id}</span>
                </span>
              </MetaRow>
            )}
            <MetaRow label="提交时间">
              <span className="num font-mono text-[11px]">{formatDateTime(item.created_at)}</span>
              <span className="ml-1 text-fg-4">({relativeTime(item.created_at, now)})</span>
            </MetaRow>
            {(item.target.nickname || item.target.user_id) && (
              <MetaRow label="目标对象">
                <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
                  <User className="h-3 w-3 text-fg-4" />
                  {item.target.nickname ?? item.target.user_id}
                  {item.target.user_id && <span className="font-mono text-[10.5px] text-fg-4">{item.target.user_id}</span>}
                  {item.target.remark && <span className="text-[10.5px] text-fg-4">备注 {item.target.remark}</span>}
                </span>
              </MetaRow>
            )}
            <MetaRow label="动作类型"><span className="font-mono text-[11px]">{item.action_type}</span></MetaRow>
            <MetaRow label="原始状态"><span className="font-mono text-[11px]">{item.source_state || '—'}</span></MetaRow>
            <MetaRow label="风险">
              <span className="inline-flex flex-wrap items-center justify-end gap-1.5">
                <span className={cn('font-medium', riskTone[riskKey] && riskTone[riskKey] !== 'neutral' ? `tint-${riskTone[riskKey]} text-tint` : 'text-fg-3')}>{riskLabel[riskKey] ?? '风险未知'}</span>
                {item.risk_source ? <span className="text-[10.5px] text-fg-4">来源 {item.risk_source}</span> : null}
              </span>
            </MetaRow>
            <MetaRow label="处理人">{decidedByLabel(item)}</MetaRow>
            <MetaRow label="处理时间">{item.decided_at ? formatDateTime(item.decided_at) : '—'}</MetaRow>
            <MetaRow label="决策">{decisionLabel(item)}</MetaRow>
            {item.note && <MetaRow label="附言"><span className="whitespace-pre-wrap text-left">{item.note}</span></MetaRow>}
          </dl>
        </section>

        <RequestContext item={item} now={now} />

        <section>
          <SectionTitle icon={<ShieldCheck className="h-3.5 w-3.5" />}>参数与结果</SectionTitle>
          <div className="space-y-2">
            <JsonBlock value={item.params} title="动作参数" />
            {item.result && (
              <div className={cn('rounded-xl border px-3.5 py-3', `tint-${stateTone[item.state] === 'neutral' ? 'pink' : stateTone[item.state] ?? 'pink'}`, 'border-[rgb(var(--tint)/0.24)] bg-[rgb(var(--tint)/0.07)]')}>
                <p className="text-[12px] font-semibold text-fg">执行结果</p>
                <p className="mt-1 whitespace-pre-wrap text-[11.5px] leading-5 text-fg-2">
                  {item.result.message || item.result.reason || stateLabels[item.state] || item.state}
                </p>
                {item.result.retcode !== undefined && <p className="mt-1 font-mono text-[10.5px] text-fg-4">retcode {item.result.retcode}</p>}
                {item.result.verification && <p className="mt-1 text-[11px] text-fg-4">核查：{item.result.verification}</p>}
              </div>
            )}
          </div>
        </section>

        <section>
          <SectionTitle icon={<Clock className="h-3.5 w-3.5" />}>处理进度</SectionTitle>
          <Timeline item={item} now={now} />
        </section>

        {actionable && (
          <section className="rounded-xl border border-line bg-glass p-3">
            <label htmlFor="approval-note" className="flex items-center gap-1.5 text-[12.5px] font-medium text-fg">
              附言 <span className="text-[10.5px] font-normal text-fg-4">（可选，最多 1000 字）</span>
            </label>
            <Textarea
              id="approval-note"
              rows={2}
              maxLength={1000}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="例如：已核对群规，确认执行"
              className="mt-1.5 !text-[13px]"
            />
            <div className="mt-1 flex items-center justify-between gap-2 text-[10.5px] text-fg-4">
              <span>附言会随审批结果一并记录</span>
              <span className="num">{note.length}/1000</span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {['已核对群规', '按原申请处理', '先确认对方身份'].map((text) => (
                <button key={text} type="button" onClick={() => setNote(text)}
                  className="rounded-full border border-line px-2 py-0.5 text-[10.5px] text-fg-3 transition hover:bg-glass-hover hover:text-fg">
                  {text}
                </button>
              ))}
            </div>
          </section>
        )}

        <Link to={businessLink(item)}
          className="flex items-center gap-2 rounded-xl border border-dashed border-[var(--glass-border-strong)] px-3 py-2 text-[11.5px] text-fg-3 transition hover:bg-glass-hover hover:text-fg">
          <ArrowUpRight className="h-3.5 w-3.5" />
          前往{businessLabel(item)}
          <span className="ml-auto font-mono text-[10px] text-fg-4">业务页</span>
        </Link>
      </div>

      <div className="flex-none border-t border-line bg-[var(--glass-bg)] px-4 py-3">
        {actionable ? (
          <>
            <div className="flex gap-2">
              <Button variant="tinted" tint="red" className="flex-1" icon={<X className="h-4 w-4" />} disabled={busy} onClick={() => onReject(item, note)}>拒绝</Button>
              <Button variant="primary" className="flex-[1.4]" icon={<Check className="h-4 w-4" />} disabled={busy} onClick={() => onApprove(item, note)}>通过</Button>
            </div>
            <p className="mt-2 text-center text-[10.5px] text-fg-4">
              <kbd>A</kbd> 通过 · <kbd>R</kbd> 拒绝 · 操作前会再次确认，提交前有 5 秒撤销窗口
            </p>
          </>
        ) : expired ? (
          <div className="space-y-2">
            <p className="text-center text-[11.5px] text-orange">审批已到期，刷新后可按最新状态处理。</p>
            <Button className="w-full" icon={<RefreshCw className="h-4 w-4" />} onClick={onRefresh}>刷新状态</Button>
          </div>
        ) : (
          <div className="flex items-start gap-2 rounded-lg bg-glass px-3 py-2 text-[11.5px] leading-5 text-fg-3">
            <Info className="mt-0.5 h-3.5 w-3.5 flex-none text-fg-4" />
            {item.unavailable_reason || `当前状态：${stateLabels[item.state] ?? item.state}，不可再次审批。`}
          </div>
        )}
        {busy && <p role="status" className="mt-2 text-center text-[11px] text-accent">正在提交审批，请稍候…</p>}
      </div>
    </div>
  );
}
