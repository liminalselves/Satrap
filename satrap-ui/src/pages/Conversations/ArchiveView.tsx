import { useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import { useSearchParams } from 'react-router-dom';
import { controlApi } from '@/api/control';
import type { ConversationPlatform, PlatformArchiveCatalog, PlatformArchiveMessage, PlatformArchiveRecord, PlatformArchiveSnapshot } from '@/api/types';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { formatTime } from '@/utils/format';
import { GroupSummaries } from './GroupSummaries';

const errorText = (error: unknown) => axios.isAxiosError<{ error?: string }>(error)
  ? error.response?.data?.error || error.message : error instanceof Error ? error.message : String(error);
const recordKey = (record: PlatformArchiveRecord) => JSON.stringify([record.platform_id, record.self_id, record.conversation_kind, record.chat_id]);
const emptyCatalog: PlatformArchiveCatalog = { items: [], total: 0, warnings: [], conversation_kinds: [], self_ids: [] };
const stateNames = { active: '已采集', deleted: '已从档案删除', recalled: '平台已撤回', expired: '正文已过期' };
const selectionKeys = ['archive_platform', 'archive_self', 'archive_kind', 'archive_chat'];

export function ArchiveView({ platforms }: { platforms: ConversationPlatform[] }) {
  const [search, setSearch] = useSearchParams();
  const platform = search.get('platform') || '';
  const type = search.get('type') || '';
  const kind = search.get('archive_filter_kind') || '';
  const selfId = search.get('archive_filter_self') || '';
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [catalog, setCatalog] = useState(emptyCatalog);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const selectedIdentity = selectionKeys.map((key) => search.get(key) || '');
  const selectedKey = JSON.stringify(selectedIdentity);
  const descriptor = platforms.find((item) => item.id === selectedIdentity[0]);
  const selected = selectedIdentity.every(Boolean) ? catalog.items.find((item) => recordKey(item) === selectedKey) || {
    platform_id: selectedIdentity[0], adapter_id: selectedIdentity[0], self_id: selectedIdentity[1],
    conversation_kind: selectedIdentity[2], chat_id: selectedIdentity[3], label: selectedIdentity[3],
    conversation_kind_label: catalog.conversation_kinds.find((item) => item.value === selectedIdentity[2])?.label || selectedIdentity[2],
    platform_type: descriptor?.type || '', type_label: descriptor?.type_label || '', revision: 0, message_count: 0, last_message_at: null,
  } : undefined;

  useEffect(() => {
    let disposed = false;
    setLoading(true); setError('');
    controlApi.listPlatformArchives({ platform_id: platform, platform_type: type, conversation_kind: kind, self_id: selfId,
      q: submitted, offset, limit: 40 }).then((data) => { if (!disposed) setCatalog(data); })
      .catch((error) => { if (!disposed) setError(errorText(error)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [platform, type, kind, selfId, submitted, offset, refresh]);

  const chooseFilter = (key: string, value: string) => {
    const next = new URLSearchParams(search);
    value ? next.set(key, value) : next.delete(key);
    selectionKeys.forEach((key) => next.delete(key));
    if (key === 'type') next.delete('platform');
    if (key === 'type' || key === 'platform') { next.delete('archive_filter_kind'); next.delete('archive_filter_self'); }
    setSearch(next); setOffset(0);
  };
  const types = [...new Map(platforms.map((item) => [item.type, item.type_label])).entries()];
  return <div className="space-y-4">
    <Card className="space-y-3">
      <p className="text-sm text-text-secondary">这里保存平台实际采集的入站消息与确认发送的回复。删除只影响档案检索，模型上下文和平台原消息保持原状；消息原文仅可查看。</p>
      <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" onSubmit={(event) => { event.preventDefault(); setSubmitted(query); setOffset(0); }}>
        <label className="min-w-0 text-sm">平台类型<select aria-label="档案平台类型" className="glass-input mt-1 w-full" value={type} onChange={(event) => chooseFilter('type', event.target.value)}><option value="">全部类型</option>{types.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label className="min-w-0 text-sm">平台实例<select aria-label="档案平台实例" className="glass-input mt-1 w-full" value={platform} onChange={(event) => chooseFilter('platform', event.target.value)}><option value="">全部实例</option>{platform && !platforms.some((item) => item.id === platform) && <option value={platform}>{platform}</option>}{platforms.filter((item) => !type || item.type === type).map((item) => <option key={item.id} value={item.id}>{item.label} · {item.type_label}</option>)}</select></label>
        <label className="min-w-0 text-sm">对话类型<select aria-label="档案对话类型" className="glass-input mt-1 w-full" value={kind} onChange={(event) => chooseFilter('archive_filter_kind', event.target.value)}><option value="">全部对话类型</option>{kind && !catalog.conversation_kinds.some((item) => item.value === kind) && <option value={kind}>{kind}</option>}{catalog.conversation_kinds.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <label className="min-w-0 text-sm">机器人账号<select aria-label="档案机器人账号" className="glass-input mt-1 w-full" value={selfId} onChange={(event) => chooseFilter('archive_filter_self', event.target.value)}><option value="">全部账号</option>{selfId && !catalog.self_ids.includes(selfId) && <option value={selfId}>{selfId}</option>}{catalog.self_ids.map((id) => <option key={id}>{id}</option>)}</select></label>
        <label className="min-w-0 text-sm">对话名称或 ID<input aria-label="搜索档案对话" className="glass-input mt-1 w-full" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索群名、对话 ID 或账号" /></label>
        <div className="flex flex-wrap items-end gap-2"><Button type="submit" disabled={loading}>搜索档案</Button><Button type="button" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>刷新档案</Button><Button type="button" onClick={() => { setSearch({ view: 'archive' }); setQuery(''); setSubmitted(''); setOffset(0); }}>重置筛选</Button></div>
      </form>
      {error && <p role="alert" className="text-error">{error}</p>}
      {catalog.warnings.map((warning) => <p key={warning.platform_id} role="alert" className="text-warning">{warning.platform_id} 的档案读取失败，列表可能不完整</p>)}
      <p className="text-xs text-text-tertiary">共 {catalog.total} 个档案对话，按平台、账号与对话身份排列。未被采集的消息不在此列表中。</p>
    </Card>
    <div className="grid items-start gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]">
      <Card className="min-w-0 space-y-2">
        {loading && <p role="status">正在读取档案目录…</p>}
        {!loading && !catalog.items.length && <p className="text-sm text-text-secondary">暂无匹配的平台消息档案</p>}
        {catalog.items.map((record) => <button key={recordKey(record)} disabled={loading} className={`w-full space-y-2 rounded-lg p-3 text-left ${recordKey(record) === selectedKey ? 'bg-accent/10' : 'bg-glass hover:bg-glass-hover'}`} onClick={() => { const next = new URLSearchParams(search); [record.platform_id, record.self_id, record.conversation_kind, record.chat_id].forEach((value, index) => next.set(selectionKeys[index], value)); setSearch(next); }}>
          <p className="break-all font-medium">{record.label || record.chat_id}</p>
          <p className="break-all text-xs text-text-tertiary">{record.platform_id} · 账号 {record.self_id} · {record.chat_id}</p>
          <Badge variant="info">{record.conversation_kind_label}</Badge>
          <p className="text-xs text-text-secondary">{record.message_count} 条可检索消息{record.last_message_at !== null ? ` · 最近 ${formatTime(record.last_message_at)}` : ' · 当前无可检索正文'}</p>
        </button>)}
        <div className="flex justify-between gap-2 pt-2"><Button size="sm" disabled={!offset || loading} onClick={() => setOffset((value) => Math.max(0, value - 40))}>上一页档案</Button><Button size="sm" disabled={offset + 40 >= catalog.total || loading} onClick={() => setOffset((value) => value + 40)}>下一页档案</Button></div>
      </Card>
      {selected ? <ArchivePanel key={selectedKey} record={selected} refresh={refresh} onChanged={() => setRefresh((value) => value + 1)} /> : <Card><p className="text-text-secondary">选择一个档案对话查看消息</p></Card>}
    </div>
  </div>;
}

type PendingAction = { action: 'delete' | 'clear' | 'restore'; revision: number; message_ids?: string[]; backup_id?: string };

function ArchivePanel(props: { record: PlatformArchiveRecord; refresh: number; onChanged: () => void }) {
  const [view, setView] = useState<'messages' | 'summaries'>('messages');
  return <div className="min-w-0 space-y-3">
    <Card><div className="flex flex-wrap gap-2" aria-label="平台对话内容">
      <Button variant={view === 'messages' ? 'primary' : 'ghost'} onClick={() => setView('messages')}>原始消息</Button>
      <Button variant={view === 'summaries' ? 'primary' : 'ghost'} onClick={() => setView('summaries')}>群摘要</Button>
    </div></Card>
    {view === 'messages' ? <ArchiveDetail {...props} /> : <GroupSummaries record={props.record} refresh={props.refresh} />}
  </div>;
}

function ArchiveDetail({ record, refresh, onChanged }: { record: PlatformArchiveRecord; refresh: number; onChanged: () => void }) {
  const [data, setData] = useState<PlatformArchiveSnapshot>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [keyword, setKeyword] = useState('');
  const [sender, setSender] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [query, setQuery] = useState<Record<string, string>>({});
  const [readRevision, setReadRevision] = useState(0);
  const [cursors, setCursors] = useState<string[]>([]);
  const [selection, setSelection] = useState<string[]>([]);
  const [pending, setPending] = useState<PendingAction>();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [inspected, setInspected] = useState<PlatformArchiveMessage>();
  const [inspectError, setInspectError] = useState('');
  const [inspecting, setInspecting] = useState(false);
  const mounted = useRef(true);
  const inspectRequest = useRef(0);
  const { platform_id, self_id, conversation_kind, chat_id } = record;
  const identity = useMemo(() => ({ platform_id, self_id, conversation_kind, chat_id }), [platform_id, self_id, conversation_kind, chat_id]);
  const queryKey = JSON.stringify(query);
  const cursor = cursors[cursors.length - 1];
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError(''); setData(undefined); setSelection([]);
    controlApi.platformArchiveData(identity, { ...JSON.parse(queryKey), ...(cursor ? { cursor } : {}), limit: 40 }).then((data) => { if (!disposed) setData(data); })
      .catch((error) => { if (!disposed) setError(errorText(error)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [identity, queryKey, cursor, refresh, readRevision]);
  const inspect = async (id: string) => {
    const request = ++inspectRequest.current;
    setInspected(undefined); setInspectError(''); setInspecting(true);
    try {
      const result = await controlApi.platformArchiveMessage(identity, id);
      if (mounted.current && inspectRequest.current === request) setInspected(result.item);
    } catch (error) { if (mounted.current && inspectRequest.current === request) setInspectError(errorText(error)); }
    finally { if (mounted.current && inspectRequest.current === request) setInspecting(false); }
  };
  const prepare = (action: PendingAction['action'], options: Pick<PendingAction, 'message_ids' | 'backup_id'> = {}) => {
    if (!data || loading || busy) return;
    setActionError(''); setPending({ action, revision: data.revision, ...options });
  };
  const mutate = async () => {
    if (!pending || busy) return;
    setBusy(true); setActionError('');
    try {
      const result = await controlApi.mutatePlatformArchive(identity, pending.action, pending.revision, { message_ids: pending.message_ids, backup_id: pending.backup_id });
      if (!mounted.current) return;
      setNotice(pending.action === 'restore' ? `已恢复 ${result.restored_count || 0} 条记录状态，跳过 ${result.skipped_count || 0} 条` : `已从档案删除 ${result.deleted_count || 0} 条消息，可在 ${result.expires_at ? formatTime(result.expires_at) : '备份有效期'} 前恢复`);
      setPending(undefined); setInspected(undefined); setCursors([]); setSelection([]); onChanged();
    } catch (error) { if (mounted.current) setActionError(`${errorText(error)}；如状态已变化，请取消并刷新后重新选择操作`); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <Card className="min-w-0 space-y-4">
    <div><h2 className="break-all text-lg font-medium">{data?.scope.label || record.label || record.chat_id}</h2><p className="break-all text-xs text-text-tertiary">{record.platform_id} · 账号 {record.self_id} · {record.conversation_kind_label} · {record.chat_id}</p></div>
    <form className="grid gap-3 sm:grid-cols-2" onSubmit={(event) => {
      event.preventDefault();
      try {
        const next = { ...(keyword ? { keyword } : {}), ...(sender.trim() ? { sender_id: sender.trim() } : {}), ...(start ? { start_time: new Date(start).toISOString() } : {}), ...(end ? { end_time: new Date(end).toISOString() } : {}) };
        if (start && end && new Date(start) > new Date(end)) throw new Error('开始时间不能晚于结束时间');
        setQuery(next); setCursors([]); setNotice(''); setReadRevision((value) => value + 1);
      } catch (error) { setError(errorText(error)); }
    }}>
      <label className="min-w-0 text-sm">消息关键词<input aria-label="档案消息关键词" className="glass-input mt-1 w-full" value={keyword} onChange={(event) => setKeyword(event.target.value)} /></label>
      <label className="min-w-0 text-sm">发送者 ID<input aria-label="档案发送者 ID" className="glass-input mt-1 w-full" value={sender} onChange={(event) => setSender(event.target.value)} /></label>
      <label className="min-w-0 text-sm">开始时间<input aria-label="档案开始时间" type="datetime-local" className="glass-input mt-1 w-full" value={start} onChange={(event) => setStart(event.target.value)} /></label>
      <label className="min-w-0 text-sm">结束时间<input aria-label="档案结束时间" type="datetime-local" className="glass-input mt-1 w-full" value={end} onChange={(event) => setEnd(event.target.value)} /></label>
      <p className="text-xs text-text-tertiary sm:col-span-2">时间使用当前设备时区（{Intl.DateTimeFormat().resolvedOptions().timeZone}），查询会转换为带时区的时间。</p>
      <div className="flex flex-wrap gap-2 sm:col-span-2"><Button type="submit" disabled={loading || busy}>查询消息</Button><Button type="button" disabled={loading || busy} onClick={() => { setKeyword(''); setSender(''); setStart(''); setEnd(''); setQuery({}); setCursors([]); onChanged(); }}>重置消息筛选</Button><Button type="button" disabled={loading || busy} onClick={onChanged}>刷新消息</Button></div>
    </form>
    {loading && <p role="status">正在读取平台消息…</p>}
    {error && <p role="alert" className="text-error">{error}</p>}
    {notice && <p role="status" className="text-success">{notice}</p>}
    {data && <>
      <p className="text-xs text-text-tertiary">档案保留 {data.retention_days} 天。实际采集范围：{data.coverage.archived_from === null ? '暂无可检索消息' : `${formatTime(data.coverage.archived_from)} 至 ${formatTime(data.coverage.archived_to ?? data.coverage.archived_from)}`}。本地采集范围可能包含缺失消息。</p>
      {data.truncated && <p role="alert" className="text-warning">部分消息正文超出本次查询预算，查看消息详情可读取单条存档内容。</p>}
      <div className="flex flex-wrap gap-2"><Button variant="danger" size="sm" disabled={!selection.length || busy || loading} onClick={() => prepare('delete', { message_ids: [...selection] })}>删除所选档案（{selection.length}）</Button><Button variant="danger" size="sm" disabled={busy || loading} onClick={() => prepare('clear')}>清空此对话全部档案</Button></div>
      {!data.items.length && <p className="text-sm text-text-secondary">当前条件下没有可检索消息</p>}
      <div className="space-y-3">{data.items.map((message) => <div key={message.message_id} className="space-y-2 rounded-lg bg-glass p-3">
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" aria-label={`选择档案消息 ${message.message_id}`} disabled={busy || loading} checked={selection.includes(message.message_id)} onChange={(event) => setSelection((values) => event.target.checked ? [...values, message.message_id] : values.filter((id) => id !== message.message_id))} />消息 {message.message_id}</label>
        <ArchiveMessage message={message} onQuote={inspect} />
        <div className="flex flex-wrap gap-2"><Button size="sm" disabled={busy} onClick={() => inspect(message.message_id)}>查看消息详情</Button><Button size="sm" variant="danger" disabled={busy || loading} onClick={() => prepare('delete', { message_ids: [message.message_id] })}>删除这条档案</Button></div>
      </div>)}</div>
      <div className="flex justify-between gap-2"><Button size="sm" disabled={!cursors.length || loading || busy} onClick={() => setCursors((values) => values.slice(0, -1))}>较新消息</Button><Button size="sm" disabled={!data.has_more || !data.next_cursor || loading || busy} onClick={() => { if (data.next_cursor) setCursors((values) => [...values, data.next_cursor!]); }}>较早消息</Button></div>
      <details className="space-y-2"><summary className="cursor-pointer text-sm">可恢复的档案删除（{data.backups.length}）</summary>
        <p className="text-xs text-text-tertiary">恢复保留删除时的记录状态，不覆盖后来收到的消息或平台撤回。被后续操作覆盖的备份需先处理后续操作。</p>
        {data.backups.map((backup) => <div key={backup.backup_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-glass p-3"><p className="text-xs">{backup.action === 'clear' ? '清空对话' : '删除消息'} · {formatTime(backup.created_at)}<br />有效至 {formatTime(backup.expires_at)}</p><Button size="sm" disabled={busy || loading} onClick={() => prepare('restore', { backup_id: backup.backup_id })}>恢复此删除</Button></div>)}
      </details>
    </>}
    <Modal open={!!pending} onClose={() => { if (!busy) setPending(undefined); }} title={pending?.action === 'restore' ? '恢复平台消息档案' : '删除平台消息档案'}>
      <div className="space-y-4">
        <p className="break-all text-sm">目标：{record.platform_id} / {record.self_id} / {record.conversation_kind_label} / {record.chat_id}</p>
        <p className="text-sm">{pending?.action === 'clear' ? '这会清空此对话的全部档案，不受当前消息筛选影响。' : pending?.action === 'delete' ? `这会删除选中的 ${pending.message_ids?.length || 0} 条档案消息。` : '这会恢复仍归属该删除操作且未过期的记录状态。'}</p>
        <p className="text-sm text-text-secondary">仅影响平台消息档案及工具检索，模型上下文和平台原消息保持原状。删除备份按档案保留期过期。</p>
        {actionError && <p role="alert" className="text-error">{actionError}</p>}
        <div className="flex justify-end gap-2"><Button disabled={busy} onClick={() => setPending(undefined)}>取消</Button><Button variant={pending?.action === 'restore' ? 'primary' : 'danger'} disabled={busy} onClick={mutate}>{busy ? '正在处理…' : pending?.action === 'restore' ? '确认恢复' : '确认删除档案'}</Button></div>
      </div>
    </Modal>
    <Modal open={inspecting || !!inspected || !!inspectError} onClose={() => { inspectRequest.current += 1; setInspecting(false); setInspected(undefined); setInspectError(''); }} title="平台消息详情" size="lg">
      {inspecting && <p role="status">正在读取消息详情…</p>}{inspectError && <p role="alert" className="text-error">{inspectError}</p>}
      {inspected && <div className="space-y-3"><p className="break-all text-sm">消息 ID：{inspected.message_id} · {stateNames[inspected.status]}</p><ArchiveMessage message={inspected} onQuote={inspect} /><p className="text-xs text-text-tertiary">来源：{inspected.source} · {inspected.verified ? '归属已核验' : '归属未核验'}</p><details><summary className="cursor-pointer text-sm">组件与媒体摘要</summary><pre className="mt-2 whitespace-pre-wrap break-all rounded-lg bg-glass p-3 text-xs">{JSON.stringify({ components: inspected.components, media: inspected.media }, null, 2)}</pre></details></div>}
    </Modal>
  </Card>;
}

function ArchiveMessage({ message, onQuote }: { message: PlatformArchiveMessage; onQuote: (id: string) => void }) {
  return <div className="min-w-0 space-y-2">
    <p className="break-all text-xs text-text-secondary">{message.direction === 'outbound' ? '机器人发出' : '收到消息'} · {message.card || message.nickname || message.sender_id || '发送者资料已擦除'}{message.sender_id && `（${message.sender_id}）`}</p>
    <p className="text-xs text-text-tertiary">{formatTime(message.message_time)} · {message.time_source === 'platform' ? '平台时间' : '本地采集时间'}</p>
    {message.status === 'active' ? <p className="whitespace-pre-wrap break-words text-sm">{message.text || '这条消息没有文本正文'}</p> : <p className="text-sm text-text-secondary">{stateNames[message.status]}，正文不可检索</p>}
    {message.reply_to_message_id && <Button variant="ghost" size="sm" onClick={() => onQuote(message.reply_to_message_id!)}>查看引用消息 {message.reply_to_message_id}</Button>}
    {!!message.mentions?.length && <p className="break-all text-xs">提及对象：{message.mentions.join('、')}</p>}
    {!!message.media?.length && <p className="text-xs text-text-secondary">附带 {message.media.length} 个媒体组件，档案保存引用摘要</p>}
    {message.truncated && <p className="text-xs text-warning">正文或组件摘要已截断</p>}
  </div>;
}
