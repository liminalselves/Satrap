import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupAccountsResult, type GroupListResult, type GroupRow, type GroupSettings } from '@/api/groups';
import { PageHeader } from '@/components/common/PageHeader';
import { Card } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Modal } from '@/components/ui/Modal';
import { useBackendStore } from '@/stores/useBackendStore';
import { confirmDiscard, useDirtyGuard } from '@/hooks/useDirtyGuard';
import { groupActionLabels } from './groupActionLabels';

const MEMBERSHIPS = [
  ['joined', '已加入'], ['all', '全部'], ['left', '已离开'],
  ['unknown', '待确认'], ['config_only', '仅配置未发现'],
] as const;
const RESPONSES = [['all', '全部响应状态'], ['enabled', '响应开启'], ['disabled', '响应关闭']] as const;
type ApprovalDraft = Record<string, 'inherit' | 'approval_required' | 'auto_execute'>;

function defaultsFrom(settings: GroupSettings): ApprovalDraft {
  return Object.fromEntries(settings.approval_actions.map((item) => [item.action_type,
    settings.approval_defaults[item.action_type] || 'inherit']));
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.code ? `${error.message} (${error.code})` : error.message;
  return error instanceof Error ? error.message : '请求失败';
}

function timeText(value: number | null): string {
  return value == null ? '无记录' : new Date(value * 1000).toLocaleString();
}

function GroupItem({ item, destination }: { item: GroupRow; destination: string }) {
  const status = item.response_enabled ? '开启' : '关闭';
  return (
    <div className="flex items-center justify-between gap-3 border-b border-glass-border px-3 py-3 last:border-0">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate font-medium text-text-primary">{item.group_name || `群 ${item.group_id}`}</span>
          <Badge variant={item.response_enabled ? 'success' : 'default'}>{status}</Badge>
        </div>
        <p className="mt-1 text-xs text-text-secondary">{item.group_id} · {item.membership === 'joined' ? '已加入' : item.membership === 'left' ? '已离开' : item.membership === 'config_only' ? '仅配置' : '待确认'}</p>
      </div>
      <Link className="glass-button px-3 py-1.5 text-sm shrink-0" to={destination}>进入</Link>
    </div>
  );
}

