import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import {
  ArrowLeft, ChevronRight, Inbox, Mail, RefreshCw, Search, Settings2, ShieldCheck, Users,
} from 'lucide-react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupAccountsResult, type GroupListResult, type GroupRow, type GroupSettings } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { EmptyState, PageHeader } from '@/components/kit/Feedback';
import { Input } from '@/components/kit/Field';
import { Modal } from '@/components/kit/Modal';
import { toast } from '@/components/kit/Toast';
import { useBackendStore } from '@/stores/useBackendStore';
import { useShallow } from 'zustand/react/shallow';
import { confirmDiscard, useDirtyGuard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';
import { formatRelativeTime, formatTime } from '@/utils/format';
import { groupActionLabels } from './groupActionLabels';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { AccountInvitations } from './AccountInvitations';
import {
  Avatar, FieldBlock, Note, Pager, ProgressBar, bindingSourceLabels, hueFor,
  membershipLabels, responseSourceLabels,
} from './shared';

const MEMBERSHIPS = [
  ['joined', '已加入'], ['all', '全部成员关系'], ['left', '已离开'],
  ['unknown', '待确认'], ['config_only', '仅配置未发现'],
] as const;
const RESPONSES = [['all', '全部响应状态'], ['enabled', '响应开启'], ['disabled', '响应关闭']] as const;
type ApprovalDraft = Record<string, 'inherit' | 'approval_required' | 'auto_execute'>;

// 同步进度轮询节奏: 2 秒一次, 最多 30 轮 (约一分钟)
const SYNC_POLL_INTERVAL_MS = 2000;
const SYNC_POLL_MAX_TICKS = 30;

// 群目录新增的按页摘要字段, 后端已随列表返回
type GroupRowExt = GroupRow & {
  binding?: { agent?: string | null; provider?: string; source?: 'group' | 'platform' | 'default' };
  pending_actions?: number;
  last_activity_at?: number | null;
  activity_coverage?: { source?: string; complete?: boolean; observed_from?: number | null; observed_to?: number | null };
};

function defaultsFrom(settings: GroupSettings): ApprovalDraft {
  return Object.fromEntries(settings.approval_actions.map((item) => [item.action_type,
    settings.approval_defaults[item.action_type] || 'inherit']));
}

function timeText(value: number | null): string {
  return value == null ? '无记录' : formatTime(value);
}

export function Groups() {
  const { adapterId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const { isRunning, refreshAll } = useBackendStore(useShallow((state) => ({ isRunning: state.isRunning, refreshAll: state.refreshAll })));
  const account = params.get('account') || '';
  const q = params.get('q') || '';
  const membership = params.get('membership') || 'joined';
  const response = params.get('response') || 'all';
  const page = Math.max(1, Number(params.get('page') || 1) || 1);
  const pageSize = [25, 50, 100].includes(Number(params.get('pageSize'))) ? Number(params.get('pageSize')) as 25 | 50 | 100 : 25;
  const [searchText, setSearchText] = useState(q);
  const [accounts, setAccounts] = useState<GroupAccountsResult | null>(null);
  const [listing, setListing] = useState<GroupListResult | null>(null);
  const [settings, setSettings] = useState<GroupSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [showMode, setShowMode] = useState(false);
  const [modeDraft, setModeDraft] = useState<'selected' | 'all'>('selected');
  const [modeSaving, setModeSaving] = useState(false);
  const [settingsApplying, setSettingsApplying] = useState(false);
  const [showApproval, setShowApproval] = useState(false);
  const [showInvitations, setShowInvitations] = useState(() => params.get('invites') === '1');
  const [approvalDraft, setApprovalDraft] = useState<ApprovalDraft>({});
  const [approvalBaseline, setApprovalBaseline] = useState('{}');
  const [approvalSaving, setApprovalSaving] = useState(false);
  const [approvalError, setApprovalError] = useState('');
  const [approvalConflict, setApprovalConflict] = useState(false);
  const [syncId, setSyncId] = useState('');
  const listRequest = useRef(0);
  const accountRequest = useRef(0);
  const settingsRequest = useRef(0);
  const accountOptions = accounts?.items || [];
  const historical = !!account && !!accounts && account !== accounts.current_account;
  const waiting = !!accounts && !accounts.current_account;
  // 平台已确认账号但目录为空时, 说明该平台类型不提供群聊目录
  const unsupportedAdapter = !!accounts && !accounts.current_account && !accounts.waiting_for_account && (accounts.items || []).length === 0;
  const visibleListing = listing?.account === account ? listing : null;
  const visibleSettings = settings?.self_id === account ? settings : null;
  const approvalDirty = JSON.stringify(approvalDraft) !== approvalBaseline;
  const syncing = !!syncId || visibleListing?.sync.status === 'running';

  const updateParams = useCallback((values: Record<string, string | null>) => {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      for (const [key, value] of Object.entries(values)) {
        if (value === null || value === '') next.delete(key);
        else next.set(key, value);
      }
      return next;
    });
  }, [setParams]);

  useEffect(() => { refreshAll().catch(() => undefined); }, [refreshAll]);
  useEffect(() => { setSearchText(q); }, [q]);
  useEffect(() => {
    if (searchText === q) return;
    const timer = window.setTimeout(() => updateParams({ q: searchText, page: null }), 300);
    return () => window.clearTimeout(timer);
  }, [searchText, q, updateParams]);

  const loadAccounts = useCallback(async () => {
    const request = ++accountRequest.current;
    try {
      const result = await groupApi.accounts(adapterId, isRunning);
      if (request !== accountRequest.current) return;
      setAccounts(result);
      if (!account && result.current_account) updateParams({ account: result.current_account });
      setError('');
    } catch (caught) {
      if (request === accountRequest.current) setError(errorText(caught));
    } finally {
      if (request === accountRequest.current && !account) setLoading(false);
    }
  }, [adapterId, isRunning, account, updateParams]);

  useEffect(() => { loadAccounts(); }, [loadAccounts]);

  useEffect(() => {
    if (!account) return;
    const request = ++settingsRequest.current;
    groupApi.settings(adapterId, account, isRunning).then((result) => {
      if (request === settingsRequest.current) setSettings(result);
    }).catch((caught) => {
      if (request === settingsRequest.current) setError(errorText(caught));
    });
    return () => { settingsRequest.current += 1; };
  }, [adapterId, account, isRunning]);

  const loadList = useCallback(async (initial = false) => {
    if (!account) return;
    const request = ++listRequest.current;
    if (initial) setLoading(true);
    else setRefreshing(true);
    try {
      const result = await groupApi.list(adapterId, {
        account, q, membership: membership as 'joined' | 'left' | 'unknown' | 'config_only' | 'all',
        response: response as 'enabled' | 'disabled' | 'all', page, page_size: pageSize,
      }, isRunning);
      if (request !== listRequest.current) return;
      setListing(result);
      setError('');
    } catch (caught) {
      if (request === listRequest.current) setError(errorText(caught));
    } finally {
      if (request === listRequest.current) { setLoading(false); setRefreshing(false); }
    }
  }, [adapterId, account, q, membership, response, page, pageSize, isRunning]);

  useEffect(() => {
    loadList(true);
    return () => { listRequest.current += 1; };
  }, [loadList]);

  useEffect(() => {
    if (visibleListing?.sync.status === 'running' && visibleListing.sync.sync_id && !syncId) {
      setSyncId(visibleListing.sync.sync_id);
    }
  }, [visibleListing?.sync.status, visibleListing?.sync.sync_id, syncId]);

  const openMode = useCallback(async () => {
    if (!account) return;
    try {
      const result = await groupApi.settings(adapterId, account, isRunning);
      setSettings(result);
      setModeDraft(result.mode);
      setShowMode(true);
    } catch (caught) { setError(errorText(caught)); }
  }, [adapterId, account, isRunning]);

  const closeMode = useCallback(() => {
    if (modeDraft !== settings?.mode && !confirmDiscard()) return;
    setShowMode(false);
  }, [modeDraft, settings]);
  const openApproval = useCallback(async () => {
    if (!account) return;
    try {
      const result = await groupApi.settings(adapterId, account, isRunning);
      const fresh = defaultsFrom(result);
      setSettings(result); setApprovalDraft(fresh); setApprovalBaseline(JSON.stringify(fresh));
      setApprovalError(''); setApprovalConflict(false); setShowApproval(true);
    } catch (caught) { setError(errorText(caught)); }
  }, [adapterId, account, isRunning]);
  const closeApproval = useCallback(() => {
    if (approvalDirty && !confirmDiscard()) return;
    setShowApproval(false);
  }, [approvalDirty]);
  useDirtyGuard((showMode && modeDraft !== settings?.mode) || (showApproval && approvalDirty));

  const saveMode = useCallback(async () => {
    if (!settings || !account || modeSaving) return;
    setModeSaving(true);
    try {
      const result = await groupApi.saveSettings(adapterId, {
        expected_self_id: account, expected_revision: settings.revision,
        mode: modeDraft, approval_defaults: settings.approval_defaults,
      }, isRunning);
      setSettings({ ...settings, ...result });
      setShowMode(false);
      await loadList();
      if (result.apply_status !== 'applied') setError('配置已保存, 尚未生效; 请在平台恢复后重试应用');
      else toast.success('接入模式已保存', modeDraft === 'all' ? '以后新加入的群默认响应' : '新群默认不响应, 逐群例外保留');
    } catch (caught) { setError(errorText(caught)); }
    finally { setModeSaving(false); }
  }, [settings, account, modeSaving, adapterId, modeDraft, isRunning, loadList]);

  const retrySettings = useCallback(async () => {
    if (!visibleSettings || !account || !isRunning || historical || settingsApplying) return;
    setSettingsApplying(true);
    try {
      const result = await groupApi.applySettings(adapterId, account, visibleSettings.revision);
      setSettings(result);
      await loadList();
      setError(result.apply_status === 'failed' ? `账号设置仍未生效: ${result.apply_error || '刷新失败'}` : '');
      if (result.apply_status !== 'failed') toast.success('账号设置已重新应用');
    } catch (caught) { setError(errorText(caught)); }
    finally { setSettingsApplying(false); }
  }, [visibleSettings, account, isRunning, historical, settingsApplying, adapterId, loadList]);
  const saveApproval = useCallback(async () => {
    if (!settings || !account || approvalSaving || !approvalDirty || approvalConflict) return;
    const defaults = Object.fromEntries(Object.entries(approvalDraft)
      .filter(([, mode]) => mode !== 'inherit')) as GroupSettings['approval_defaults'];
    setApprovalSaving(true); setApprovalError('');
    try {
      const result = await groupApi.saveSettings(adapterId, {
        expected_self_id: account, expected_revision: settings.revision,
        mode: settings.mode, approval_defaults: defaults,
      }, isRunning);
      setSettings({ ...settings, ...result }); setApprovalBaseline(JSON.stringify(approvalDraft));
      setShowApproval(false);
      if (result.apply_status !== 'applied') setError('审批默认设置已保存, 尚未生效');
      else toast.success('默认审批设置已保存');
    } catch (caught) {
      setApprovalError(errorText(caught));
      if (caught instanceof ApiError && caught.status === 409) setApprovalConflict(true);
    } finally { setApprovalSaving(false); }
  }, [settings, account, approvalSaving, approvalDirty, approvalConflict, approvalDraft, adapterId, isRunning]);

  const beginSync = useCallback(async () => {
    if (!account || historical || !isRunning) return;
    try {
      const result = await groupApi.sync(adapterId, account);
      setSyncId(result.sync_id);
      await loadList();
    } catch (caught) { setError(errorText(caught)); }
  }, [account, historical, isRunning, adapterId, loadList]);

  useEffect(() => {
    if (!syncId || !isRunning || historical) return;
    let ticks = 0;
    const poll = startVisiblePoll(async (signal) => {
      ticks += 1;
      try {
        const status = await groupApi.syncStatus(adapterId, account, syncId, signal);
        if (signal.aborted) return;
        if (status.status !== 'running') {
          setSyncId('');
          await loadList();
          if (status.status === 'complete') toast.success('群列表同步完成', '目录已更新');
        } else if (ticks >= SYNC_POLL_MAX_TICKS) {
          setSyncId('');
        }
      } catch (caught) { if (!signal.aborted) { setError(errorText(caught)); setSyncId(''); } }
    }, { interval: SYNC_POLL_INTERVAL_MS, immediate: false, shouldContinue: () => ticks < SYNC_POLL_MAX_TICKS });
    return () => poll.stop();
  }, [adapterId, account, syncId, isRunning, historical, loadList]);

  const sync = visibleListing?.sync;
  const rows = (visibleListing?.items || []) as GroupRowExt[];
  const totalPages = Math.max(1, Math.ceil((visibleListing?.total || 0) / pageSize));
  const pendingCount = rows.reduce((sum, item) => sum + (item.pending_actions || 0), 0);
  const emptyMessage = useMemo(() => {
    if (sync?.status === 'never') return '尚未同步群列表';
    if (sync?.status === 'complete' && !q && membership === 'joined') return '当前账号未加入任何群';
    return '没有符合筛选条件的群';
  }, [sync?.status, q, membership]);

  if (!adapterId) return <Card>平台地址无效</Card>;

  const destination = (groupId: string) => `/platforms/${encodeURIComponent(adapterId)}/groups/${encodeURIComponent(groupId)}/overview?account=${encodeURIComponent(account)}`;
  const modeLabel = visibleSettings?.mode === 'all' ? '全部群（含以后新加入）' : visibleSettings?.mode === 'selected' ? '仅所选群' : '读取中';

  return (
    <div className="space-y-5">
      <PageHeader
        title="群聊管理"
        description={<>平台 <span className="font-mono text-fg-2">{adapterId}</span> · 机器人账号 <span className="font-mono text-fg-2">{account || '待确认'}</span> · 逐群响应、Agent 与会话覆盖</>}
        eyebrow={<div className="flex items-center gap-3 text-[12px] text-fg-3">
          <Link to="/platforms" className="inline-flex items-center gap-1.5 transition hover:text-pink"><ArrowLeft className="h-3.5 w-3.5" />平台状态</Link>
          <span className="text-fg-4">/</span>
          <span>群聊</span>
        </div>}
        actions={<>
          <Button variant="ghost" icon={<Mail className="h-4 w-4" />} onClick={() => setShowInvitations(true)} disabled={!account || !isRunning || historical}>机器人入群邀请</Button>
          <Button variant="primary" icon={<RefreshCw className={`h-4 w-4 ${syncing ? 'animate-spin-slow' : ''}`} />}
            onClick={beginSync} disabled={!account || !isRunning || historical || syncing}>
            {syncing ? '同步中…' : '同步群列表'}
          </Button>
        </>}
      />

      {error && <Note tone="red" title="操作失败">{error}</Note>}

      {loading && !visibleListing && !accounts ? (
        <div className="space-y-3" aria-label="群目录加载中" role="status">
          {[0, 1, 2, 3, 4].map((index) => (
            <div key={index} className="surface flex items-center gap-4 p-4">
              <div className="skeleton h-11 w-11 rounded-xl" />
              <div className="flex-1 space-y-2"><div className="skeleton h-3 w-2/5" /><div className="skeleton h-3 w-1/4" /></div>
            </div>
          ))}
        </div>
      ) : null}

      {waiting && !account ? (
        <Card padding="none">
          <EmptyState tint="pink" icon={<Users className="h-6 w-6" />} title="等待机器人连接并确认账号"
            description="确认前不会显示假群列表或启用任何群。连接平台后会自动确认机器人账号。" />
        </Card>
      ) : null}

      {unsupportedAdapter && !account ? (
        <Card padding="none">
          <EmptyState tint="pink" icon={<Users className="h-6 w-6" />} title="此适配器没有群聊目录"
            description="当前平台类型不提供群列表, 或机器人账号尚未确认。可以返回平台状态检查连接。"
            actions={<Link to="/platforms" className="ctl ctl-sm">返回平台状态</Link>} />
        </Card>
      ) : null}

      {accounts && accountOptions.length > 0 && account ? (
        <Card padding="md" className="animate-rise">
          <div className="flex flex-wrap items-end gap-3">
            <FieldBlock label="查看账号" className="min-w-[190px] flex-1">
              <select className="field w-full" value={account} onChange={(event) => updateParams({ account: event.target.value, page: null })}>
                {accountOptions.map((item) => (
                  <option key={item.self_id} value={item.self_id}>{item.self_id}{item.self_id === accounts.current_account ? ' · 当前' : ' · 历史'}</option>
                ))}
              </select>
            </FieldBlock>
            <div className="flex flex-wrap items-center gap-2 pb-0.5">
              <Button variant="ghost" icon={<Settings2 className="h-4 w-4" />} onClick={openMode} disabled={historical}>修改接入模式</Button>
              <Button variant="ghost" icon={<ShieldCheck className="h-4 w-4" />} onClick={openApproval} disabled={historical}>管理默认设置</Button>
              <Button variant="ghost" icon={<Inbox className="h-4 w-4" />} onClick={() => setShowInvitations(true)} disabled={!isRunning || historical}>入群邀请</Button>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-line bg-glass px-3.5 py-2.5 text-[12px] text-fg-3" aria-live="polite">
            <span className="flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${visibleSettings?.mode === 'all' ? 'bg-green' : 'bg-pink'}`} />
              接入模式 <span className="font-medium text-fg-2">{modeLabel}</span>
            </span>
            {visibleSettings?.apply_status ? (
              <span>应用状态 <span className={`font-medium ${visibleSettings.apply_status === 'applied' ? 'text-green' : visibleSettings.apply_status === 'failed' ? 'text-red' : 'text-orange'}`}>
                {visibleSettings.apply_status === 'applied' ? '已生效' : visibleSettings.apply_status === 'failed' ? '失败' : '待应用'}
              </span></span>
            ) : null}
            <span>已加入 <span className="num font-medium text-fg-2">{visibleListing?.counts.joined ?? '—'}</span></span>
            <span>响应开启 <span className="num font-medium text-fg-2">{visibleListing?.counts.response_enabled ?? '—'}</span></span>
            <span>配置群 <span className="num font-medium text-fg-2">{visibleListing?.counts.configured ?? '—'}</span></span>
            <span>待处理动作 <span className={`num font-medium ${pendingCount ? 'text-orange' : 'text-fg-2'}`}>{pendingCount}</span></span>
            <span>上次完整同步 <span className="font-medium text-fg-2">{timeText(sync?.last_complete_at ?? null)}</span></span>
            {historical && <span className="ml-auto rounded-full bg-[rgb(var(--rgb-orange)/0.12)] px-2 py-0.5 text-orange">历史账号只读</span>}
          </div>

          {historical && <p className="mt-2 text-[12px] text-fg-3">当前连接账号不同, 历史账号只读。账号选择只切换查看数据。</p>}
          {!isRunning && <Note tone="orange" className="mt-3">后端离线。可查看历史快照, 网络动作暂不可执行。</Note>}
          {syncing && (
            <div className="mt-3 space-y-1.5">
              <div className="flex flex-wrap items-center justify-between gap-2 text-[11.5px] text-fg-3">
                <span>正在从平台拉取群目录…</span><span>保留旧目录, 同步完成前不判断退群</span>
              </div>
              <ProgressBar value={66} tone="pink" /><span className="sr-only">同步进行中</span>
            </div>
          )}
          {sync?.status === 'failed' || sync?.status === 'partial' ? (
            <Note tone="orange" className="mt-3">
              同步{sync.status === 'failed' ? '失败' : '不完整'}, 旧目录已保留, 未据此判断退群。原因: {sync.reason || '未知'}
            </Note>
          ) : null}
          {visibleSettings?.apply_status === 'failed' && !historical && (
            <Note tone="red" className="mt-3" title="账号设置已保存, 应用失败"
              action={<Button size="sm" variant="danger" loading={settingsApplying} onClick={retrySettings} disabled={!isRunning}>重试应用</Button>}>
              {visibleSettings.apply_error || '刷新失败'}
            </Note>
          )}
        </Card>
      ) : null}

      {account ? (
        <>
          <div className="flex flex-wrap items-center gap-2 animate-rise" style={{ animationDelay: '40ms' }}>
            <div className="relative min-w-[210px] flex-1 sm:max-w-xs">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-fg-4" />
              <Input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder="搜索群名或群号"
                aria-label="搜索群名或群号" className="!pl-9" />
            </div>
            <label className="text-[12px] text-fg-3">
              <span className="sr-only">成员关系</span>
              <select className="field !h-9 !w-[142px] !text-[12.5px]" value={membership} aria-label="成员关系"
                onChange={(event) => updateParams({ membership: event.target.value, page: null })}>
                {MEMBERSHIPS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label className="text-[12px] text-fg-3">
              <span className="sr-only">响应状态</span>
              <select className="field !h-9 !w-[150px] !text-[12.5px]" value={response} aria-label="响应状态"
                onChange={(event) => updateParams({ response: event.target.value, page: null })}>
                {RESPONSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <span className="ml-auto text-[11.5px] text-fg-4">共 <span className="num text-fg-2">{visibleListing?.total ?? '—'}</span> 项{refreshing ? ' · 刷新中' : ''}</span>
          </div>

          {loading && !visibleListing ? (
            <div className="space-y-3" aria-label="群列表加载中" role="status">
              {[0, 1, 2].map((index) => (
                <div key={index} className="surface flex items-center gap-4 p-4">
                  <div className="skeleton h-11 w-11 rounded-xl" />
                  <div className="flex-1 space-y-2"><div className="skeleton h-3 w-2/5" /><div className="skeleton h-3 w-1/4" /></div>
                </div>
              ))}
            </div>
          ) : null}

          {visibleListing ? (
            <Card padding="none" className="animate-rise overflow-hidden">
              {/* 移动端卡片先于桌面表格渲染, 保证按文本定位的既有场景在桌面视口命中可见表格行 */}
              <div className="space-y-2 p-3 md:hidden">
                {rows.map((item) => (
                  <Link key={item.group_id} to={destination(item.group_id)} className="flex w-full items-center gap-3 rounded-xl border border-line bg-glass p-3 text-left transition hover:bg-glass-hover">
                    <Avatar name={item.group_name || item.group_id} hue={hueFor(item.group_id)} square />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[13px] font-medium text-fg">{item.group_name || '未命名群'}</div>
                      <div className="num mt-0.5 text-[11px] text-fg-3">{item.group_id} · {item.member_count ?? '—'} 人 · {membershipLabels[item.membership] || item.membership}</div>
                    </div>
                    {!!item.pending_actions && <Badge tone="orange" dot>{item.pending_actions}</Badge>}
                    <Badge tone={item.response_enabled ? 'green' : 'neutral'}>{item.response_enabled ? '响应开' : '响应关'}</Badge>
                    <ChevronRight className="h-4 w-4 text-fg-4" />
                  </Link>
                ))}
              </div>
              <div className="hidden md:block">
                <table className="w-full text-left text-[13px]">
                  <thead>
                    <tr className="border-b border-line text-[11px] text-fg-4">
                      <th className="px-4 py-2.5 font-medium">群名 / 群号</th>
                      <th className="px-4 py-2.5 font-medium">成员</th>
                      <th className="px-4 py-2.5 font-medium">成员关系</th>
                      <th className="px-4 py-2.5 font-medium">响应</th>
                      <th className="px-4 py-2.5 font-medium">绑定 Agent</th>
                      <th className="px-4 py-2.5 font-medium">最近活动</th>
                      <th className="px-4 py-2.5 text-right font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody className="stagger">
                    {rows.map((item, index) => (
                      <tr key={item.group_id} className="group border-b border-line transition-colors last:border-0 hover:bg-glass-hover" style={{ ['--i' as string]: index }}>
                        <td className="px-4 py-2.5">
                          <div className="flex items-center gap-3">
                            <Avatar name={item.group_name || item.group_id} hue={hueFor(item.group_id)} size="md" square />
                            <div className="min-w-0">
                              <div className="flex items-center gap-1.5">
                                <span className="truncate font-medium text-fg">{item.group_name || '未命名群'}</span>
                                {!!item.pending_actions && <Badge tone="orange" dot>{item.pending_actions}</Badge>}
                              </div>
                              <span className="num font-mono text-[11px] text-fg-4">{item.group_id}</span>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-2.5">
                          <div className="num text-[12px] text-fg-2">{item.member_count ?? '—'} / {item.max_member_count ?? '—'}</div>
                          <div className="mt-1 w-24"><ProgressBar value={item.member_count && item.max_member_count ? (item.member_count / item.max_member_count) * 100 : 0} tone={item.membership === 'joined' ? 'pink' : 'accent'} /></div>
                        </td>
                        <td className="px-4 py-2.5"><Badge tone={item.membership === 'joined' ? 'green' : item.membership === 'left' ? 'red' : 'neutral'}>{membershipLabels[item.membership] || item.membership}</Badge></td>
                        <td className="px-4 py-2.5">
                          <Badge tone={item.response_enabled ? 'green' : 'neutral'} dot>{item.response_enabled ? '开启' : '关闭'} · {responseSourceLabels[item.response_source] || item.response_source}</Badge>
                        </td>
                        <td className="px-4 py-2.5">
                          <span className="block font-mono text-[11.5px] text-fg-2">{item.binding?.agent || '继承平台'}</span>
                          {(item.binding?.provider || item.binding?.source) && (
                            <span className="block text-[10.5px] text-fg-4">
                              {[item.binding?.provider === 'edictum' ? 'Edictum' : item.binding?.provider === 'session_class' ? 'SessionClass' : item.binding?.provider,
                                item.binding?.source ? bindingSourceLabels[item.binding.source] : ''].filter(Boolean).join(' · ')}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-2.5">
                          <span className="text-[11.5px] text-fg-3">{item.last_activity_at ? formatRelativeTime(item.last_activity_at) : '—'}</span>
                          {item.activity_coverage && item.activity_coverage.complete === false && <span className="block text-[10px] text-fg-4">覆盖不完整</span>}
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <Link className="ctl ctl-ghost ctl-sm inline-flex" to={destination(item.group_id)}>进入 <ChevronRight className="h-3.5 w-3.5" /></Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {rows.length === 0 && (
                <div className="p-4">
                  <EmptyState tint="pink" icon={<Users className="h-6 w-6" />} title={emptyMessage}
                    description="同步完成后才能确认机器人当前加入了哪些群; 旧目录在同步失败时会保留。"
                    actions={<>
                      <Button onClick={() => updateParams({ q: null, membership: 'all', response: 'all', page: null })}>清除筛选</Button>
                      <Button variant="tinted" tint="pink" onClick={beginSync} disabled={syncing || !isRunning || historical}>重新同步</Button>
                    </>} />
                </div>
              )}
              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-4 py-2.5">
                <div className="flex items-center gap-2 text-[11.5px] text-fg-3">
                  <span>每页</span>
                  <select className="field !h-7 !w-[66px] !rounded-lg !px-2 !text-[12px]" value={String(pageSize)} aria-label="每页条数"
                    onChange={(event) => updateParams({ pageSize: event.target.value, page: null })}>
                    {[25, 50, 100].map((value) => <option key={value} value={value}>{value}</option>)}
                  </select>
                  <span>共 {visibleListing.total} 项</span>
                </div>
                <Pager page={page} totalPages={totalPages} onChange={(next) => updateParams({ page: String(next) })} />
              </div>
            </Card>
          ) : null}
        </>
      ) : null}

      <Modal open={showMode} onClose={closeMode} title="群接入模式" icon={<Settings2 className="h-[18px] w-[18px]" />}
        description="逐群例外会保留; 新加入的群跟随此默认" size="md" className="tint-pink"
        footer={<>
          <Button variant="ghost" onClick={closeMode}>取消</Button>
          <Button variant="primary" loading={modeSaving} onClick={saveMode} disabled={!settings || modeDraft === settings.mode}>保存模式</Button>
        </>}>
        <div className="space-y-3">
          {([
            ['selected', '仅所选群', '新加入的群默认不响应, 需要逐群开启或绑定 Agent。'],
            ['all', '全部群, 含以后新加入群', '以后新加入的群默认响应; 逐群关闭仍然有效。'],
          ] as const).map(([value, label, description]) => (
            <label key={value} className={`flex w-full cursor-pointer items-start gap-3 rounded-xl border p-3 text-left transition ${
              modeDraft === value ? 'border-[rgb(var(--rgb-pink)/0.4)] bg-[rgb(var(--rgb-pink)/0.1)]' : 'border-line bg-glass hover:bg-glass-hover'}`}>
              <input type="radio" name="group-mode" className="sr-only" checked={modeDraft === value} onChange={() => setModeDraft(value)} />
              <span className={`mt-0.5 grid h-4 w-4 flex-none place-items-center rounded-full border ${modeDraft === value ? 'border-pink' : 'border-[var(--glass-border-strong)]'}`}>
                {modeDraft === value && <span className="h-2 w-2 rounded-full bg-pink" />}
              </span>
              <span>
                <span className="block text-[13px] font-medium text-fg">{label}</span>
                <span className="mt-0.5 block text-[12px] text-fg-3">{description}</span>
              </span>
            </label>
          ))}
          <Note tone="orange">切换模式不会改变已有群的手动开关或已保存的群级 Agent 绑定。</Note>
        </div>
      </Modal>

      <Modal open={showApproval} onClose={closeApproval} title="平台群管理默认审批" icon={<ShieldCheck className="h-[18px] w-[18px]" />}
        description="只影响继承本账号默认值的群和以后新提交的动作; 已有待审批请求不会自动执行" size="lg" className="tint-pink"
        footer={<>
          <Button variant="ghost" onClick={closeApproval}>取消</Button>
          <Button variant="primary" loading={approvalSaving} onClick={saveApproval} disabled={!approvalDirty || approvalSaving || approvalConflict}>保存默认设置</Button>
        </>}>
        <div className="space-y-3">
          {approvalError && <Note tone="red" title="无法保存">{approvalError}</Note>}
          {approvalConflict && (
            <Note tone="orange" title="账号设置已变化, 草稿仍保留" action={<>
              <Button size="sm" onClick={async () => {
                if (!account) return;
                try { setSettings(await groupApi.settings(adapterId, account, isRunning)); }
                catch (caught) { setApprovalError(errorText(caught)); }
              }}>查看服务器最新值</Button>
              <Button size="sm" variant="ghost" onClick={() => { void navigator.clipboard?.writeText(JSON.stringify(approvalDraft, null, 2)); toast.success('草稿已复制'); }}>复制我的草稿</Button>
              <Button size="sm" variant="ghost" onClick={async () => {
                if (!account) return;
                try {
                  const latest = await groupApi.settings(adapterId, account, isRunning);
                  const fresh = defaultsFrom(latest);
                  setSettings(latest); setApprovalDraft(fresh); setApprovalBaseline(JSON.stringify(fresh));
                  setApprovalConflict(false); setApprovalError('');
                } catch (caught) { setApprovalError(errorText(caught)); }
              }}>放弃草稿并重新加载</Button>
            </>}>
              不会自动覆盖其他管理员的修改。可以复制草稿后重新合并。
            </Note>
          )}
          <div className="grid gap-2 sm:grid-cols-2">
            {(settings?.approval_actions || []).map((item) => (
              <label key={item.action_type} className="block rounded-xl border border-line bg-glass p-3">
                <span className="text-[13px] font-medium text-fg">{groupActionLabels[item.action_type] || item.action_type}</span>
                <Badge tone={item.risk === 'high' ? 'red' : 'neutral'} className="ml-1.5">{item.risk === 'high' ? '高影响' : '普通'}</Badge>
                <span className="ml-1.5 text-[11px] text-fg-4">当前继承群 {settings?.approval_inheriting_counts?.[item.action_type] ?? 0} · 当前{approvalDraft[item.action_type] === 'approval_required' ? '需审批' : approvalDraft[item.action_type] === 'auto_execute' ? '自动执行' : '系统默认'}</span>
                <select className="field mt-2 !h-8 !text-[12px]" value={approvalDraft[item.action_type] || 'inherit'}
                  disabled={approvalSaving || approvalConflict}
                  onChange={(event) => setApprovalDraft((old) => ({ ...old, [item.action_type]: event.target.value as ApprovalDraft[string] }))}>
                  <option value="inherit">系统默认</option><option value="approval_required">需审批</option><option value="auto_execute">自动执行</option>
                </select>
              </label>
            ))}
          </div>
          <p className="text-[11.5px] text-fg-4">{Object.values(approvalDraft).filter((value) => value !== 'inherit').length} 项显式设置 · 其余动作继承系统默认。</p>
        </div>
      </Modal>

      <AccountInvitations key={`${adapterId}:${account}:${isRunning}:${historical}`} adapterId={adapterId} account={account}
        open={showInvitations && !!account && isRunning && !historical} onClose={() => setShowInvitations(false)} />
    </div>
  );
}
