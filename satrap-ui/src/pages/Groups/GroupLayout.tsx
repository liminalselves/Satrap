import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, Navigate, NavLink, Outlet, useLocation, useOutletContext, useParams, useSearchParams } from 'react-router-dom';
import { ApiError } from '@/api/client';
import { groupApi, type GroupConfigResult } from '@/api/groups';
import { PageHeader } from '@/components/common/PageHeader';
import { Card } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { useBackendStore } from '@/stores/useBackendStore';

const sections = [
  ['overview', '概览'], ['policy', '响应策略'], ['session', '会话配置'],
  ['members', '成员'], ['manage', '群管理'], ['events', '事件与诊断'],
  ['actions', '审批与记录'],
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
  return <Card role="alert">群详情标签不存在。<Link className="text-accent" to={listPath}>返回群列表</Link></Card>;
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '群配置读取失败';
}

export function GroupLayout() {
  const { adapterId = '', groupId = '' } = useParams();
  const location = useLocation();
  const lastSegment = location.pathname.split('/').filter(Boolean).slice(-1)[0] || '';
  const section = lastSegment === groupId ? 'overview' : lastSegment;
  const [params, setParams] = useSearchParams();
  const account = params.get('account') || '';
  const { isRunning } = useBackendStore();
  const [config, setConfig] = useState<GroupConfigResult | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const request = useRef(0);
  const historical = !!config && account !== config.current_account;
  const listPath = `/platforms/${encodeURIComponent(adapterId)}/groups?account=${encodeURIComponent(account)}`;

  const reload = useCallback(async () => {
    if (!account) return;
    const current = ++request.current;
    try {
      const result = await groupApi.config(adapterId, groupId, account, isRunning);
      if (current !== request.current) return;
      setConfig(result);
      setError('');
    } catch (caught) {
      if (current === request.current) setError(errorText(caught));
    } finally {
      if (current === request.current) setLoading(false);
    }
  }, [adapterId, groupId, account, isRunning]);

  useEffect(() => {
    if (account) { setLoading(true); reload(); }
    else {
      groupApi.accounts(adapterId, isRunning).then((result) => {
        if (result.current_account) {
          const next = new URLSearchParams(params);
          next.set('account', result.current_account);
          setParams(next, { replace: true });
        } else setLoading(false);
      }).catch((caught) => { setError(errorText(caught)); setLoading(false); });
    }
    return () => { request.current += 1; };
  }, [account, adapterId, isRunning, reload, params, setParams]);
  useEffect(() => {
    if (!account || !isRunning) return;
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') void reload();
    }, 5_000);
    return () => window.clearInterval(interval);
  }, [account, isRunning, reload]);

  if (!account && !loading) return <Card>等待机器人确认账号。<Link className="text-accent" to={listPath}>返回群列表</Link></Card>;
  if (loading && !config) return <Card className="min-h-52 animate-pulse" aria-label="群详情加载中" />;
  if (!config || config.account !== account || config.group.group_id !== groupId) {
    return <Card role="alert">{error || '群记录不存在'}。<Link className="text-accent" to={listPath}>返回群列表</Link></Card>;
  }

  const group = config.group;
  const title = group.group_name || `群 ${groupId}`;
  const membership = group.membership === 'joined' ? '已加入' : group.membership === 'left' ? '已离开' : group.membership === 'config_only' ? '仅保留配置' : '待确认';
  const enabled = config.effective.policy.enabled === true;
  return (
    <div className="space-y-5">
      <PageHeader title={title} description={`平台 ${adapterId} / 机器人 ${account} / 群 ${groupId}`}
        actions={<Link className="glass-button px-3 py-2 text-sm" to={listPath}>返回群列表</Link>} />
      {error && <Card role="alert" className="border border-error text-error">{error} <Button size="sm" onClick={reload}>重试读取</Button></Card>}
      <Card className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-sm" aria-live="polite">
          <Badge variant={group.membership === 'joined' ? 'success' : 'default'}>{membership}</Badge>
          <Badge variant={enabled ? 'success' : 'default'}>响应{enabled ? '开启' : '关闭'}</Badge>
          <span className="text-text-secondary">配置修订 {config.saved_revision} · {config.apply_status === 'applied' ? '已生效' : '已保存, 待应用'}</span>
          <button type="button" className="text-accent hover:underline" onClick={() => navigator.clipboard.writeText(groupId)}>复制群号</button>
        </div>
        {historical && <p className="text-sm text-warning">当前连接账号不同, 此账号的群配置只读</p>}
        {!isRunning && <p className="text-sm text-warning">{historical
          ? '后端离线或未确认绑定账号, 此页仅可查看历史快照'
          : '后端离线。可保存冷配置, 网络动作暂不可执行'}</p>}
        <nav className="flex gap-1 overflow-x-auto border-t border-glass-border pt-3" aria-label="群详情标签">
          {sections.map(([key, label]) => (
            <NavLink key={key} to={`/platforms/${encodeURIComponent(adapterId)}/groups/${encodeURIComponent(groupId)}/${key}?account=${encodeURIComponent(account)}`}
              className={`shrink-0 rounded-lg px-3 py-2 text-sm ${section === key ? 'bg-accent/15 text-accent' : 'text-text-secondary hover:text-text-primary'}`}>
              {label}
            </NavLink>
          ))}
        </nav>
      </Card>
      <Outlet context={{ adapterId, groupId, account, isRunning, historical, config, setConfig, reload } satisfies GroupContext} />
    </div>
  );
}
