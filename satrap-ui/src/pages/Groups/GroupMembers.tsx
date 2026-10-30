import { createUuid } from '@/utils/uuid';
import { Link } from 'react-router-dom';
import { approvalLink } from '@/api/platformActions';
import { groupActionStateLabels as actionStateLabels } from './groupActionLabels';
import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Search, UserCheck, Wrench } from 'lucide-react';
import { groupApi, GROUP_PAGE_SIZE, type GroupAction, type GroupMember } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { EmptyState } from '@/components/kit/Feedback';
import { Input } from '@/components/kit/Field';
import { Modal } from '@/components/kit/Modal';
import { toast } from '@/components/kit/Toast';
import { useGroupContext } from './GroupLayout';
import { errorText } from '@/utils/errorText';
import { groupActionLabels } from './groupActionLabels';
import { Avatar, FieldBlock, Note, Pager, RoleBadge, hueFor } from './shared';
import { Select as KitSelect } from '@/components/kit/Select';

const memberActions = [
  ['set_group_card', '修改群昵称'], ['ban_group_member', '禁言或解除'],
  ['set_group_admin', '设置或取消管理员'], ['kick_group_member', '移出成员'],
] as const;

export function GroupMembers() {
  const { adapterId, groupId, account, historical, isRunning, config } = useGroupContext();
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<GroupMember[]>([]);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<{ member: GroupMember; action: string } | null>(null);
  const [types, setTypes] = useState<Awaited<ReturnType<typeof groupApi.actionTypes>>['items']>([]);
  const [duration, setDuration] = useState('1800');
  const [enable, setEnable] = useState('true');
  const [card, setCard] = useState('');
  const [reject, setReject] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<GroupAction | null>(null);
  const [operationId, setOperationId] = useState('');
  const [submitted, setSubmitted] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => { setDebounced(query); setPage(1); }, 300);
    return () => window.clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    if (!isRunning || historical || config.group.membership !== 'joined') return;
    let live = true;
    groupApi.actionTypes(adapterId, groupId, account).then((value) => {
      if (live) setTypes(value.items);
    }).catch((caught) => { if (live) setError(errorText(caught, '成员读取失败')); });
    return () => { live = false; };
  }, [adapterId, groupId, account, isRunning, historical, config.group.membership]);
  useEffect(() => {
    if (!isRunning || historical || config.group.membership !== 'joined') return;
    let live = true;
    setLoading(true);
    groupApi.members(adapterId, groupId, account, debounced, page).then((value) => {
      if (!live) return;
      setItems(value.items); setTotal(value.total_loaded); setTruncated(value.truncated); setError('');
    }).catch((caught) => { if (live) setError(errorText(caught, '成员读取失败')); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [adapterId, groupId, account, isRunning, historical, config.group.membership, debounced, page, refreshKey]);
  const open = (member: GroupMember, action: string) => {
    if (result?.state === 'unknown' || result?.state === 'executing') return;
    setSelected({ member, action }); setResult(null); setCard(member.card || ''); setDuration('1800');
    setEnable('true'); setReject(false); setError(''); setOperationId(createUuid()); setSubmitted(null);
  };
  const submit = async () => {
    if (!selected || busy) return;
    const params: Record<string, unknown> = { user_id: String(selected.member.user_id) };
    if (selected.action === 'set_group_card') params.card = card;
    if (selected.action === 'ban_group_member') params.duration = Number(duration);
    if (selected.action === 'set_group_admin') params.enable = enable === 'true';
    if (selected.action === 'kick_group_member') params.reject_add_request = reject;
    const payload = submitted || params;
    setSubmitted(payload);
    setBusy(true);
    try {
      const saved = await groupApi.submitAction(adapterId, groupId, {
        expected_self_id: account, action_id: operationId, action_type: selected.action, params: payload,
      });
      setResult(saved); setError('');
      if (saved.state === 'pending') toast.info('动作已提交审批', '等待人工批准后执行');
      else if (saved.state === 'succeeded') toast.success('成员操作已完成');
    } catch (caught) { setError(errorText(caught, '成员读取失败')); }
    finally { setBusy(false); }
  };
  const inspect = async () => {
    if (!operationId) return;
    try {
      const saved = await groupApi.action(adapterId, groupId, account, operationId);
      setResult(saved); setError('');
      if (saved.state === 'pending') toast.info('动作仍在等待人工批准', '请在审批中心处理后再次查询');
    } catch (caught) { setError(errorText(caught, '成员读取失败')); }
  };
  const closeAction = useCallback(() => {
    if (result?.state === 'unknown' || result?.state === 'executing') return;
    setSelected(null);
  }, [result?.state]);
  const available = isRunning && !historical && config.group.membership === 'joined';
  const totalPages = Math.max(1, Math.ceil(total / GROUP_PAGE_SIZE));

  return <div className="space-y-4 animate-fade">
    {error && <Note tone="red" title="成员读取失败">{error}</Note>}
    <Card padding="none">
      <div className="flex flex-wrap items-center gap-3 border-b border-line p-4">
        <div className="min-w-0">
          <h3 className="text-[14px] font-semibold text-fg">群成员</h3>
          <p className="text-[11.5px] text-fg-3">按需从平台读取, 只对本次取得的成员搜索与分页; 角色和能力以平台实际返回为准</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <div className="relative w-56">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-4" />
            <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索账号昵称、群昵称或 QQ"
              aria-label="搜索群成员" className="!h-8 !pl-8 !text-[12.5px]" disabled={!available} />
          </div>
          <Button size="sm" variant="ghost" icon={<RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin-slow' : ''}`} />}
            disabled={!available || loading} onClick={() => setRefreshKey((value) => value + 1)}>刷新成员</Button>
        </div>
      </div>
      {!available && <div className="p-4"><Note tone="orange">此群当前无法读取成员, 历史目录仍可查看。</Note></div>}
      {truncated && <div className="px-4 pt-3"><Note tone="orange">平台成员列表超过 2048 条或达到安全上限, 本页仅展示已取得的集合。</Note></div>}
      {loading ? (
        <div className="space-y-2 p-4">{[0, 1, 2, 3, 4].map((index) => <div key={index} className="skeleton h-10 rounded-lg" />)}</div>
      ) : items.length === 0 ? (
        <EmptyState tint="pink" icon={<UserCheck className="h-6 w-6" />} title="没有符合条件的成员"
          description={debounced ? `没有匹配 “${debounced}” 的成员, 换个关键字试试。` : '平台未返回成员列表。'}
          actions={debounced ? <Button onClick={() => setQuery('')}>清除搜索</Button> : undefined} />
      ) : (
        <div className="divide-y divide-[var(--glass-border)]">
          {items.map((member) => (
            <div key={String(member.user_id)} className="flex flex-wrap items-center gap-3 px-4 py-2.5 transition-colors hover:bg-glass-hover">
              <Avatar name={member.card || member.nickname || String(member.user_id)} hue={hueFor(String(member.user_id))} size="md" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="truncate text-[13px] font-medium text-fg">{member.card || member.nickname || '未提供昵称'}</span>
                  <RoleBadge role={member.role} />
                  {member.title && <Badge tone="teal">{member.title}</Badge>}
                </div>
                <p className="num mt-0.5 font-mono text-[10.5px] text-fg-4">{member.user_id} · 入群 {member.join_time ? new Date(member.join_time * 1000).toLocaleDateString('zh-CN') : '—'} · 最近发言 {member.last_sent_time ? new Date(member.last_sent_time * 1000).toLocaleString('zh-CN') : '—'}</p>
              </div>
              <div className="flex flex-wrap items-center gap-1">
                {memberActions.map(([action, label]) => {
                  const meta = types.find((item) => item.action_type === action);
                  return <Button key={action} size="sm" variant="ghost" disabled={!available || !meta?.available || result?.state === 'unknown' || result?.state === 'executing'}
                    onClick={() => open(member, action)}>{label}</Button>;
                })}
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="border-t border-line px-4 py-2.5">
        <Pager page={page} totalPages={totalPages} onChange={setPage} disabled={!available}
          label={<span>已加载匹配 <span className="num text-fg-2">{total}</span> 人{truncated ? ', 总人数未知' : ''} · 每页 {GROUP_PAGE_SIZE}</span>} />
      </div>
    </Card>

    <Modal open={selected !== null} onClose={closeAction}
      title={selected ? groupActionLabels[selected.action] ?? selected.action : ''} icon={<Wrench className="h-[18px] w-[18px]" />}
      description={selected ? `目标群 ${groupId} · 目标成员 ${selected.member.user_id}` : undefined}
      size="md" className="tint-pink" footer={<>
        <Button variant="ghost" onClick={() => { setSelected(null); setResult(null); }} disabled={result?.state === 'unknown' || result?.state === 'executing'}>关闭</Button>
        <Button variant="ghost" onClick={inspect} disabled={!operationId || busy}>按操作 ID 查询</Button>
        {!result && <Button variant="primary" loading={busy} onClick={submit}>{submitted ? '按原 ID 重试提交' : '提交动作'}</Button>}
      </>}>
      {selected && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 rounded-xl border border-line bg-glass p-3">
            <Avatar name={selected.member.card || selected.member.nickname || String(selected.member.user_id)} hue={hueFor(String(selected.member.user_id))} size="lg" />
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-fg">{selected.member.card || selected.member.nickname}</div>
              <div className="num font-mono text-[11px] text-fg-4">{selected.member.user_id} · {selected.member.role || '角色未知'}</div>
            </div>
          </div>
          <div className="rounded-xl border border-line bg-glass px-3 py-2 text-[12px] text-fg-2">
            审批: {types.find((item) => item.action_type === selected.action)?.approval_mode === 'approval_required' ? '提交后等待人工批准' : '提交后自动执行'}
          </div>
          {selected.action === 'ban_group_member' && (
            <FieldBlock label="时长, 秒" hint="0 为解除禁言; 最长 2592000 秒 (30 天)">
              <Input type="number" min={0} max={2592000} value={duration} disabled={!!submitted} onChange={(event) => setDuration(event.target.value)} />
            </FieldBlock>
          )}
          {selected.action === 'set_group_admin' && (
            <FieldBlock label="操作">
              <KitSelect className="field w-full" value={enable} disabled={!!submitted} onChange={(value) => setEnable(value)}>
                <option value="true">设置管理员</option><option value="false">取消管理员</option>
              </KitSelect>
            </FieldBlock>
          )}
          {selected.action === 'set_group_card' && (
            <FieldBlock label="新群昵称" hint="留空表示清除, 最多 60 字符">
              <Input value={card} maxLength={60} disabled={!!submitted} onChange={(event) => setCard(event.target.value)} />
            </FieldBlock>
          )}
          {selected.action === 'kick_group_member' && (
            <label className="flex cursor-pointer items-center gap-2 text-[12.5px] text-fg-2">
              <input type="checkbox" className="accent-[rgb(var(--rgb-pink))]" checked={reject} disabled={!!submitted} onChange={(event) => setReject(event.target.checked)} />拒绝后续加群请求
            </label>
          )}
          {result && (
            <div className="rounded-xl border border-line bg-glass p-3 text-[12.5px] animate-pop" role="status">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={result.state === 'succeeded' ? 'green' : result.state === 'failed' || result.state === 'rejected' ? 'red' : 'orange'} dot>{actionStateLabels[result.state] || result.state}</Badge>
                <span className="num font-mono text-[10.5px] text-fg-4">{result.action_id}</span>
              </div>
              <p className="mt-1 text-fg-2">操作 {result.action_id}: {actionStateLabels[result.state] || result.state}{result.state === 'unknown' ? ', 结果未知, 请先核实平台状态' : ''}</p>
              {result.result?.reason && <p className="mt-1 text-fg-3">{result.result.reason}</p>}
              {result.state === 'pending' && (
                <Link className="mt-1 inline-flex text-[12px] text-accent hover:underline"
                  to={approvalLink({ platform: adapterId, account, source: 'group', target: groupId, action: result.action_id })}>
                  已提交, 前往审批中心
                </Link>
              )}
            </div>
          )}
          {result?.state === 'unknown' && <Button variant="ghost" onClick={() => {
            setSelected(null); setResult(null); setSubmitted(null); setOperationId('');
          }}>我已核实平台状态, 可以创建新操作</Button>}
          <p className="break-all text-[11px] text-fg-4">操作 ID: {operationId}</p>
        </div>
      )}
    </Modal>
  </div>;
}
