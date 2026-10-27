import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { AlertTriangle, ArrowLeft, Info, RefreshCw, WifiOff, X } from 'lucide-react';
import { cn } from '@/utils/cn';
import { approvalsApi, sourceStateLabels, type ApprovalCatalog, type ApprovalQuery, type DecisionInput, type DecisionOutput } from '@/api/contracts';
import { ApiError } from '@/api/client';
import { useBackendStore } from '@/stores/useBackendStore';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { errorText } from '@/utils/errorText';
import { Badge, Button, Card, Drawer, IconButton, PageHeader, toast } from '@/components/kit';
import { FilterRail } from './FilterRail';
import { RequestList, type SortMode } from './RequestList';
import { DetailPanel } from './DetailPanel';
import { BatchBar, DecisionModal, ShortcutsModal, UndoBar } from './Dialogs';
import {
  actionDescription, actionKey, ALL_STATES, coverageTone, isActionable, keyId, serviceLabels,
  type ApprovalSource, type FacetCounts, type FilterScope, type StateFilter, type UnifiedAction,
} from './data';

const PAGE_SIZE = 20;
const MAX_BATCH = 20;
const UNDO_MS = 5000;
const WIDE_QUERY = '(min-width: 1280px)';
const VALID_STATES: StateFilter[] = [...ALL_STATES, 'all'];
const VALID_SOURCES: FilterScope['source'][] = ['all', 'group', 'invitation', 'friend', 'memory'];

interface ScheduledDecision {
  seq: number;
  timer: number;
  items: UnifiedAction[];
  snapshots: UnifiedAction[];
  approve: boolean;
  note: string;
}

interface UndoEntry {
  seq: number;
  keys: string[];
  snapshots: UnifiedAction[];
  title: string;
  description: string;
}

function useWideDetail() {
  const [wide, setWide] = useState(() => typeof window !== 'undefined' && window.matchMedia(WIDE_QUERY).matches);
  useEffect(() => {
    const media = window.matchMedia(WIDE_QUERY);
    const sync = () => setWide(media.matches);
    sync();
    media.addEventListener('change', sync);
    return () => media.removeEventListener('change', sync);
  }, []);
  return wide;
}

function decisionKey(input: { platform_id: string; self_id: string; source: ApprovalSource; action_id: string }): string {
  return keyId(input);
}

let decisionSeq = 0;