export function Groups() {
  const { adapterId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const { isRunning, refreshAll } = useBackendStore();
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
  const [showApproval, setShowApproval] = useState(false);
  const [approvalDraft, setApprovalDraft] = useState<ApprovalDraft>({});
  const [approvalBaseline, setApprovalBaseline] = useState('{}');
  const [approvalSaving, setApprovalSaving] = useState(false);
  const [approvalError, setApprovalError] = useState('');
  const [approvalConflict, setApprovalConflict] = useState(false);
  const [syncId, setSyncId] = useState('');
  const [syncTicks, setSyncTicks] = useState(0);
  const listRequest = useRef(0);
  const accountRequest = useRef(0);
  const settingsRequest = useRef(0);
  const accountOptions = accounts?.items || [];
  const historical = !!account && !!accounts && account !== accounts.current_account;
  const waiting = !!accounts && !accounts.current_account;
  const visibleListing = listing?.account === account ? listing : null;
  const visibleSettings = settings?.self_id === account ? settings : null;
  const approvalDirty = JSON.stringify(approvalDraft) !== approvalBaseline;

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
      setSyncTicks(0);
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
    } catch (caught) { setError(errorText(caught)); }
    finally { setModeSaving(false); }
  }, [settings, account, modeSaving, adapterId, modeDraft, isRunning, loadList]);
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
      setSyncTicks(0);
      await loadList();
    } catch (caught) { setError(errorText(caught)); }
  }, [account, historical, isRunning, adapterId, loadList]);

  useEffect(() => {
    if (!syncId || syncTicks >= 30) return;
    const timer = window.setInterval(async () => {
      if (document.hidden) return;
      try {
        const status = await groupApi.syncStatus(adapterId, account, syncId);
        setSyncTicks((value) => value + 1);
        if (status.status !== 'running') {
          setSyncId('');
          await loadList();
        }
      } catch (caught) { setError(errorText(caught)); setSyncId(''); }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [adapterId, account, syncId, syncTicks, loadList]);

  const sync = visibleListing?.sync;
  const totalPages = Math.max(1, Math.ceil((visibleListing?.total || 0) / pageSize));
  const emptyMessage = useMemo(() => {
    if (sync?.status === 'never') return '尚未同步群列表';
    if (sync?.status === 'complete' && !q && membership === 'joined') return '当前账号未加入任何群';
    return '没有符合筛选条件的群';
  }, [sync?.status, q, membership]);

  if (!adapterId) return <Card>平台地址无效</Card>;

  return (
    <div className="space-y-5">
      <PageHeader title="群聊管理" description={`平台 ${adapterId}`} actions={
        <Link className="glass-button px-4 py-2 text-sm" to="/platforms">平台设置</Link>
      } />
      {loading && !visibleListing && !accounts ? <Card className="animate-pulse min-h-36" aria-label="群目录加载中" /> : null}
      {error && <Card className="border border-error text-error" role="alert">{error}</Card>}
      {waiting && !account ? <Card>等待机器人连接并确认账号。确认前不会显示假群列表或启用任何群</Card> : null}
      {accounts && accountOptions.length > 0 && (
        <Card className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <label className="min-w-36 flex-1 text-sm text-text-secondary">查看账号
              <select className="glass-input mt-1 w-full" value={account} onChange={(event) => updateParams({ account: event.target.value, page: null })}>
                {accountOptions.map((item) => <option key={item.self_id} value={item.self_id}>{item.self_id}{item.self_id === accounts.current_account ? ' · 当前' : ' · 历史'}</option>)}
              </select>
            </label>
            <Button onClick={beginSync} disabled={!isRunning || historical || !!syncId || sync?.status === 'running'} title={historical ? '历史账号只读' : !isRunning ? '平台离线' : undefined}>
              <RefreshCw className="mr-2 inline h-4 w-4" />同步群列表
            </Button>
            <Button variant="subtle" onClick={openMode} disabled={historical}>修改接入模式</Button>
            <Button variant="subtle" onClick={openApproval} disabled={historical}>管理默认设置</Button>
          </div>
          {historical && <p className="text-sm text-text-secondary">当前连接账号不同, 历史账号只读。账号选择只切换查看数据</p>}
          <div className="flex flex-wrap gap-3 text-sm text-text-secondary" aria-live="polite">
            <span>接入模式: {visibleSettings?.mode === 'all' ? '全部群, 含以后新加入群' : visibleSettings?.mode === 'selected' ? '仅所选群' : '读取中'}</span>
            <span>已加入 {visibleListing?.counts.joined ?? '—'}</span>
            <span>响应开启 {visibleListing?.counts.response_enabled ?? '—'}</span>
            <span>上次完整同步: {timeText(sync?.last_complete_at ?? null)}</span>
          </div>
          {sync?.status === 'failed' || sync?.status === 'partial' ? (
            <p className="text-sm text-warning" role="status">同步{sync.status === 'failed' ? '失败' : '不完整'}, 旧目录已保留, 未据此判断退群。原因: {sync.reason || '未知'}</p>
          ) : null}
        </Card>
      )}
      {account && (
        <>
          <Card className="grid gap-3 md:grid-cols-4">
            <label className="text-sm text-text-secondary md:col-span-2">搜索群名或群号
              <Input className="mt-1" value={searchText} onChange={(event) => setSearchText(event.target.value)} />
            </label>
            <label className="text-sm text-text-secondary">成员关系
              <select className="glass-input mt-1 w-full" value={membership} onChange={(event) => updateParams({ membership: event.target.value, page: null })}>
                {MEMBERSHIPS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label className="text-sm text-text-secondary">响应状态
              <select className="glass-input mt-1 w-full" value={response} onChange={(event) => updateParams({ response: event.target.value, page: null })}>
                {RESPONSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
          </Card>
          {loading && !visibleListing ? <Card className="animate-pulse min-h-48" aria-label="群列表加载中" /> : null}
          {visibleListing && (
            <>
              <div className="md:hidden"><Card>
                {visibleListing.items.length ? visibleListing.items.map((item) => <GroupItem key={item.group_id} item={item} destination={`/platforms/${encodeURIComponent(adapterId)}/groups/${encodeURIComponent(item.group_id)}/overview?account=${encodeURIComponent(account)}`} />) : <p className="p-5 text-center text-text-secondary">{emptyMessage}</p>}
              </Card></div>
              <Card className="hidden md:block">
                <table className="w-full text-sm">
                  <thead><tr className="text-left text-text-secondary"><th className="p-3">群名 / 群号</th><th className="p-3">成员数</th><th className="p-3">关系</th><th className="p-3">响应</th><th className="p-3">操作</th></tr></thead>
                  <tbody>{visibleListing.items.map((item) => <tr key={item.group_id} className="border-t border-glass-border">
                    <td className="p-3"><span className="font-medium text-text-primary">{item.group_name || '未命名群'}</span><br /><span className="text-xs text-text-secondary">{item.group_id}</span></td>
                    <td className="p-3">{item.member_count ?? '—'} / {item.max_member_count ?? '—'}</td>
                    <td className="p-3">{item.membership === 'joined' ? '已加入' : item.membership === 'left' ? '已离开' : item.membership === 'config_only' ? '仅配置' : '待确认'}</td>
                    <td className="p-3"><Badge variant={item.response_enabled ? 'success' : 'default'}>{item.response_enabled ? '开启' : '关闭'} · {item.response_source === 'group' ? '本群' : item.response_source === 'platform' ? '平台总开关' : '账号'}</Badge></td>
                    <td className="p-3"><Link className="text-accent hover:underline" to={`/platforms/${encodeURIComponent(adapterId)}/groups/${encodeURIComponent(item.group_id)}/overview?account=${encodeURIComponent(account)}`}>进入</Link></td>
                  </tr>)}</tbody>
                </table>
                {!visibleListing.items.length && <p className="p-6 text-center text-text-secondary">{emptyMessage}</p>}
              </Card>
              <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-text-secondary" aria-live="polite">
                <span>共 {visibleListing.total} 项 {refreshing ? '· 刷新中' : ''}</span>
                <div className="flex items-center gap-2">
                  <label>每页 <select className="glass-input" value={pageSize} onChange={(event) => updateParams({ pageSize: event.target.value, page: null })}>
                    {[25, 50, 100].map((value) => <option key={value} value={value}>{value}</option>)}
                  </select></label>
                  <Button size="sm" disabled={page <= 1} onClick={() => updateParams({ page: String(page - 1) })}>上一页</Button>
                  <span>{page} / {totalPages}</span>
                  <Button size="sm" disabled={page >= totalPages} onClick={() => updateParams({ page: String(page + 1) })}>下一页</Button>
                </div>
              </div>
            </>
          )}
        </>
      )}
      <Modal open={showMode} onClose={closeMode} title="群接入模式">
        <div className="space-y-4 text-sm text-text-secondary">
          <p>逐群例外会保留。切换为全部群后, 以后新加入的群默认响应; 切换为仅所选群后, 新群默认不响应</p>
          <label className="flex items-center gap-2"><input type="radio" name="group-mode" checked={modeDraft === 'selected'} onChange={() => setModeDraft('selected')} />仅所选群</label>
          <label className="flex items-center gap-2"><input type="radio" name="group-mode" checked={modeDraft === 'all'} onChange={() => setModeDraft('all')} />全部群, 含以后新加入群</label>
          <div className="flex justify-end gap-2"><Button variant="subtle" onClick={closeMode}>取消</Button><Button variant="primary" onClick={saveMode} disabled={modeSaving || !settings}>保存模式</Button></div>
        </div>
      </Modal>
      <Modal open={showApproval} onClose={closeApproval} title="平台群管理默认审批" size="lg">
        <div className="space-y-4 text-sm text-text-secondary">
          <p>只影响继承本账号默认值的群和以后新提交的动作。已有待审批请求不会自动执行</p>
          {approvalError && <p className="text-error" role="alert">{approvalError}</p>}
          {approvalConflict && <div className="space-y-2 rounded-lg border border-warning p-3" role="alert">
            <p>账号设置已变化, 草稿仍保留</p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={async () => {
                if (!account) return;
                try { setSettings(await groupApi.settings(adapterId, account, isRunning)); }
                catch (caught) { setApprovalError(errorText(caught)); }
              }}>查看服务器最新值</Button>
              <Button size="sm" variant="subtle" onClick={() => navigator.clipboard.writeText(JSON.stringify(approvalDraft, null, 2))}>复制我的草稿</Button>
              <Button size="sm" variant="subtle" onClick={async () => {
                if (!account) return;
                try {
                  const latest = await groupApi.settings(adapterId, account, isRunning);
                  const fresh = defaultsFrom(latest);
                  setSettings(latest); setApprovalDraft(fresh); setApprovalBaseline(JSON.stringify(fresh));
                  setApprovalConflict(false); setApprovalError('');
                } catch (caught) { setApprovalError(errorText(caught)); }
              }}>放弃草稿并重新加载</Button>
            </div>
          </div>}
          <div className="grid gap-3 md:grid-cols-2">
            {(settings?.approval_actions || []).map((item) => <label key={item.action_type} className="block rounded-lg border border-glass-border p-3">
              <span className="font-medium text-text-primary">{groupActionLabels[item.action_type] || item.action_type}</span>
              <span className="ml-2">{item.risk === 'high' ? '高影响' : '普通'} · 当前继承群 {settings?.approval_inheriting_counts?.[item.action_type] ?? 0}</span>
              <select className="glass-input mt-2 w-full" value={approvalDraft[item.action_type] || 'inherit'}
                disabled={approvalSaving || approvalConflict}
                onChange={(event) => setApprovalDraft((old) => ({ ...old, [item.action_type]: event.target.value as ApprovalDraft[string] }))}>
                <option value="inherit">系统默认</option><option value="approval_required">需审批</option><option value="auto_execute">自动执行</option>
              </select>
            </label>)}
          </div>
          <div className="flex justify-end gap-2"><Button variant="subtle" onClick={closeApproval}>取消</Button>
            <Button variant="primary" onClick={saveApproval} disabled={!approvalDirty || approvalSaving || approvalConflict}>保存默认设置</Button></div>
        </div>
      </Modal>
    </div>
  );
}
