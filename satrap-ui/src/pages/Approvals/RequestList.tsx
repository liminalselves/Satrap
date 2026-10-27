import { useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, Bot, Check, CheckCircle2, ChevronRight, Clock, Globe2, Inbox, Keyboard, Loader2,
  MessageSquare, PartyPopper, RefreshCw, Search, SlidersHorizontal, Sparkles, Users, X, XCircle,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, Card, EmptyState, IconButton, Input, Skeleton, StatusDot, Tabs } from '@/components/kit';
import { Avatar, DecisionChip, ExpiryText, KbdHint, RiskChip, SourceChip, StateChip } from './bits';
import {
  actionDescription, actionKey, actorLabel, businessLabel, businessLink, formatHms, formatLatency, groupLabel,
  isActionable, isExpired, relativeTime, requestComment, type ApprovalPlatform, type DecisionOutput, type FilterScope,
  type StateFilter, type UnifiedAction,
} from './data';

export type SortMode = 'newest' | 'urgent';

const stateTitle: Record<StateFilter, string> = {
  pending: '待处理', ready: '准备执行', executing: '执行中', succeeded: '已通过', rejected: '已拒绝',
  expired: '已过期', failed: '执行失败', unknown: '结果未确认', all: '全部记录',
};

function RequestCard({ item, index, selected, active, now, online, output, onOpen, onToggleSelect, onApprove, onReject }: {
  item: UnifiedAction;
  index: number;
  selected: boolean;
  active: boolean;
  now: number;
  online: boolean;
  output?: DecisionOutput;
  onOpen: (item: UnifiedAction) => void;
  onToggleSelect: (item: UnifiedAction) => void;
  onApprove: (item: UnifiedAction) => void;
  onReject: (item: UnifiedAction) => void;
}) {
  const actionable = isActionable(item, now);
  const expired = isExpired(item, now);
  const key = actionKey(item);
  const comment = requestComment(item);

  return (
    <div
      data-approval-key={key}
      onClick={(event) => {
        if ((event.target as HTMLElement).closest('button, input, label, a')) return;
        onOpen(item);
      }}
      className={cn(
        'tile group relative flex items-start gap-3 !rounded-2xl p-3.5 pr-3',
        `tint-${item.source === 'group' ? 'pink' : item.source === 'invitation' ? 'purple' : item.source === 'memory' ? 'green' : 'teal'}`,
        active && 'ring-1 ring-[rgb(var(--tint)/0.45)]',
      )}
      style={{ ['--i' as string]: index }}
    >
      {active && <span aria-hidden className="absolute bottom-4 left-0 top-4 w-[3px] rounded-r-full bg-tint" />}

      <div className="pt-0.5">
        <label
          title={actionable ? '选择此项' : item.unavailable_reason || '当前不可处理'}
          className={cn(
            'relative grid h-4 w-4 place-items-center rounded-[5px] border transition-colors',
            selected
              ? 'border-[rgb(var(--tint)/0.8)] bg-[rgb(var(--tint)/0.85)] text-white'
              : 'border-[var(--glass-border-strong)] bg-glass hover:border-[rgb(var(--tint)/0.55)]',
            !actionable && 'opacity-35',
          )}
        >
          <input
            type="checkbox"
            className="sr-only"
            checked={selected}
            disabled={!actionable}
            onChange={() => onToggleSelect(item)}
            aria-label={`选择 ${actionDescription(item)}`}
          />
          {selected && <Check className="h-3 w-3" strokeWidth={3} />}
        </label>
      </div>

      <button
        type="button"
        onClick={() => onOpen(item)}
        aria-current={active ? 'true' : undefined}
        className="flex min-w-0 flex-1 items-start gap-3 text-left"
      >
        <span className="relative flex-none">
          <Avatar item={item} />
          <span className="absolute -bottom-0.5 -right-0.5 grid h-4 w-4 place-items-center rounded-full border-2 border-[var(--bg-base)] bg-[var(--bg-base)]">
            <StatusDot tone={online ? 'green' : 'orange'} className="!h-2 !w-2" />
          </span>
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-1.5">
            <SourceChip source={item.source} />
            <StateChip item={item} now={now} />
            <RiskChip risk={item.risk} />
            {output && <DecisionChip output={output} />}
          </span>
          <span className="mt-1.5 block truncate text-[14px] font-medium leading-5 text-fg">{actionDescription(item)}</span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11.5px] text-fg-3">
            <span className="inline-flex items-center gap-1"><Globe2 className="h-3 w-3" />{item.platform_id}</span>
            <span className="text-fg-4">·</span>
            <span className="inline-flex items-center gap-1"><Bot className="h-3 w-3" />{item.self_id}</span>
            {item.group_id && (
              <>
                <span className="text-fg-4">·</span>
                <span className="inline-flex items-center gap-1"><Users className="h-3 w-3" />{groupLabel(item)}</span>
              </>
            )}
          </span>
          {comment && (
            <span className="mt-1.5 flex items-start gap-1.5 rounded-lg bg-glass px-2 py-1 text-[11.5px] leading-4 text-fg-3">
              <MessageSquare className="mt-0.5 h-3 w-3 flex-none" />
              <span className="line-clamp-1">{comment}</span>
            </span>
          )}
          <span className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-fg-4">
            <span className="truncate">{actorLabel(item)}</span>
            <span>·</span>
            <span>{relativeTime(item.created_at, now)}</span>
            {item.expires_at !== null && (
              <>
                <span>·</span>
                <ExpiryText item={item} now={now} />
              </>
            )}
          </span>
          {!item.can_decide && (item.state === 'pending' || item.state === 'ready') && !expired && item.unavailable_reason && (
            <span className="mt-1 flex items-start gap-1 text-[11px] leading-4 text-orange">
              <AlertTriangle className="mt-0.5 h-3 w-3 flex-none" />{item.unavailable_reason}
            </span>
          )}
          {expired && (
            <span className="mt-1 flex items-start gap-1 text-[11px] leading-4 text-red">
              <Clock className="mt-0.5 h-3 w-3 flex-none" />动作已到期，刷新后按最新状态处理
            </span>
          )}
          {item.state !== 'pending' && item.state !== 'ready' && item.state !== 'executing' && (item.result?.message || item.result?.reason) && (
            <span className="mt-1 line-clamp-1 text-[11px] leading-4 text-fg-3">{item.result.message || item.result.reason}</span>
          )}
          {item.state === 'unknown' && (
            <span className="mt-1 flex items-start gap-1 text-[11px] leading-4 text-orange">
              <AlertTriangle className="mt-0.5 h-3 w-3 flex-none" />结果未确认，请勿重复提交；页面会重新读取原动作
            </span>
          )}
          {item.note && (
            <span className="mt-1 line-clamp-1 text-[11px] leading-4 text-fg-3">附言：{item.note}</span>
          )}
        </span>
      </button>

      <div className="flex flex-none flex-col items-end gap-2 pl-1">
        <span className="num hidden font-mono text-[10.5px] text-fg-4 sm:block">{item.action_id.slice(-8)}</span>
        {actionable ? (
          <span className="hidden items-center gap-0.5 sm:flex sm:opacity-0 sm:transition-opacity sm:group-hover:opacity-100 sm:group-focus-within:opacity-100">
            <IconButton label="通过" shortcut="A" size="sm" onClick={() => onApprove(item)} className="!text-green hover:!bg-[rgb(var(--rgb-green)/0.12)]">
              <Check className="h-3.5 w-3.5" />
            </IconButton>
            <IconButton label="拒绝" shortcut="R" size="sm" onClick={() => onReject(item)} className="!text-red hover:!bg-[rgb(var(--rgb-red)/0.12)]">
              <X className="h-3.5 w-3.5" />
            </IconButton>
          </span>
        ) : (
          <ChevronRight className="h-4 w-4 text-fg-4 transition-transform duration-300 group-hover:translate-x-0.5 group-hover:text-tint" />
        )}
      </div>
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="stagger space-y-2" aria-busy="true" aria-label="正在读取审批记录">
      {[0, 1, 2, 3, 4].map((index) => (
        <div key={index} className="surface !rounded-2xl p-4" style={{ ['--i' as string]: index }}>
          <div className="flex gap-3">
            <Skeleton className="h-10 w-10 flex-none rounded-xl" />
            <div className="min-w-0 flex-1 space-y-2">
              <div className="flex gap-2"><Skeleton className="h-5 w-16 rounded-full" /><Skeleton className="h-5 w-20 rounded-full" /></div>
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-3 w-1/2" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

const celebrationDots = [
  { left: '12%', top: '26%', size: 6, tint: 'pink', delay: 0 },
  { left: '22%', top: '64%', size: 4, tint: 'purple', delay: 240 },
  { left: '78%', top: '24%', size: 5, tint: 'teal', delay: 420 },
  { left: '86%', top: '58%', size: 4, tint: 'pink', delay: 120 },
  { left: '66%', top: '78%', size: 6, tint: 'orange', delay: 520 },
  { left: '32%', top: '14%', size: 4, tint: 'green', delay: 360 },
];

function AllClear({ filtered, todayDecided, latency, onSetState, onResetFilters }: {
  filtered: boolean; todayDecided: number | null; latency: number | null;
  onSetState: (state: StateFilter) => void; onResetFilters: () => void;
}) {
  return (
    <Card padding="none" tint="pink" className="relative overflow-hidden animate-rise">
      <div aria-hidden className="pointer-events-none absolute -left-16 -top-24 h-56 w-56 rounded-full bg-[radial-gradient(circle,rgb(var(--rgb-pink)/0.28),transparent_70%)] blur-2xl" />
      <div aria-hidden className="pointer-events-none absolute -bottom-24 -right-12 h-56 w-56 rounded-full bg-[radial-gradient(circle,rgb(var(--rgb-purple)/0.22),transparent_70%)] blur-2xl" />
      {celebrationDots.map((dot, index) => (
        <span
          key={index}
          aria-hidden
          className={cn('pointer-events-none absolute rounded-full animate-pop', `tint-${dot.tint}`, 'bg-tint shadow-[0_0_12px_rgb(var(--tint)/0.9)]')}
          style={{ left: dot.left, top: dot.top, width: dot.size, height: dot.size, animationDelay: `${dot.delay}ms` }}
        />
      ))}
      <div className="relative">
        <EmptyState
          tint="pink"
          icon={<PartyPopper className="h-7 w-7" />}
          title={filtered ? '当前范围已处理完' : '全部处理完'}
          description={
            filtered
              ? '这个筛选范围里没有待处理审批了，可以重置筛选继续处理其他审批。'
              : `待处理队列已清空，今天共决定 ${todayDecided ?? '未知'} 项审批。新的动作提交后会实时出现在这里。`
          }
          actions={
            <>
              <Button variant="tinted" tint="green" onClick={() => onSetState('succeeded')}>查看已通过</Button>
              {filtered
                ? <Button onClick={onResetFilters}>查看全部待处理</Button>
                : <Button onClick={() => onSetState('all')}>查看全部记录</Button>}
            </>
          }
        />
        <div className="mx-auto -mt-4 mb-6 flex w-fit items-center gap-4 rounded-full border border-line bg-glass px-4 py-2 text-[11px] text-fg-3">
          <span className="flex items-center gap-1.5"><Sparkles className="h-3.5 w-3.5 text-pink" />今日已决定 <span className="num font-medium text-fg-2">{todayDecided ?? '未知'}</span></span>
          <span className="h-3 w-px bg-[var(--glass-border-strong)]" />
          <span className="flex items-center gap-1.5"><Clock className="h-3.5 w-3.5 text-fg-3" />平均决策 {formatLatency(latency)}</span>
        </div>
      </div>
    </Card>
  );
}

function EmptyForState({ state, filtered, query, onClearQuery, onResetFilters }: {
  state: StateFilter; filtered: boolean; query: string; onClearQuery: () => void; onResetFilters: () => void;
}) {
  if (query) {
    return (
      <Card padding="none" className="animate-rise">
        <EmptyState tint="pink" icon={<Search className="h-6 w-6" />} title={`没有匹配 “${query}” 的审批`}
          description="换一个关键字，或清除搜索条件后再试"
          actions={<Button onClick={onClearQuery}>清除搜索</Button>} />
      </Card>
    );
  }
  if (filtered) {
    return (
      <Card padding="none" className="animate-rise">
        <EmptyState tint="pink" icon={<SlidersHorizontal className="h-6 w-6" />} title="当前筛选范围没有动作记录"
          description="可以放宽平台、来源或状态条件，查看其他审批"
          actions={<Button onClick={onResetFilters}>重置筛选</Button>} />
      </Card>
    );
  }
  const icon = state === 'succeeded' ? <CheckCircle2 className="h-6 w-6" /> : state === 'rejected' ? <XCircle className="h-6 w-6" /> : state === 'executing' ? <Loader2 className="h-6 w-6" /> : <Inbox className="h-6 w-6" />;
  const title = state === 'all' ? '没有任何审批记录' : `没有${stateTitle[state]}记录`;
  return (
    <Card padding="none" className="animate-rise">
      <EmptyState tint="pink" icon={icon} title={title}
        description="切换状态筛选，或等待新的审批动作提交" />
    </Card>
  );
}

export interface RequestListProps {
  items: UnifiedAction[];
  filteredCount: number;
  loading: boolean;
  now: number;
  isRunning: boolean;
  scope: FilterScope;
  query: string;
  onQuery: (value: string) => void;
  sort: SortMode;
  onSort: (value: SortMode) => void;
  selected: Set<string>;
  onToggleSelect: (item: UnifiedAction) => void;
  onSelectAll: (checked: boolean) => void;
  activeKey: string | null;
  onOpen: (item: UnifiedAction) => void;
  onApprove: (item: UnifiedAction) => void;
  onReject: (item: UnifiedAction) => void;
  cursorActive: boolean;
  hasMore: boolean;
  onNext: () => void;
  onFirst: () => void;
  onOpenFilters: () => void;
  onRefresh: () => void;
  syncing: boolean;
  polling: boolean;
  onTogglePolling: () => void;
  pollInterval: number;
  lastSync: number;
  onOpenShortcuts: () => void;
  todayDecided: number | null;
  latencyMs: number | null;
  onResetFilters: () => void;
  onSetState: (state: StateFilter) => void;
  platforms: ApprovalPlatform[];
  decisions: Record<string, DecisionOutput>;
}

export function RequestList(props: RequestListProps) {
  const {
    items, filteredCount, loading, now, isRunning, scope, query, onQuery, sort, onSort, selected, onToggleSelect, onSelectAll,
    activeKey, onOpen, onApprove, onReject, cursorActive, hasMore, onNext, onFirst, onOpenFilters, onRefresh, syncing,
    polling, onTogglePolling, pollInterval, lastSync, onOpenShortcuts, todayDecided, latencyMs, onResetFilters,
    onSetState, platforms, decisions,
  } = props;

  const listRef = useRef<HTMLDivElement>(null);
  const selectAllRef = useRef<HTMLInputElement>(null);
  const actionable = items.filter((item) => isActionable(item, now));
  const actionableKeys = actionable.map(actionKey);
  const selectedVisible = actionableKeys.filter((key) => selected.has(key)).length;
  const filtersActive = !!scope.platform || scope.source !== 'all' || !!scope.target || !!scope.action || scope.account !== 'current' || !!scope.q;

  useEffect(() => {
    if (!activeKey) return;
    const element = Array.from(listRef.current?.querySelectorAll<HTMLElement>('[data-approval-key]') ?? [])
      .find((node) => node.dataset.approvalKey === activeKey);
    element?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [activeKey]);

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = selectedVisible > 0 && selectedVisible < actionableKeys.length;
    }
  }, [selectedVisible, actionableKeys.length]);

  return (
    <section className="min-w-0 space-y-3" aria-label="审批列表">
      <div className="surface tint-pink !rounded-2xl p-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <div className="flex items-baseline gap-2">
            <h2 className="text-[15px] font-semibold text-fg">{stateTitle[scope.state]}</h2>
            <span className="num text-xs text-fg-3">{loading ? '读取中…' : `${filteredCount} 条`}</span>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <button type="button" onClick={onTogglePolling} aria-pressed={polling}
              title={polling ? '暂停自动刷新' : '恢复自动刷新'}
              className="rounded-full transition-transform hover:scale-[1.03] active:scale-95">
              <Badge tone={polling ? 'green' : 'orange'} dot live={polling}>
                {polling ? `自动刷新 ${pollInterval}s` : '自动刷新已暂停'}
              </Badge>
            </button>
            <span className="num hidden font-mono text-[10.5px] text-fg-4 sm:inline">同步 {formatHms(lastSync)}</span>
            <IconButton label="键盘快捷键" shortcut="?" size="sm" onClick={onOpenShortcuts}><Keyboard className="h-4 w-4" /></IconButton>
          </div>
        </div>

        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <div className="min-w-[150px] flex-1">
            <Input
              id="approval-search"
              leading={<Search className="h-4 w-4" />}
              placeholder="搜索描述、群、用户或动作 ID…"
              aria-label="搜索审批"
              value={query}
              onChange={(event) => onQuery(event.target.value)}
              className="!h-8 !text-[13px]"
            />
          </div>
          <Tabs
            size="sm"
            label="审批排序"
            value={sort}
            onChange={onSort}
            items={[{ value: 'newest', label: '最新提交' }, { value: 'urgent', label: '即将过期' }]}
          />
          <IconButton label="打开筛选" size="sm" className="lg:hidden" onClick={onOpenFilters}><SlidersHorizontal className="h-4 w-4" /></IconButton>
          <Button size="sm" icon={<RefreshCw className={cn('h-3.5 w-3.5', syncing && 'animate-spin-slow')} />}
            disabled={!isRunning || loading || syncing} onClick={onRefresh}>刷新</Button>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-fg-4">
          <label className={cn('flex items-center gap-2', actionableKeys.length === 0 && 'opacity-50')}>
            <input
              ref={selectAllRef}
              type="checkbox"
              className="h-3.5 w-3.5 accent-[rgb(var(--rgb-pink))]"
              checked={actionableKeys.length > 0 && selectedVisible === actionableKeys.length}
              disabled={actionableKeys.length === 0}
              onChange={(event) => onSelectAll(event.target.checked)}
            />
            <span>{selectedVisible > 0 ? `已选 ${selected.size} 项` : `全选可处理 (${actionableKeys.length})`}</span>
          </label>
          <span className="hidden h-3 w-px bg-[var(--glass-border-strong)] sm:block" />
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <KbdHint keys="J K">移动</KbdHint>
            <KbdHint keys="A">通过</KbdHint>
            <KbdHint keys="R">拒绝</KbdHint>
            <KbdHint keys="X">选择</KbdHint>
            <KbdHint keys="/">搜索</KbdHint>
          </span>
          <span className="ml-auto hidden items-center gap-1.5 sm:flex">
            <StatusDot tone={isRunning ? 'green' : 'orange'} live={isRunning} />
            {isRunning ? '审批通道已连接' : '后端未连接'}
          </span>
        </div>
      </div>

      {loading ? (
        <ListSkeleton />
      ) : items.length === 0 ? (
        query ? (
          <EmptyForState state={scope.state} filtered={filtersActive} query={query} onClearQuery={() => onQuery('')} onResetFilters={onResetFilters} />
        ) : scope.state === 'pending' || scope.state === 'ready' ? (
          <AllClear filtered={filtersActive} todayDecided={todayDecided} latency={latencyMs} onSetState={onSetState} onResetFilters={onResetFilters} />
        ) : (
          <EmptyForState state={scope.state} filtered={filtersActive} query={query} onClearQuery={() => onQuery('')} onResetFilters={onResetFilters} />
        )
      ) : (
        <div ref={listRef} key={`${scope.state}-${sort}-${scope.q}`} className="stagger space-y-2">
          {items.map((item, index) => {
            const platform = platforms.find((entry) => entry.platform_id === item.platform_id);
            const key = actionKey(item);
            return (
              <RequestCard
                key={key}
                item={item}
                index={index}
                selected={selected.has(key)}
                active={activeKey === key}
                now={now}
                online={platform?.online ?? true}
                output={decisions[key]}
                onOpen={onOpen}
                onToggleSelect={onToggleSelect}
                onApprove={onApprove}
                onReject={onReject}
              />
            );
          })}
        </div>
      )}

      {!loading && items.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 pt-1 text-[11px] text-fg-4">
          <span>已显示 <span className="num text-fg-3">{items.length}</span> / <span className="num text-fg-3">{filteredCount}</span> 条</span>
          <span className="num font-mono">{cursorActive ? '后续批次' : '第 1 批'}</span>
          <div className="ml-auto flex gap-2">
            <Button size="sm" disabled={!cursorActive || syncing} onClick={onFirst}>回到首批</Button>
            <Button size="sm" variant="tinted" tint="pink" disabled={!hasMore || syncing} onClick={onNext}>下一批审批</Button>
          </div>
        </div>
      )}

      {items.length > 0 && (
        <p className="text-[11px] text-fg-4">
          批量审批每批最多 20 项；{businessLabel(items[0])}等原始申请仍在对应业务页处理。
          <Link className="ml-1 text-tint hover:underline" to={businessLink(items[0])}>前往业务页</Link>
        </p>
      )}
    </section>
  );
}
