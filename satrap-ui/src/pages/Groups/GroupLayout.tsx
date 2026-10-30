import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, Navigate, NavLink, Outlet, useLocation, useOutletContext, useParams, useSearchParams } from 'react-router-dom';
import { AlertTriangle, ArrowLeft, ChevronRight, Clock3, RefreshCw, ShieldCheck } from 'lucide-react';
import { groupApi, type GroupConfigResult } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { PageHeader } from '@/components/kit/Feedback';
import { toast } from '@/components/kit/Toast';
import { useBackendStore } from '@/stores/useBackendStore';
import { errorText } from '@/utils/errorText';
import { VersionedConfigRead } from '@/api/configVersions';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { getApiBaseUrl, getControlApiUrl } from '@/utils/constants';
import { Avatar, CopyValue, Note, ProgressBar, hueFor } from './shared';

const sections = [
  ['overview', '概览'], ['policy', '响应策略'], ['session', '会话配置'],
  ['members', '成员'], ['manage', '群管理'], ['events', '事件与诊断'],
  ['actions', '动作记录'],
] as const;

interface GroupContext {
  adapterId: string;
  groupId: string;
  account: string;
  isRunning: boolean;
  historical: boolean;
  config: GroupConfigResult;
  setConfig: (value: GroupConfigResult) => void;
  reload: () => Promise<void>;
}

export function useGroupContext(): GroupContext {
  return useOutletContext<GroupContext>();
}

export function GroupDefault() {
  const location = useLocation();
  return <Navigate to={`overview${location.search}`} replace />;
}

export function GroupInvalidSection() {
  const { adapterId, account } = useGroupContext();
  const listPath = `/platforms/${encodeURIComponent(adapterId)}/groups?account=${encodeURIComponent(account)}`;
  return <Card role="alert" padding="lg">
    <Note tone="orange" title="群详情标签不存在" action={<Link className="ctl ctl-sm" to={listPath}>返回群列表</Link>}>
      该分区已不存在, 请选择上方标签。
    </Note>
  </Card>;
}

const applyText = (config: GroupConfigResult) => config.apply_status === 'applied' ? '已生效'
  : config.apply_status === 'failed' ? '应用失败' : '已保存, 待应用';

