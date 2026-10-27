import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, ArrowUpRight, CheckCircle2, CircleSlash, Clock, Globe2, Inbox, ListFilter, Loader2, RotateCcw,
  SlidersHorizontal, Wrench, XCircle,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, Field, Input, Select, StatusDot, type SelectOption } from '@/components/kit';
import { sourceIcons } from './bits';
import {
  ALL_SOURCES, formatLatency, sourceLabels, sourceTint, type FacetCounts, type FilterScope, type StateFilter,
  type ApprovalPlatform,
} from './data';

const stateRows: { value: StateFilter; label: string; icon: typeof Inbox }[] = [
  { value: 'pending', label: '待处理', icon: Inbox },
  { value: 'executing', label: '执行中', icon: Loader2 },
  { value: 'succeeded', label: '已通过', icon: CheckCircle2 },
  { value: 'rejected', label: '已拒绝', icon: XCircle },
  { value: 'expired', label: '已过期', icon: Clock },
  { value: 'all', label: '全部记录', icon: ListFilter },
];

const minorStateRows: { value: StateFilter; label: string; icon: typeof Inbox }[] = [
  { value: 'ready', label: '准备执行', icon: Loader2 },
  { value: 'unknown', label: '结果未确认', icon: AlertTriangle },
  { value: 'failed', label: '执行失败', icon: CircleSlash },
];

function RailHeading({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between px-2.5 pb-1 pt-2">
      <span className="text-[10.5px] font-semibold uppercase tracking-[0.09em] text-fg-4">{children}</span>
      {action}
    </div>
  );
}

function RailRow({ active, label, icon, count, loading, onClick, meta }: {
  active: boolean; label: React.ReactNode; icon: React.ReactNode; count: number | null | undefined; loading?: boolean;
  onClick: () => void; meta?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        'group relative flex w-full items-center gap-2.5 rounded-[10px] px-2.5 py-1.5 text-left text-[13px] transition-colors',
        active ? 'bg-[rgb(var(--tint)/0.14)] font-medium text-tint' : 'text-fg-2 hover:bg-glass-hover hover:text-fg',
      )}
    >
      {active && <span aria-hidden className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-tint" />}
      <span className={cn('grid h-5 w-5 flex-none place-items-center rounded-md', active ? 'bg-[rgb(var(--tint)/0.16)]' : 'text-fg-3')}>{icon}</span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {meta}
      <span className={cn('num rounded-full px-1.5 text-[11px] leading-[18px]', active ? 'bg-[rgb(var(--tint)/0.2)] text-tint' : 'bg-glass-hover text-fg-3')}>
        {loading || count === null || count === undefined ? '—' : count}
      </span>
    </button>
  );
}

export interface FilterRailProps {
  scope: FilterScope;
  counts: FacetCounts;
  platforms: ApprovalPlatform[];
  accounts: string[];
  loading: boolean;
  complete: boolean;
  totalRead: number;
  summaryReady: boolean;
  onPatch: (patch: Partial<FilterScope>) => void;
  onReset: () => void;
  onApplyText: (target: string, action: string) => void;
  onDone?: () => void;
}