export function Approvals() {
  const navigate = useNavigate();
  const wide = useWideDetail();
  const [search, setSearch] = useSearchParams();
  const isRunning = useBackendStore((state) => state.isRunning);
  const runState = useBackendStore((state) => state.runState);

  const platform = search.get('platform') || '';
  const account = search.get('account') || 'current';
  const sourceParam = search.get('source') || '';
  const stateParam = search.get('state') || '';
  const target = search.get('target') || '';
  const action = search.get('action') || '';
  const q = search.get('q') || '';
  const cursor = search.get('cursor') || '';
  const sortParam = search.get('sort');
  const orderParam = search.get('order');
  const source = VALID_SOURCES.includes(sourceParam as FilterScope['source']) ? sourceParam as FilterScope['source'] : 'all';
  const state = VALID_STATES.includes(stateParam as StateFilter) ? stateParam as StateFilter : (action ? 'all' : 'pending');
  const sortParamFinal: 'created' | 'expires' = sortParam === 'expires' ? 'expires' : 'created';
  const order: 'asc' | 'desc' = orderParam === 'asc' || orderParam === 'desc' ? orderParam : (sortParamFinal === 'expires' ? 'asc' : 'desc');
  const scope: FilterScope = { platform, account, source, state, target, action, q, sort: sortParamFinal, order };
  const scopeSignature = `${platform}|${account}|${source}|${state}|${target}|${action}|${q}|${sortParamFinal}|${order}|${cursor}`;
  const filterSignature = `${platform}|${account}|${source}|${state}|${target}|${action}|${q}`;

  const [catalog, setCatalog] = useState<ApprovalCatalog | null>(null);
  const [summary, setSummary] = useState<ApprovalCatalog | null>(null);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ items: UnifiedAction[]; approve: boolean; note: string; scope: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [undo, setUndo] = useState<UndoEntry | null>(null);
  const [decisions, setDecisions] = useState<Record<string, DecisionOutput>>({});
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [polling, setPolling] = useState(true);
  const [lastSync, setLastSync] = useState(Date.now());
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  const [queryDraft, setQueryDraft] = useState(q);

  const request = useRef(0);
  const busyRef = useRef(false);
  const mountedRef = useRef(true);
  const scheduledRef = useRef<ScheduledDecision | null>(null);
  const reloadRef = useRef<((signal?: AbortSignal, mode?: 'load' | 'refresh' | 'quiet') => Promise<void>) | null>(null);
  const queryTimer = useRef<number | null>(null);
  const scopeRef = useRef(scopeSignature);
  scopeRef.current = scopeSignature;

  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; }; }, []);
  useEffect(() => { setQueryDraft(q); }, [q]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Math.floor(Date.now() / 1000)), 15000);
    return () => window.clearInterval(timer);
  }, []);

  const reload = useCallback(async (signal?: AbortSignal, mode: 'load' | 'refresh' | 'quiet' = 'load') => {
    if (!isRunning) return;
    const current = ++request.current;
    if (mode === 'load') setLoading(true);
    if (mode === 'refresh') setSyncing(true);
    const query: ApprovalQuery = {
      platform_id: platform || undefined,
      account,
      source: source === 'all' ? undefined : source,
      state,
      target: target || undefined,
      action_id: action || undefined,
      q: q.trim() || undefined,
      sort: sortParamFinal,
      order,
      limit: PAGE_SIZE,
      cursor: cursor || undefined,
    };
    try {
      const [list, total] = await Promise.allSettled([
        approvalsApi.list(query, signal),
        approvalsApi.summary({
          platform_id: platform || undefined, account, source: source === 'all' ? undefined : source,
        }, signal),
      ]);
      if (current !== request.current || signal?.aborted || !mountedRef.current) return;
      if (list.status === 'rejected') throw list.reason;
      setCatalog(list.value);
      if (total.status === 'fulfilled') setSummary(total.value);
      setError('');
      setLastSync(Date.now());
    } catch (caught) {
      if (current === request.current && !signal?.aborted && mountedRef.current) setError(errorText(caught));
    } finally {
      if (current === request.current && !signal?.aborted && mountedRef.current) {
        if (mode === 'load') setLoading(false);
        if (mode === 'refresh') setSyncing(false);
      }
    }
  }, [isRunning, platform, account, source, state, target, action, q, sortParamFinal, order, cursor]);
  reloadRef.current = reload;

  /* 筛选、分页或后端状态变化: 重新读取整页, 并清理选择与瞬时提示 */
  useEffect(() => {
    setCatalog(null);
    setSummary(null);
    setError('');
    setNotice('');
    setDecisions({});
    setSelected(new Set());
    setActiveKey(null);
    setConfirm(null);
    if (!isRunning) { setLoading(false); return; }
    const controller = new AbortController();
    void reload(controller.signal, 'load');
    return () => { request.current += 1; controller.abort(); };
  }, [reload, isRunning]);

  /* 与生产一致: 仅“执行中”筛选在开始轮询后的 60 秒内使用 2 秒间隔, 其余 10 秒 */
  const pollStartedRef = useRef(Date.now());
  useEffect(() => { pollStartedRef.current = Date.now(); }, [state, polling, isRunning]);
  const pollInterval = state === 'executing' && Date.now() - pollStartedRef.current < 60000 ? 2 : 10;
  useEffect(() => {
    if (!polling || !isRunning) return;
    const poll = startVisiblePoll((signal) => reload(signal, 'quiet'), {
      interval: () => (state === 'executing' && Date.now() - pollStartedRef.current < 60000 ? 2000 : 10000),
      immediate: false,
    });
    return () => poll.stop();
  }, [polling, isRunning, reload, state]);

  /* 撤销只在前端尚未提交的 5 秒内成立: 离开页面时立即提交已确认的批次 */
  useEffect(() => () => {
    const scheduled = scheduledRef.current;
    if (!scheduled) return;
    scheduledRef.current = null;
    window.clearTimeout(scheduled.timer);
    const payload: DecisionInput[] = scheduled.items.map((item) => ({
      key: { platform_id: item.platform_id, self_id: item.self_id, source: item.source, action_id: item.action_id },
      expected_revision: item.revision,
      approve: scheduled.approve,
      ...(scheduled.note ? { note: scheduled.note } : {}),
    }));
    void approvalsApi.decide(payload).catch(() => undefined);
  }, []);

  const items = catalog?.items ?? [];
  const pageItems = items;
  const activeItem = activeKey ? items.find((item) => actionKey(item) === activeKey) ?? null : null;
  const selectedItems = items.filter((item) => selected.has(actionKey(item)));
  const selectedActionable = selectedItems.filter((item) => isActionable(item, now)).length;

  /* ---------- 汇总与分面 ---------- */
  const byState = summary?.by_state ?? catalog?.by_state ?? {};
  const bySource = summary?.by_source ?? catalog?.by_source ?? {};
  const pendingCount = summary?.pending_count ?? catalog?.pending_count ?? (byState.pending ?? null);
  const stateAll = Object.values(byState).length
    ? Object.values(byState).reduce<number>((sum, value) => sum + (value ?? 0), 0)
    : (catalog?.total ?? null);
  const sourceTotal = Object.values(bySource).length
    ? Object.values(bySource).reduce<number>((sum, value) => sum + (value ?? 0), 0)
    : null;
  const platforms = catalog?.platforms ?? [];
  const counts: FacetCounts = {
    states: {
      ...byState,
      pending: byState.pending ?? pendingCount,
      all: stateAll,
    },
    sources: {
      ...bySource,
      ...(source === 'all' && sourceTotal !== null ? { all: sourceTotal } : {}),
    },
    platforms: Object.fromEntries(platforms.map((entry) => [entry.platform_id, entry.pending_count])),
    pendingTotal: byState.pending ?? pendingCount,
    todayDecided: summary?.today_decided ?? catalog?.today_decided ?? null,
    latencyMs: summary?.decision_latency_ms ?? catalog?.decision_latency_ms ?? null,
  };
  const complete = summary?.complete ?? catalog?.complete ?? false;
  const warnings = (summary?.warnings?.length ? summary.warnings : catalog?.warnings) ?? [];
  const coverage = summary?.sources ?? catalog?.sources ?? [];
  const accounts = [...new Set(platforms
    .filter((entry) => !platform || entry.platform_id === platform)
    .flatMap((entry) => entry.accounts))];
  const focusedFilter = !!platform || source !== 'all' || !!target || !!action || account !== 'current' || !!q.trim();
  const sortMode: SortMode = sortParamFinal === 'expires' ? 'urgent' : 'newest';
  const hasMore = catalog?.has_more ?? false;
  const latency = counts.latencyMs;

  /* ---------- URL 筛选 ---------- */
  const patchScope = (patch: Partial<FilterScope>) => {
    const merged: FilterScope = patch.platform !== undefined
      ? { ...scope, ...patch, account: 'current', target: '', action: '' }
      : { ...scope, ...patch };
    const next = new URLSearchParams(search);
    const write = (key: string, value: string, fallback: string) => {
      if (value && value !== fallback) next.set(key, value);
      else next.delete(key);
    };
    write('platform', merged.platform, '');
    write('account', merged.account, 'current');
    write('source', merged.source, 'all');
    const defaultState = merged.action ? 'all' : 'pending';
    write('state', merged.state, defaultState);
    write('target', merged.target, '');
    write('action', merged.action, '');
    write('q', merged.q.trim(), '');
    if (merged.sort === 'expires') {
      next.set('sort', 'expires');
      next.set('order', merged.order === 'desc' ? 'desc' : 'asc');
    } else {
      next.delete('sort');
      next.delete('order');
    }
    next.delete('cursor');
    setSearch(next, { replace: true });
    setSelected(new Set());
    setActiveKey(null);
    setConfirm(null);
  };
  const resetFilters = () => {
    setSearch({}, { replace: true });
    setQueryDraft('');
    setSelected(new Set());
    setActiveKey(null);
    setNotice('');
  };
  const setSort = (value: SortMode) => {
    const next = new URLSearchParams(search);
    next.delete('cursor');
    if (value === 'urgent') { next.set('sort', 'expires'); next.set('order', 'asc'); }
    else { next.delete('sort'); next.delete('order'); }
    setSearch(next, { replace: true });
    setSelected(new Set());
  };
  const commitQuery = (value: string) => {
    if (value.trim() === q) return;
    patchScope({ q: value });
  };
  const updateQuery = (value: string) => {
    setQueryDraft(value);
    if (queryTimer.current) window.clearTimeout(queryTimer.current);
    queryTimer.current = window.setTimeout(() => {
      queryTimer.current = null;
      if (mountedRef.current) commitQuery(value);
    }, 350);
    if (cursor) {
      const next = new URLSearchParams(search);
      next.delete('cursor');
      setSearch(next, { replace: true });
    }
  };
  useEffect(() => () => { if (queryTimer.current) window.clearTimeout(queryTimer.current); }, []);

  const nextPage = () => {
    if (!catalog?.next_cursor) return;
    const next = new URLSearchParams(search);
    next.set('cursor', catalog.next_cursor);
    setSearch(next);
  };
  const firstPage = () => {
    const next = new URLSearchParams(search);
    next.delete('cursor');
    setSearch(next);
  };

  /* ---------- 选择 ---------- */
  const toggleSelect = useCallback((item: UnifiedAction) => {
    if (!isActionable(item, now)) return;
    const key = actionKey(item);
    if (!selected.has(key) && selected.size >= MAX_BATCH) {
      toast.warning('每批最多 20 项审批', '请先提交或取消部分选择');
      return;
    }
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, [now, selected]);
  const selectAll = (checked: boolean) => {
    const keys = pageItems.filter((item) => isActionable(item, now)).map(actionKey);
    if (checked) {
      const room = Math.max(0, MAX_BATCH - selected.size);
      if (keys.filter((key) => !selected.has(key)).length > room) {
        toast.warning('每批最多 20 项审批', '已选择前 20 项可处理记录');
      }
    }
    setSelected((previous) => {
      const next = new Set(previous);
      if (!checked) {
        keys.forEach((key) => next.delete(key));
        return next;
      }
      keys.forEach((key) => {
        if (next.size >= MAX_BATCH) return;
        next.add(key);
      });
      return next;
    });
  };

  /* ---------- 审批 ---------- */
  const openDecision = (list: UnifiedAction[], approve: boolean, note = '') => {
    const relevant = list.filter((item) => item.state === 'pending' || item.state === 'ready');
    if (!relevant.length) {
      toast.warning('没有可审批的动作', '所选审批已过期或已完成');
      return;
    }
    if (relevant.length > MAX_BATCH) {
      toast.warning('每批最多 20 项审批', '请减少选择后再提交');
      return;
    }
    if (!relevant.some((item) => isActionable(item, now))) {
      toast.warning('没有可提交的审批', '审批已到期或状态已变化，请刷新后重试');
      return;
    }
    setConfirm({ items: relevant, approve, note, scope: filterSignature });
  };

  const sendDecision = useCallback(async (scheduled: ScheduledDecision) => {
    const { items: batch, snapshots, approve, note, seq } = scheduled;
    if (scheduledRef.current?.seq !== seq) return;
    scheduledRef.current = null;
    if (mountedRef.current) setUndo(null);
    const keys = batch.map(actionKey);
    try {
      const payload: DecisionInput[] = batch.map((item) => ({
        key: { platform_id: item.platform_id, self_id: item.self_id, source: item.source, action_id: item.action_id },
        expected_revision: item.revision,
        approve,
        ...(note ? { note } : {}),
      }));
      const response = await approvalsApi.decide(payload);
      const outputs = new Map<string, DecisionOutput>();
      for (const output of response.items ?? []) outputs.set(decisionKey(output.key), output);
      if (mountedRef.current) {
        setDecisions((previous) => {
          const next = { ...previous };
          for (const [key, output] of outputs) next[key] = output;
          return next;
        });
        setCatalog((previous) => previous ? {
          ...previous,
          items: previous.items.map((item) => {
            const output = outputs.get(actionKey(item));
            if (!output) return item;
            if (output.result === 'applied') {
              const applied = output.action;
              return {
                ...item,
                revision: applied?.revision ?? item.revision,
                state: applied?.state ?? item.state,
                source_state: applied?.source_state ?? item.source_state,
                decision: applied?.decision ?? (approve ? 'approved' : 'rejected'),
                can_decide: applied ? applied.state === 'pending' || applied.state === 'ready' : false,
                unavailable_reason: applied && (applied.state === 'pending' || applied.state === 'ready')
                  ? item.unavailable_reason : '审批已记录，等待平台回读结果',
                note: note || item.note,
                decided_at: item.decided_at ?? Math.floor(Date.now() / 1000),
                decided_by: item.decided_by ?? { kind: 'panel' as const, id: null },
              };
            }
            if (output.result === 'unknown') {
              return {
                ...item,
                state: 'unknown' as const,
                can_decide: false,
                unavailable_reason: '结果未确认，已重新读取原动作，请先核实平台状态',
                result: item.result ?? { reason: '提交回执不确定' },
              };
            }
            return {
              ...item,
              can_decide: false,
              unavailable_reason: output.error?.message || (output.result === 'conflict' ? '状态修订已变化，正在重新读取' : '本次提交未生效'),
            };
          }),
        } : previous);
        const counts: Record<DecisionOutput['result'], number> = { applied: 0, conflict: 0, unavailable: 0, failed: 0, unknown: 0 };
        for (const output of outputs.values()) counts[output.result] += 1;
        const parts = [
          counts.applied ? `${counts.applied} 项已写入` : '',
          counts.conflict ? `${counts.conflict} 项修订冲突` : '',
          counts.unavailable ? `${counts.unavailable} 项不可用` : '',
          counts.failed ? `${counts.failed} 项失败` : '',
          counts.unknown ? `${counts.unknown} 项结果未知` : '',
        ].filter(Boolean);
        const summaryText = parts.length ? parts.join(' · ') : '未返回逐项结果';
        const needsRequery = counts.conflict + counts.unavailable + counts.failed + counts.unknown > 0;
        setNotice(`${approve ? '批准' : '驳回'} ${batch.length} 项: ${summaryText}${needsRequery ? '；正在重新读取原动作，结果未知时不会自动重发' : ''}`);
        if (counts.unknown) toast.warning('审批结果未确认', '已重新读取原动作，请先在业务页核实，系统不会自动重发');
        else if (counts.conflict || counts.failed || counts.unavailable) toast.warning('部分审批未完成', summaryText);
        else toast.success(approve ? `已通过 ${batch.length} 项审批` : `已驳回 ${batch.length} 项审批`, '决策已写入，平台结果以 action.state 为准');
      }
    } catch (caught) {
      const message = errorText(caught);
      const status = caught instanceof ApiError ? caught.status : undefined;
      const rejectedBatch = status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 409 && status !== 425 && status !== 429;
      if (mountedRef.current) {
        if (rejectedBatch) {
          setCatalog((previous) => {
            if (!previous) return previous;
            const snapshotMap = new Map(snapshots.map((item) => [actionKey(item), item]));
            return { ...previous, items: previous.items.map((item) => snapshotMap.get(actionKey(item)) ?? item) };
          });
          setNotice(`审批未提交: ${message}`);
          toast.error('审批未提交', message);
        } else {
          const unknown: DecisionOutput = { key: batch[0] ? { platform_id: batch[0].platform_id, self_id: batch[0].self_id, source: batch[0].source, action_id: batch[0].action_id } : { platform_id: '', self_id: '', source: 'group', action_id: '' }, result: 'unknown', action: null, error: { code: 'request_unknown', message } };
          setDecisions((previous) => {
            const next = { ...previous };
            for (const item of batch) next[actionKey(item)] = { ...unknown, key: { platform_id: item.platform_id, self_id: item.self_id, source: item.source, action_id: item.action_id } };
            return next;
          });
          setCatalog((previous) => previous ? {
            ...previous,
            items: previous.items.map((item) => keys.includes(actionKey(item)) ? {
              ...item, state: 'unknown' as const, can_decide: false,
              unavailable_reason: '审批响应未确认，正在重新读取原动作，请勿重复提交',
              result: item.result ?? { reason: '提交响应丢失' },
            } : item),
          } : previous);
          setNotice(`审批响应未确认: ${message}。正在按原动作重新读取，请勿重新发起同一操作`);
          toast.warning('审批结果未确认', '正在重新读取原动作，不会自动重发');
        }
      }
    } finally {
      if (mountedRef.current) { busyRef.current = false; setBusy(false); }
      const load = reloadRef.current;
      if (load) await load(undefined, 'quiet');
    }
  }, []);

  const scheduleDecision = (batch: UnifiedAction[], approve: boolean, note: string) => {
    if (busyRef.current) {
      toast.warning('上一次审批仍在提交', '请等待状态回读后再操作');
      return;
    }
    const actionable = batch.filter((item) => isActionable(item, now));
    if (!actionable.length) {
      toast.warning('没有可提交的审批', '审批已过期或状态已变化，请刷新后重试');
      return;
    }
    if (actionable.length > MAX_BATCH) {
      toast.warning('每批最多 20 项审批', '请减少选择后再提交');
      return;
    }
    const seq = decisionSeq += 1;
    const keys = actionable.map(actionKey);
    const snapshots = actionable.map((item) => ({ ...item }));
    busyRef.current = true;
    setBusy(true);
    setDecisions((previous) => {
      const next = { ...previous };
      keys.forEach((key) => delete next[key]);
      return next;
    });
    setCatalog((previous) => previous ? {
      ...previous,
      items: previous.items.map((item) => keys.includes(actionKey(item)) ? {
        ...item,
        state: approve ? 'executing' as const : 'rejected' as const,
        can_decide: false,
        unavailable_reason: approve ? '已确认，5 秒撤销窗口结束后提交' : '已确认驳回，5 秒撤销窗口结束后记录',
        note: note || item.note,
      } : item),
    } : previous);
    setSelected((previous) => {
      const next = new Set(previous);
      keys.forEach((key) => next.delete(key));
      return next;
    });
    const names = actionable.length === 1 ? actionDescription(actionable[0]) : `${actionable.length} 项审批`;
    setNotice(`${names} · ${approve ? '已确认批准' : '已确认驳回'}${note ? ` · 附言: ${note}` : ''} · 5 秒内可撤销，提交后不可撤回`);
    const timer = window.setTimeout(() => {
      const scheduled = scheduledRef.current;
      if (!scheduled || scheduled.seq !== seq) return;
      void sendDecision(scheduled);
    }, UNDO_MS);
    scheduledRef.current = { seq, timer, items: actionable, snapshots, approve, note };
    setUndo({
      seq,
      keys,
      snapshots,
      title: approve ? `已确认通过 ${actionable.length} 项审批` : `已确认驳回 ${actionable.length} 项审批`,
      description: '5 秒内可撤销；提交后撤销入口立即关闭',
    });
  };

  const confirmDecision = (note: string) => {
    if (!confirm) return;
    if (confirm.scope !== filterSignature) {
      setConfirm(null);
      toast.warning('筛选范围已变化，审批已取消', '请重新选择审批后再提交');
      return;
    }
    const confirmItems = confirm.items
      .map((snapshot) => items.find((item) => actionKey(item) === actionKey(snapshot)))
      .filter((item): item is UnifiedAction => !!item);
    const approve = confirm.approve;
    setConfirm(null);
    scheduleDecision(confirmItems, approve, note);
  };

  const undoDecision = () => {
    const scheduled = scheduledRef.current;
    if (!undo || !scheduled || scheduled.seq !== undo.seq) {
      setUndo(null);
      return;
    }
    window.clearTimeout(scheduled.timer);
    scheduledRef.current = null;
    busyRef.current = false;
    setBusy(false);
    setCatalog((previous) => {
      if (!previous) return previous;
      const snapshotMap = new Map(undo.snapshots.map((item) => [actionKey(item), item]));
      return { ...previous, items: previous.items.map((item) => snapshotMap.get(actionKey(item)) ?? item) };
    });
    setDecisions((previous) => {
      const next = { ...previous };
      undo.keys.forEach((key) => delete next[key]);
      return next;
    });
    setUndo(null);
    setNotice(`已撤销上一次审批：${undo.keys.length} 项记录恢复为待处理，未提交到平台`);
    toast.info('已撤销审批', '记录已恢复为待处理，未提交到平台');
  };

  const dismissUndo = () => setUndo(null);

  const refresh = async () => {
    if (!isRunning || loading || syncing) return;
    await reload(undefined, 'refresh');
    toast.info('审批已刷新', '已按当前筛选范围重新读取');
  };

  const togglePolling = () => {
    const next = !polling;
    setPolling(next);
    toast.info(next ? '已恢复自动刷新' : '已暂停自动刷新', next ? '执行中的动作每 2 秒回读' : '仍可手动刷新');
  };

  /* ---------- 键盘快捷键 ---------- */
  const keyboardState = useRef({ pageItems, activeKey, activeItem, now });
  keyboardState.current = { pageItems, activeKey, activeItem, now };
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
      const targetElement = event.target as HTMLElement | null;
      if (targetElement?.closest('input, textarea, select, [contenteditable="true"], [role="combobox"], [role="dialog"]')) return;

      const current = keyboardState.current;
      const key = event.key;

      if (key === '?') {
        event.preventDefault();
        setShortcutsOpen(true);
        return;
      }
      if (key === '/') {
        event.preventDefault();
        document.getElementById('approval-search')?.focus();
        return;
      }
      if (key === 'Escape') {
        setActiveKey(null);
        return;
      }
      if (key.toLowerCase() === 'j' || key.toLowerCase() === 'k') {
        if (!current.pageItems.length) return;
        event.preventDefault();
        const currentIndex = current.pageItems.findIndex((item) => actionKey(item) === current.activeKey);
        const direction = key.toLowerCase() === 'j' ? 1 : -1;
        const nextIndex = currentIndex < 0 ? 0 : Math.max(0, Math.min(current.pageItems.length - 1, currentIndex + direction));
        const nextKey = actionKey(current.pageItems[nextIndex]);
        setActiveKey(nextKey);
        requestAnimationFrame(() => {
          const card = Array.from(document.querySelectorAll<HTMLElement>('[data-approval-key]'))
            .find((node) => node.dataset.approvalKey === nextKey);
          card?.querySelector<HTMLElement>('button')?.focus();
        });
        return;
      }
      if (key.toLowerCase() === 'x') {
        if (!current.activeItem || !isActionable(current.activeItem, current.now)) return;
        event.preventDefault();
        toggleSelect(current.activeItem);
        return;
      }
      if (key.toLowerCase() === 'a' || key.toLowerCase() === 'r') {
        const active = current.activeItem;
        if (!active) {
          toast.info('先选择一条审批', '使用 J / K 在列表中移动');
          return;
        }
        if (!isActionable(active, current.now)) {
          toast.warning('当前审批不可处理', active.unavailable_reason || '审批已过期或状态已变化');
          return;
        }
        event.preventDefault();
        setConfirm({ items: [active], approve: key.toLowerCase() === 'a', note: '', scope: filterSignature });
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [filterSignature, toggleSelect]);

  const rail = (
    <FilterRail
      scope={scope}
      counts={counts}
      platforms={platforms}
      accounts={accounts}
      loading={loading}
      complete={complete}
      totalRead={catalog?.total ?? 0}
      summaryReady={!!summary}
      onPatch={patchScope}
      onReset={resetFilters}
      onApplyText={(nextTarget, nextAction) => patchScope({ target: nextTarget, action: nextAction })}
    />
  );

  if (!isRunning) {
    return (
      <div className="space-y-5">
        <PageHeader
          eyebrow={<Link to="/platforms" className="inline-flex items-center gap-1 text-xs text-fg-3 transition hover:text-fg"><ArrowLeft className="h-3 w-3" />平台状态</Link>}
          title="审批中心"
          description="集中处理已提交的平台动作，并回读执行结果；原始申请仍在对应业务页处理"
        />
        <Card tint="orange" padding="lg" role="status" className="animate-rise">
          <div className="flex flex-col items-start gap-4 sm:flex-row sm:items-center">
            <div className="grid h-12 w-12 flex-none place-items-center rounded-2xl bg-[rgb(var(--tint)/0.14)] text-tint">
              <WifiOff className="h-6 w-6" />
            </div>
            <div className="min-w-0 flex-1">
              <h2 className="text-lg font-semibold text-fg">{runState === 'unknown' ? '后端状态未知，暂不可读取或审批' : '后端未运行，暂不可读取或审批'}</h2>
              <p className="mt-1 text-[13px] leading-5 text-fg-3">
                审批动作保存在后端数据库中。启动后端后，这里会自动恢复读取；也可以回到仪表盘查看服务状态。
              </p>
            </div>
            <Button onClick={() => navigate('/')}>回到仪表盘</Button>
          </div>
        </Card>
      </div>
    );
  }

  const detailPanel = (
    <DetailPanel
      key={activeItem ? actionKey(activeItem) : 'empty'}
      item={activeItem}
      now={now}
      busy={busy}
      output={activeItem ? decisions[actionKey(activeItem)] : undefined}
      onApprove={(item, note) => openDecision([item], true, note)}
      onReject={(item, note) => openDecision([item], false, note)}
      onClose={() => setActiveKey(null)}
      onRefresh={() => void refresh()}
    />
  );

  return (
    <div className="space-y-5 pb-24">
      <PageHeader
        eyebrow={<Link to="/platforms" className="inline-flex items-center gap-1 text-xs text-fg-3 transition hover:text-fg"><ArrowLeft className="h-3 w-3" />平台状态</Link>}
        title="审批中心"
        description="集中处理已提交的平台动作 · 所有决定都会回读平台结果，原始申请仍在业务页处理"
        actions={
          <>
            <Badge tone={(counts.states.pending ?? 0) > 0 ? 'orange' : 'green'} dot live={(counts.states.pending ?? 0) > 0}>
              {(counts.states.pending ?? 0) > 0 ? `${counts.states.pending} 项待处理` : focusedFilter ? '当前范围 0 项待处理' : '全部处理完'}
            </Badge>
            <Button icon={<RefreshCw className={cn('h-4 w-4', syncing && 'animate-spin-slow')} />}
              loading={syncing} disabled={loading || syncing} onClick={() => void refresh()}>刷新</Button>
          </>
        }
      />

      {error && (
        <div role="alert" className="flex items-start gap-2.5 rounded-xl border border-[rgb(var(--rgb-red)/0.28)] bg-[rgb(var(--rgb-red)/0.08)] px-3.5 py-2.5 text-[12px] text-fg-2 animate-rise">
          <AlertTriangle className="mt-0.5 h-4 w-4 flex-none text-red" />
          <p className="min-w-0 flex-1">{error}</p>
          <Button size="sm" onClick={() => void refresh()}>重试</Button>
        </div>
      )}

      {notice && (
        <div role="status" className="surface tint-pink flex items-start gap-2.5 !rounded-xl px-3.5 py-2.5 animate-rise">
          <Info className="mt-0.5 h-4 w-4 flex-none text-tint" />
          <p className="min-w-0 flex-1 whitespace-pre-wrap text-[11.5px] leading-5 text-fg-2">{notice}</p>
          <IconButton label="关闭提示" size="sm" onClick={() => setNotice('')} className="-my-1"><X className="h-3.5 w-3.5" /></IconButton>
        </div>
      )}

      {(!complete || warnings.length > 0 || coverage.some((entry) => entry.state !== 'available' || !entry.complete)) && (
        <div className="flex flex-wrap gap-2 animate-rise" style={{ animationDelay: '40ms' }}>
          {coverage.map((entry) => (
            <Badge key={entry.service} tone={coverageTone(entry)} dot live={entry.state === 'available'}>
              {serviceLabels[entry.service]} {sourceStateLabels[entry.state]}
              {entry.as_of ? ` · ${new Date(entry.as_of * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : ''}
            </Badge>
          ))}
          {warnings.map((warning) => (
            <div key={warning} role="alert"
              className="flex min-w-[240px] flex-1 items-start gap-2 rounded-xl border border-[rgb(var(--rgb-orange)/0.24)] bg-[rgb(var(--rgb-orange)/0.07)] px-3 py-2 text-[11.5px] leading-5 text-fg-2">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-orange" />
              <span>{warning}</span>
            </div>
          ))}
        </div>
      )}

      <div className="grid items-start gap-4 lg:grid-cols-[216px_minmax(0,1fr)] xl:grid-cols-[216px_minmax(0,1fr)_minmax(340px,390px)] 2xl:grid-cols-[232px_minmax(0,1fr)_minmax(360px,430px)]">
        <aside className="hidden lg:block" aria-label="审批筛选">
          <div className="lg:sticky lg:top-0">
            <Card padding="none" tint="pink" className="h-[calc(100vh-228px)] min-h-[520px] overflow-hidden">
              {rail}
            </Card>
          </div>
        </aside>

        <RequestList
          items={pageItems}
          filteredCount={catalog?.total ?? 0}
          loading={loading}
          now={now}
          isRunning={isRunning}
          scope={scope}
          query={queryDraft}
          onQuery={updateQuery}
          sort={sortMode}
          onSort={setSort}
          selected={selected}
          onToggleSelect={toggleSelect}
          onSelectAll={selectAll}
          activeKey={activeKey}
          onOpen={(item) => setActiveKey(actionKey(item))}
          onApprove={(item) => openDecision([item], true)}
          onReject={(item) => openDecision([item], false)}
          cursorActive={!!cursor}
          hasMore={hasMore}
          onNext={nextPage}
          onFirst={firstPage}
          onOpenFilters={() => setFiltersOpen(true)}
          onRefresh={() => void refresh()}
          syncing={syncing}
          polling={polling}
          onTogglePolling={togglePolling}
          pollInterval={pollInterval}
          lastSync={lastSync}
          onOpenShortcuts={() => setShortcutsOpen(true)}
          todayDecided={counts.todayDecided}
          latencyMs={latency}
          onResetFilters={resetFilters}
          onSetState={(nextState) => patchScope({ state: nextState })}
          platforms={platforms}
          decisions={decisions}
        />

        {wide && (
          <aside className="hidden xl:block" aria-label="审批详情">
            <div className="xl:sticky xl:top-0">
              <Card
                padding="none"
                tint={activeItem ? (activeItem.source === 'group' ? 'pink' : activeItem.source === 'invitation' ? 'purple' : activeItem.source === 'memory' ? 'green' : 'teal') : 'pink'}
                className="h-[calc(100vh-228px)] min-h-[520px] max-h-[880px] overflow-hidden"
              >
                {detailPanel}
              </Card>
            </div>
          </aside>
        )}
      </div>

      <BatchBar
        count={selected.size}
        actionable={selectedActionable}
        onApprove={() => openDecision(selectedItems, true)}
        onReject={() => openDecision(selectedItems, false)}
        onClear={() => setSelected(new Set())}
        disabled={busy}
      />

      <UndoBar
        key={undo?.seq ?? 0}
        open={!!undo}
        title={undo?.title ?? ''}
        description={undo?.description ?? ''}
        duration={UNDO_MS}
        raised={selected.size > 0}
        onUndo={undoDecision}
        onDismiss={dismissUndo}
      />

      <DecisionModal
        open={!!confirm}
        items={confirm?.items ?? []}
        approve={confirm?.approve ?? true}
        initialNote={confirm?.note ?? ''}
        now={now}
        busy={busy}
        onClose={() => setConfirm(null)}
        onConfirm={confirmDecision}
      />
      <ShortcutsModal open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />

      <Drawer open={filtersOpen} onClose={() => setFiltersOpen(false)} label="筛选审批" width={360}>
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex flex-none items-center justify-between border-b border-line px-4 py-3">
            <span className="text-sm font-semibold text-fg">筛选审批</span>
            <IconButton label="关闭筛选" shortcut="Esc" size="sm" onClick={() => setFiltersOpen(false)}><X className="h-4 w-4" /></IconButton>
          </div>
          <div className="flex min-h-0 flex-1 flex-col">
            <FilterRail
              scope={scope}
              counts={counts}
              platforms={platforms}
              accounts={accounts}
              loading={loading}
              complete={complete}
              totalRead={catalog?.total ?? 0}
              summaryReady={!!summary}
              onPatch={patchScope}
              onReset={resetFilters}
              onApplyText={(nextTarget, nextAction) => patchScope({ target: nextTarget, action: nextAction })}
              onDone={() => setFiltersOpen(false)}
            />
          </div>
        </div>
      </Drawer>

      {!wide && (
        <Drawer open={!!activeItem} onClose={() => setActiveKey(null)} label="审批详情" width={560}>
          {detailPanel}
        </Drawer>
      )}
    </div>
  );
}