export function GroupLayout() {
  const { adapterId = '', groupId = '' } = useParams();
  const location = useLocation();
  const lastSegment = location.pathname.split('/').filter(Boolean).slice(-1)[0] || '';
  const section = lastSegment === groupId ? 'overview' : lastSegment;
  const [params, setParams] = useSearchParams();
  const account = params.get('account') || '';
  const isRunning = useBackendStore((state) => state.isRunning);
  const adapterOnline = useBackendStore((state) => state.health?.adapters?.[adapterId]?.status === 'running');
  const [config, setConfig] = useState<GroupConfigResult | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [applying, setApplying] = useState(false);
  const request = useRef(0);
  const configRead = useMemo(() => new VersionedConfigRead([], (signal) =>
    groupApi.config(adapterId, groupId, account, isRunning, signal), isRunning ? getApiBaseUrl : getControlApiUrl), [adapterId, groupId, account, isRunning]);
  const currentRead = useRef(configRead);
  currentRead.current = configRead;
  const historical = !!config && account !== config.current_account;
  const listPath = `/platforms/${encodeURIComponent(adapterId)}/groups?account=${encodeURIComponent(account)}`;

  const reload = useCallback(async (signal?: AbortSignal) => {
    if (!account || configRead !== currentRead.current) return;
    const current = ++request.current;
    try {
      const result = await configRead.read({ signal });
      if (current !== request.current || signal?.aborted) return;
      setConfig(result);
      setError('');
    } catch (caught) {
      if (current === request.current && !signal?.aborted) { setError(errorText(caught)); throw caught; }
    } finally {
      if (current === request.current) setLoading(false);
    }
  }, [account, configRead]);

  const acceptConfig = useCallback((value: GroupConfigResult) => {
    if (configRead !== currentRead.current) return;
    request.current++; configRead.invalidate();
    setConfig(value); setLoading(false);
  }, [configRead]);
  const reloadConfig = useCallback(() => reload().catch(() => undefined), [reload]);

  useEffect(() => {
    const controller = new AbortController();
    if (account) { setLoading(true); void reload(controller.signal).catch(() => undefined); }
    else {
      groupApi.accounts(adapterId, isRunning, controller.signal).then((result) => {
        if (controller.signal.aborted) return;
        if (result.current_account) {
          // 函数式更新保留其它查询参数, 避免把 params 拉进依赖导致每次 URL 变化都重读
          setParams((previous) => {
            const next = new URLSearchParams(previous);
            next.set('account', result.current_account!);
            return next;
          }, { replace: true });
        } else setLoading(false);
      }).catch((caught) => { if (!controller.signal.aborted) { setError(errorText(caught)); setLoading(false); } });
    }
    return () => { request.current += 1; controller.abort(); };
  }, [account, adapterId, isRunning, reload, setParams]);
  useEffect(() => {
    if (!account || !isRunning) return;
    const poll = startVisiblePoll(reload, { interval: 5000, immediate: false });
    return () => { request.current++; poll.stop(); };
  }, [account, isRunning, reload]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await reload();
      toast.success('群配置已刷新', '已重新读取有效策略与会话路由');
    } catch { /* 错误已写入页面状态 */ }
    finally { setRefreshing(false); }
  }, [reload]);

  const retryApply = useCallback(async () => {
    if (!config || !isRunning || historical || applying) return;
    setApplying(true);
    try {
      setConfig(await groupApi.applyConfig(adapterId, groupId, account, config.saved_revision));
      setError('');
      toast.success('配置已重新应用');
    } catch (caught) { setError(errorText(caught)); }
    finally { setApplying(false); }
  }, [config, isRunning, historical, applying, adapterId, groupId, account]);

  if (!account && !loading) return <Card>等待机器人确认账号。<Link className="text-accent" to={listPath}>返回群列表</Link></Card>;
  if (loading && !config) return <Card className="min-h-52 animate-pulse" aria-label="群详情加载中" />;
  if (!config || config.account !== account || config.group.group_id !== groupId) {
    return <Card role="alert">{error || '群记录不存在'}。<Link className="text-accent" to={listPath}>返回群列表</Link></Card>;
  }

  const group = config.group;
  const title = group.group_name || `群 ${groupId}`;
  const membership = group.membership === 'joined' ? '已加入' : group.membership === 'left' ? '已离开' : group.membership === 'config_only' ? '仅保留配置' : '待确认';
  const enabled = config.effective.policy.enabled === true;
  const binding = config.effective.session.binding as { provider?: string; config_name?: string } | undefined;
  const memberRatio = group.member_count && group.max_member_count ? (group.member_count / group.max_member_count) * 100 : 0;
  const scope = String(config.effective.session.scope ?? '');
  const scopeLabel = scope === 'group_shared' ? '全群共享' : scope === 'group_member' ? '按成员隔离' : '平台默认';
  const validSection = sections.some(([key]) => key === section);

  return (
    <div className="space-y-5">
      <PageHeader title={title}
        description={<>平台 <span className="font-mono text-fg-2">{adapterId}</span> / 机器人 <span className="font-mono text-fg-2">{account}</span> / 群 <span className="font-mono text-fg-2">{groupId}</span></>}
        eyebrow={<div className="flex flex-wrap items-center gap-2 text-[12px] text-fg-3">
          <Link to="/platforms" className="transition hover:text-pink">平台状态</Link>
          <ChevronRight className="h-3 w-3 text-fg-4" />
          <Link to={listPath} className="transition hover:text-pink">群聊管理</Link>
          <ChevronRight className="h-3 w-3 text-fg-4" />
          <span className="num font-mono text-fg-2">{groupId}</span>
        </div>}
        actions={<>
          <Link to={listPath} className="ctl ctl-ghost"><ArrowLeft className="h-4 w-4" />返回群列表</Link>
          <Button variant="ghost" icon={<RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin-slow' : ''}`} />} onClick={refresh} disabled={refreshing}>刷新配置</Button>
        </>} />

      {error && <Note tone="red" title="读取失败" action={<Button size="sm" onClick={() => void reload().catch(() => undefined)}>重试读取</Button>}>{error}</Note>}

      <Card padding="none" tint={config.apply_status === 'failed' ? 'red' : 'pink'} className="relative overflow-hidden animate-rise">
        <div aria-hidden className="pointer-events-none absolute -right-20 -top-24 h-72 w-72 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.28),transparent_66%)] blur-2xl" />
        <div className="relative flex flex-col gap-5 p-5 lg:flex-row lg:items-center">
          <div className="flex min-w-0 items-center gap-4">
            <Avatar name={title} hue={hueFor(groupId)} size="xl" square />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone={group.membership === 'joined' ? 'green' : group.membership === 'left' ? 'red' : 'neutral'} dot>{membership}</Badge>
                <Badge tone={enabled ? 'green' : 'neutral'} dot>{enabled ? '响应开启' : '响应关闭'}</Badge>
                <Badge tone={config.apply_status === 'applied' ? 'green' : config.apply_status === 'failed' ? 'red' : 'orange'}>{applyText(config)}</Badge>
                {adapterOnline && <Badge tone="pink" dot live>适配器在线</Badge>}
              </div>
              <h2 className="display mt-1.5 truncate text-[24px] font-semibold text-fg">{title}</h2>
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-fg-3">
                <span className="num font-mono">群 {group.group_id}</span>
                <span>·</span>
                <span>确认于 {group.confirmed_at ? new Date(group.confirmed_at * 1000).toLocaleString('zh-CN') : '未知'}</span>
                <CopyValue value={group.group_id} label="复制群号" />
              </div>
            </div>
          </div>
          <div className="grid flex-1 grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="rounded-xl border border-line bg-glass px-3 py-2">
              <div className="text-[10.5px] text-fg-4">成员</div>
              <div className="num text-[15px] font-semibold text-fg">{group.member_count ?? '—'}<span className="text-[11px] font-normal text-fg-3"> / {group.max_member_count ?? '—'}</span></div>
              <ProgressBar value={memberRatio} tone="pink" className="mt-1.5" />
            </div>
            <div className="rounded-xl border border-line bg-glass px-3 py-2">
              <div className="text-[10.5px] text-fg-4">绑定 Agent</div>
              <div className="truncate text-[12px] font-medium text-fg">{binding?.config_name || '继承平台'}</div>
              <div className="truncate text-[10.5px] text-fg-4">{binding?.provider === 'edictum' ? 'Edictum' : binding?.provider === 'session_class' ? 'SessionClass' : '未指定'}</div>
            </div>
            <div className="rounded-xl border border-line bg-glass px-3 py-2">
              <div className="text-[10.5px] text-fg-4">会话范围</div>
              <div className="truncate text-[12px] font-medium text-fg">{scopeLabel}</div>
              <div className="truncate text-[10.5px] text-fg-4">可归属实例 {config.session_instances?.current_route_count ?? '—'}</div>
            </div>
            <div className="rounded-xl border border-line bg-glass px-3 py-2">
              <div className="text-[10.5px] text-fg-4">配置修订</div>
              <div className="num text-[15px] font-semibold text-fg">#{config.saved_revision}</div>
              <div className="text-[10.5px] text-fg-4">路由代次 {config.route_generation}</div>
            </div>
          </div>
        </div>
        {config.apply_status === 'failed' && (
          <div className="relative px-5 pb-4">
            <Note tone="red" title="配置应用失败" icon={<AlertTriangle className="h-4 w-4" />}
              action={<Button size="sm" variant="danger" loading={applying} onClick={retryApply} disabled={!isRunning || historical}>重试应用</Button>}>
              {config.apply_error || '未提供失败原因'}
            </Note>
          </div>
        )}
        {historical && <div className="relative px-5 pb-4"><Note tone="orange">当前连接账号不同, 此账号的群配置只读。</Note></div>}
        {!isRunning && (
          <div className="relative px-5 pb-4"><Note tone="orange">{historical
            ? '后端离线或未确认绑定账号, 此页仅可查看历史快照。'
            : '后端离线。可保存冷配置, 网络动作暂不可执行。'}</Note></div>
        )}
        <div className="relative -mx-1 overflow-x-auto border-t border-line px-4 py-2.5">
          <nav className="flex max-w-full items-center gap-0.5 overflow-x-auto rounded-[12px] border border-line bg-glass p-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" aria-label="群详情标签">
            {sections.map(([key, label]) => {
              const active = (validSection ? section : 'overview') === key;
              return (
                <NavLink key={key} to={`/platforms/${encodeURIComponent(adapterId)}/groups/${encodeURIComponent(groupId)}/${key}?account=${encodeURIComponent(account)}`}
                  // 动作记录同时是审批与执行记录入口, 保留可访问名以兼容既有导航契约
                  aria-label={key === 'actions' ? '审批与记录' : undefined}
                  className={`relative inline-flex h-8 shrink-0 items-center gap-2 whitespace-nowrap rounded-[9px] px-3.5 text-sm font-medium transition-colors duration-200 ${
                    active ? 'border border-[rgb(var(--rgb-accent)/0.28)] bg-[rgb(var(--rgb-accent)/0.14)] text-accent shadow-[inset_0_1px_0_var(--glass-highlight)]' : 'text-fg-3 hover:text-fg'}`}>
                  {label}
                  {key === 'members' && group.member_count !== null && (
                    <span className={`num rounded-full px-1.5 text-[11px] leading-[18px] ${active ? 'bg-[rgb(var(--rgb-accent)/0.2)]' : 'bg-glass-hover text-fg-3'}`}>{group.member_count}</span>
                  )}
                </NavLink>
              );
            })}
          </nav>
        </div>
      </Card>

      <Outlet context={{ adapterId, groupId, account, isRunning, historical, config, setConfig: acceptConfig, reload: reloadConfig } satisfies GroupContext} />

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3 text-[11px] text-fg-4">
        <span className="flex items-center gap-1.5"><Clock3 className="h-3 w-3" />配置修订 {config.saved_revision} · 生效 {config.active_revision ?? '无'} · 每 5 秒自动刷新有效设置</span>
        <div className="flex items-center gap-1.5">
          <ShieldCheck className="h-3 w-3" />写操作使用修订号避免覆盖并发修改
        </div>
      </div>
    </div>
  );
}