export function FilterRail({
  scope, counts, platforms, accounts, loading, complete, totalRead, summaryReady,
  onPatch, onReset, onApplyText, onDone,
}: FilterRailProps) {
  const [targetDraft, setTargetDraft] = useState(scope.target);
  const [actionDraft, setActionDraft] = useState(scope.action);

  useEffect(() => setTargetDraft(scope.target), [scope.target]);
  useEffect(() => setActionDraft(scope.action), [scope.action]);

  const apply = () => onApplyText(targetDraft.trim(), actionDraft.trim());
  const allPlatformsCount = Object.values(counts.platforms).some((value) => value === null)
    ? null
    : Object.values(counts.platforms).reduce<number>((sum, value) => sum + (value ?? 0), 0);
  const accountOptions: SelectOption[] = [
    { value: 'current', label: '当前账号', description: '各平台默认机器人' },
    ...accounts.map((account) => ({ value: account, label: account })),
  ];
  if (scope.account !== 'current' && !accounts.includes(scope.account)) {
    accountOptions.push({ value: scope.account, label: scope.account });
  }

  return (
    <div className="flex h-full min-h-0 flex-col tint-pink">
      <div className="flex items-center justify-between gap-2 px-4 pb-1 pt-3.5">
        <div className="flex items-center gap-2">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-[rgb(var(--tint)/0.16)] text-tint"><SlidersHorizontal className="h-3.5 w-3.5" /></span>
          <span className="text-[13px] font-semibold text-fg">收件箱</span>
        </div>
        <Badge tone="pink" dot live={!loading}>{loading ? '读取中' : `${counts.states.pending ?? '—'} 待处理`}</Badge>
      </div>

      <div className="scroll fade-edges min-h-0 flex-1 px-2 py-2">
        <RailHeading>状态</RailHeading>
        <nav aria-label="审批状态筛选" className="space-y-0.5">
          {stateRows.map((row) => (
            <RailRow
              key={row.value}
              active={scope.state === row.value}
              label={row.label}
              icon={<row.icon className={cn('h-3.5 w-3.5', row.value === 'executing' && !loading && 'animate-spin-slow')} />}
              count={row.value === 'all' ? counts.states.all : counts.states[row.value]}
              loading={loading}
              onClick={() => onPatch({ state: row.value })}
            />
          ))}
        </nav>
        <div className="mt-0.5 space-y-0.5 border-t border-line/60 pt-1">
          {minorStateRows.map((row) => (
            <RailRow
              key={row.value}
              active={scope.state === row.value}
              label={row.label}
              icon={<row.icon className="h-3.5 w-3.5" />}
              count={counts.states[row.value]}
              loading={loading}
              onClick={() => onPatch({ state: row.value })}
            />
          ))}
        </div>

        <div className="my-2 h-px bg-[var(--glass-border)]" />

        <RailHeading>按类型</RailHeading>
        <nav aria-label="审批类型筛选" className="space-y-0.5">
          <RailRow
            active={scope.source === 'all'}
            label="全部来源"
            icon={<ListFilter className="h-3.5 w-3.5" />}
            count={ALL_SOURCES.reduce<number>((sum, source) => sum + (counts.sources[source] ?? 0), 0)}
            loading={loading}
            onClick={() => onPatch({ source: 'all' })}
          />
          {ALL_SOURCES.map((source) => {
            const Icon = sourceIcons[source];
            return (
              <RailRow
                key={source}
                active={scope.source === source}
                label={sourceLabels[source]}
                icon={<Icon className={cn('h-3.5 w-3.5', `tint-${sourceTint[source]}`)} />}
                count={counts.sources[source]}
                loading={loading}
                onClick={() => onPatch({ source })}
              />
            );
          })}
        </nav>

        <div className="my-2 h-px bg-[var(--glass-border)]" />

        <RailHeading>按平台 · 待处理</RailHeading>
        <nav aria-label="审批平台筛选" className="space-y-0.5">
          <RailRow
            active={scope.platform === ''}
            label="全部平台"
            icon={<Globe2 className="h-3.5 w-3.5" />}
            count={allPlatformsCount}
            loading={loading}
            onClick={() => onPatch({ platform: '', account: 'current', target: '', action: '' })}
          />
          {platforms.map((platform) => (
            <RailRow
              key={platform.platform_id}
              active={scope.platform === platform.platform_id}
              label={
                <span className="block min-w-0">
                  <span className="block truncate">{platform.platform_id}</span>
                  <span className="block truncate font-mono text-[10px] text-fg-4">
                    {platform.current_account ? `当前 ${platform.current_account}` : '当前账号未确认'}
                  </span>
                </span>
              }
              icon={<StatusDot tone={platform.online ? 'green' : 'orange'} live={platform.online} className="!h-2 !w-2" />}
              count={platform.pending_count}
              loading={loading}
              onClick={() => onPatch({ platform: platform.platform_id, account: 'current', target: '', action: '' })}
              meta={!platform.online ? <span className="rounded-full bg-[rgb(var(--rgb-orange)/0.14)] px-1.5 text-[10px] leading-[16px] text-orange">离线</span> : undefined}
            />
          ))}
          {!loading && platforms.length === 0 && <p className="px-2.5 py-2 text-[11px] text-fg-4">没有已登记的平台</p>}
        </nav>

        <div className="my-2 h-px bg-[var(--glass-border)]" />

        <RailHeading>范围与精确筛选</RailHeading>
        <div className="space-y-2.5 px-1 pt-1">
          <Field label="机器人账号" hint="当前账号 = 各平台默认机器人">
            <Select
              aria-label="审批账号"
              value={scope.account}
              onChange={(value) => onPatch({ account: value })}
              options={accountOptions}
            />
          </Field>
          <Field label="目标群或用户">
            <Input
              value={targetDraft}
              onChange={(event) => setTargetDraft(event.target.value)}
              onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); apply(); } }}
              placeholder="群号、用户 ID 或昵称"
              aria-label="审批目标"
            />
          </Field>
          <Field label="动作 ID">
            <Input
              value={actionDraft}
              onChange={(event) => setActionDraft(event.target.value)}
              onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); apply(); } }}
              placeholder="例如 act-882301-ban-01"
              aria-label="审批动作 ID"
              className="font-mono !text-[12px]"
            />
          </Field>
          <div className="flex gap-2 pt-0.5">
            <Button size="sm" variant="tinted" tint="pink" className="flex-1" onClick={apply}>应用筛选</Button>
            <Button size="sm" variant="ghost" icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={onReset} aria-label="重置全部筛选">重置</Button>
          </div>
          <div className="rounded-xl border border-dashed border-[var(--glass-border-strong)] p-2.5">
            <div className="flex items-center gap-1.5 text-[10.5px] font-medium text-fg-2">
              <Wrench className="h-3 w-3 text-fg-3" />工具调用审批
            </div>
            <p className="mt-1 text-[10.5px] leading-4 text-fg-4">
              在对话内按「执行审批模式」处理，不进入平台审批队列。原始好友与入群申请仍在对应业务页处理。
            </p>
            <Link to="/chat" className="mt-1 inline-flex items-center gap-0.5 text-[10.5px] text-tint transition hover:underline">
              打开对话<ArrowUpRight className="h-3 w-3" />
            </Link>
          </div>
        </div>
      </div>

      <div className="flex-none space-y-1.5 border-t border-line px-3.5 py-2.5">
        <div className="flex items-center gap-2 text-[11px] text-fg-3">
          <StatusDot tone={complete ? 'green' : 'orange'} />
          已读取 <span className="num font-medium text-fg-2">{loading ? '…' : totalRead}</span> 条匹配记录
        </div>
        <div className={cn('flex items-center gap-1.5 text-[11px]', complete ? 'text-fg-4' : 'text-orange')}>
          {complete ? '全部平台范围已确认' : '部分平台范围未确认, 统计可能不完整'}
        </div>
        {!summaryReady && <div className="text-[11px] text-orange">汇总暂不可用, 数量可能未确认</div>}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-fg-4">
          <span>今日已决定 <span className="num font-medium text-fg-2">{counts.todayDecided ?? '未知'}</span></span>
          <span className="h-2.5 w-px bg-[var(--glass-border-strong)]" />
          <span>平均决策 <span className="num font-medium text-fg-2">{formatLatency(counts.latencyMs)}</span></span>
        </div>
        {onDone && <Button size="sm" variant="primary" className="mt-1.5 w-full" onClick={onDone}>查看结果</Button>}
      </div>
    </div>
  );
}
