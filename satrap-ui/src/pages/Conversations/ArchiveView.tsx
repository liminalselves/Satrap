import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Archive, Bell, ChevronRight, CornerUpLeft, FileText, MessageSquare, RefreshCw, RotateCcw, Search, Smile, Tags,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Button, IconButton } from '@/components/kit/Button';
import { Badge } from '@/components/kit/Badge';
import { Modal } from '@/components/kit/Modal';
import { controlApi } from '@/api/control';
import type {
  ConversationPlatform, PlatformArchiveCatalog, PlatformArchiveMessage, PlatformArchiveRecord, PlatformArchiveSnapshot,
} from '@/api/types';
import { formatTime, formatRelativeTime } from '@/utils/format';
import { errorText } from '@/utils/errorText';
import { GroupSummaries } from './GroupSummaries';
import { GroupMemories } from './GroupMemories';
import { GroupReminders } from './GroupReminders';
import { StickerSettings } from './StickerSettings';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { InlineEmpty, KeyValue, Notice, Pager, SearchField, StatTile } from './common';
import { Select as KitSelect } from '@/components/kit/Select';

const recordKey = (record: PlatformArchiveRecord) => JSON.stringify([record.platform_id, record.self_id, record.conversation_kind, record.chat_id]);
const emptyCatalog: PlatformArchiveCatalog = { items: [], total: 0, warnings: [], conversation_kinds: [], self_ids: [] };
const stateNames = { active: '已采集', deleted: '已从档案删除', recalled: '平台已撤回', expired: '正文已过期' };
const selectionKeys = ['archive_platform', 'archive_self', 'archive_kind', 'archive_chat'];
const kindTint = (kind: string) => kind === 'group' ? 'green' : kind === 'private' ? 'purple' : 'teal';

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
      .catch((reason) => { if (!disposed) setError(errorText(reason)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [platform, type, kind, selfId, submitted, offset, refresh]);

  const chooseFilter = (key: string, value: string) => {
    const next = new URLSearchParams(search);
    if (value) next.set(key, value); else next.delete(key);
    selectionKeys.forEach((filterKey) => next.delete(filterKey));
    if (key === 'type') next.delete('platform');
    if (key === 'type' || key === 'platform') { next.delete('archive_filter_kind'); next.delete('archive_filter_self'); }
    setSearch(next); setOffset(0);
  };
  const select = (record: PlatformArchiveRecord) => {
    const next = new URLSearchParams(search);
    selectionKeys.forEach((key, index) => next.set(key, [record.platform_id, record.self_id, record.conversation_kind, record.chat_id][index]));
    setSearch(next);
  };
  const resetFilters = () => {
    setSearch({ view: 'archive' }); setQuery(''); setSubmitted(''); setOffset(0); setError('');
  };
  const types = [...new Map(platforms.map((item) => [item.type, item.type_label])).entries()];
  const page = Math.max(0, Math.floor(offset / 40));

  return (
    <div className="grid items-start gap-3 xl:grid-cols-[340px_minmax(0,1fr)]">
      <div className="surface tint-green flex min-h-[420px] min-w-0 flex-col overflow-hidden !rounded-[16px] lg:h-full">
        <div className="flex-none space-y-2 border-b border-line p-3">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <h2 className="flex items-center gap-2 text-[14px] font-semibold text-fg"><Archive className="h-4 w-4 text-tint" />平台消息档案</h2>
              <p className="mt-0.5 text-[11px] text-fg-4">共 {catalog.total} 个档案对话 · 按平台与对话身份排列</p>
            </div>
            <IconButton label="刷新档案" size="sm" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>
              <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin-slow')} />
            </IconButton>
          </div>
          <SearchField value={query} onChange={(value) => { setQuery(value); setOffset(0); }} onEnter={() => { setSubmitted(query); setOffset(0); }} placeholder="搜索群名、对话 ID 或账号" ariaLabel="搜索档案对话" />
          <form className="grid grid-cols-2 gap-1.5" onSubmit={(event) => { event.preventDefault(); setSubmitted(query); setOffset(0); }}>
            <label className="min-w-0 text-[10.5px] text-fg-4">平台类型
              <KitSelect aria-label="档案平台类型" className="field mt-1 !h-8 w-full text-[11px]" value={type} onChange={(value) => chooseFilter('type', value)}>
                <option value="">全部类型</option>{types.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </KitSelect>
            </label>
            <label className="min-w-0 text-[10.5px] text-fg-4">平台实例
              <KitSelect aria-label="档案平台实例" className="field mt-1 !h-8 w-full text-[11px]" value={platform} onChange={(value) => chooseFilter('platform', value)}>
                <option value="">全部实例</option>
                {platform && !platforms.some((item) => item.id === platform) && <option value={platform}>{platform}</option>}
                {platforms.filter((item) => !type || item.type === type).map((item) => <option key={item.id} value={item.id}>{item.label} · {item.type_label}</option>)}
              </KitSelect>
            </label>
            <label className="min-w-0 text-[10.5px] text-fg-4">对话类型
              <KitSelect aria-label="档案对话类型" className="field mt-1 !h-8 w-full text-[11px]" value={kind} onChange={(value) => chooseFilter('archive_filter_kind', value)}>
                <option value="">全部对话类型</option>
                {kind && !catalog.conversation_kinds.some((item) => item.value === kind) && <option value={kind}>{kind}</option>}
                {catalog.conversation_kinds.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </KitSelect>
            </label>
            <label className="min-w-0 text-[10.5px] text-fg-4">机器人账号
              <KitSelect aria-label="档案机器人账号" className="field mt-1 !h-8 w-full text-[11px]" value={selfId} onChange={(value) => chooseFilter('archive_filter_self', value)}>
                <option value="">全部账号</option>
                {selfId && !catalog.self_ids.includes(selfId) && <option value={selfId}>{selfId}</option>}
                {catalog.self_ids.map((id) => <option key={id}>{id}</option>)}
              </KitSelect>
            </label>
            <div className="col-span-2 flex items-center justify-between gap-2">
              <span className="truncate text-[10px] text-fg-4">未被采集的消息不在此列表中。</span>
              <div className="flex items-center gap-1">
                <Button size="sm" variant="ghost" type="button" onClick={resetFilters} icon={<RotateCcw className="h-3 w-3" />}>重置筛选</Button>
                <Button size="sm" variant="ghost" type="submit" disabled={loading} icon={<Search className="h-3 w-3" />}>搜索档案</Button>
              </div>
            </div>
          </form>
        </div>

        <div className="scroll min-h-0 flex-1 space-y-1 p-2">
          {error && <div className="p-1"><Notice tone="error">{error}</Notice></div>}
          {catalog.warnings.map((warning) => (
            <div key={warning.platform_id} className="p-1"><Notice tone="warning">{warning.platform_id} 的档案读取失败，列表可能不完整</Notice></div>
          ))}
          {loading && <div className="space-y-2 p-1"><div className="skeleton h-20" /><div className="skeleton h-20" /><div className="skeleton h-20" /></div>}
          {!loading && !catalog.items.length && (
            <InlineEmpty icon={<Archive className="h-6 w-6" />} title="暂无匹配的平台消息档案"
              description="调整平台、对话类型或关键词后重试。未被采集的消息不会出现在档案中。" />
          )}
          {!loading && catalog.items.map((record) => {
            const active = recordKey(record) === selectedKey;
            return (
              <button key={recordKey(record)} type="button" disabled={loading} aria-current={active ? 'true' : undefined}
                onClick={() => select(record)}
                className={cn('group relative flex w-full items-start gap-2.5 rounded-xl px-2.5 py-2.5 text-left transition-colors disabled:opacity-70',
                  active ? 'bg-[rgb(var(--rgb-green)/0.14)] text-fg' : 'text-fg-2 hover:bg-glass-hover hover:text-fg')}>
                {active && <span className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-tint" />}
                <span className={cn('grid h-9 w-9 flex-none place-items-center rounded-xl border border-[rgb(var(--tint)/0.24)] bg-[rgb(var(--tint)/0.14)] text-tint', `tint-${kindTint(record.conversation_kind)}`)}>
                  {record.conversation_kind === 'group' ? <Tags className="h-4 w-4" /> : record.conversation_kind === 'private' ? <MessageSquare className="h-4 w-4" /> : <Archive className="h-4 w-4" />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-[13px] font-medium text-fg">{record.label || record.chat_id}</span>
                    <Badge tone="neutral">{record.conversation_kind_label}</Badge>
                  </span>
                  <span className="mt-0.5 block truncate font-mono text-[10px] text-fg-4">{record.platform_id} · 账号 {record.self_id} · {record.chat_id}</span>
                  <span className="mt-0.5 flex items-center gap-2 text-[10.5px] text-fg-3">
                    <span className="num">{record.message_count} 条可检索消息</span>
                    <span>·</span>
                    {record.last_message_at !== null
                      ? <span className="num" title={formatTime(record.last_message_at)}>{formatRelativeTime(record.last_message_at)}</span>
                      : <span>当前无可检索正文</span>}
                  </span>
                </span>
                <ChevronRight className="mt-1 h-3.5 w-3.5 flex-none text-fg-4 transition-transform group-hover:translate-x-0.5" />
              </button>
            );
          })}
        </div>

        <div className="flex-none border-t border-line px-3 py-2">
          <Pager page={page} pageCount={Math.max(1, Math.ceil(catalog.total / 40))} total={catalog.total} pageSize={40}
            label="个档案" onPage={(next) => setOffset(next * 40)} disabled={loading} prevLabel="上一页档案" nextLabel="下一页档案" />
        </div>
      </div>

      <div className="min-w-0">
        {selected ? <ArchivePanel key={selectedKey} record={selected} refresh={refresh} onChanged={() => setRefresh((value) => value + 1)} />
          : (
            <div className="surface tint-green flex min-h-[320px] h-full flex-col items-center justify-center !rounded-[16px] p-6 text-center">
              <div className="grid h-12 w-12 place-items-center rounded-2xl bg-[rgb(var(--rgb-green)/0.14)] text-green"><Archive className="h-6 w-6" /></div>
              <h3 className="mt-3 text-[15px] font-semibold text-fg">选择一个档案对话</h3>
              <p className="mt-1 max-w-sm text-[12.5px] leading-5 text-fg-3">这里保存平台实际采集的入站消息与确认发送的回复；删除只影响档案检索，模型上下文和平台原消息保持原状。</p>
            </div>
          )}
      </div>
    </div>
  );
}

type PendingAction = { action: 'delete' | 'clear' | 'restore'; revision: number; message_ids?: string[]; backup_id?: string; delete_memories?: boolean; cancel_reminders?: boolean };
type InnerTab = 'messages' | 'summaries' | 'memories' | 'reminders' | 'stickers';

function ArchivePanel(props: { record: PlatformArchiveRecord; refresh: number; onChanged: () => void }) {
  const [view, setView] = useState<InnerTab>('messages');
  const [dirty, setDirty] = useState(false);
  const switchView = (next: InnerTab) => { if (next !== view && (!dirty || confirmDiscard())) { setDirty(false); setView(next); } };
  const tabs: Array<{ value: InnerTab; label: string; icon: ReactNode }> = [
    { value: 'messages', label: '原始消息', icon: <MessageSquare className="h-3.5 w-3.5" /> },
    { value: 'summaries', label: '群摘要', icon: <FileText className="h-3.5 w-3.5" /> },
    ...(props.record.conversation_kind === 'group' ? [
      { value: 'memories' as InnerTab, label: '长期记忆', icon: <Tags className="h-3.5 w-3.5" /> },
      { value: 'reminders' as InnerTab, label: '提醒', icon: <Bell className="h-3.5 w-3.5" /> },
    ] : []),
    { value: 'stickers', label: '表情设置', icon: <Smile className="h-3.5 w-3.5" /> },
  ];
  return (
    <div className="surface tint-green flex min-h-[420px] min-w-0 flex-col overflow-hidden !rounded-[16px] lg:h-full">
      <div className="flex-none border-b border-line p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-fg-3">
              <span className="text-tint">{props.record.type_label || props.record.platform_type}</span>
              <span className="text-fg-4">·</span>
              <span className="font-mono">{props.record.platform_id}</span>
              <span className="text-fg-4">·</span>
              <span>账号 {props.record.self_id}</span>
            </div>
            <h2 className="mt-0.5 break-all text-[17px] font-semibold text-fg">{props.record.label || props.record.chat_id}</h2>
            <p className="mt-0.5 break-all font-mono text-[10.5px] text-fg-4">{props.record.conversation_kind_label} · {props.record.chat_id}</p>
          </div>
          <div className="grid flex-none grid-cols-2 gap-1.5">
            <StatTile label="可检索消息" value={<span className="num">{props.record.message_count}</span>} tint="green" />
            <StatTile label="修订" value={<span className="num">{props.record.revision}</span>} />
          </div>
        </div>
        <div className="scroll mt-2.5 max-w-full overflow-x-auto pb-0.5">
          <div className="flex w-max items-center gap-1 rounded-[12px] border border-line bg-glass p-1" aria-label="平台对话内容">
            {tabs.map((item) => (
              <Button key={item.value} size="sm" variant={view === item.value ? 'tinted' : 'ghost'} tint={view === item.value ? 'green' : undefined}
                aria-pressed={view === item.value} onClick={() => switchView(item.value)} icon={item.icon}>{item.label}</Button>
            ))}
          </div>
        </div>
      </div>
      <div className="scroll min-h-0 flex-1 p-3">
        {view === 'messages' ? <ArchiveDetail {...props} />
          : view === 'stickers' ? <StickerSettings record={props.record} onDirty={setDirty} />
            : view === 'memories' ? <GroupMemories record={props.record} refresh={props.refresh} onDirty={setDirty} />
              : view === 'reminders' ? <GroupReminders record={props.record} refresh={props.refresh} onDirty={setDirty} />
                : <GroupSummaries record={props.record} refresh={props.refresh} />}
      </div>
    </div>
  );
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
    controlApi.platformArchiveData(identity, { ...JSON.parse(queryKey), ...(cursor ? { cursor } : {}), limit: 40 }).then((value) => { if (!disposed) setData(value); })
      .catch((reason) => { if (!disposed) setError(errorText(reason)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [identity, queryKey, cursor, refresh, readRevision]);

  const inspect = async (id: string) => {
    const request = ++inspectRequest.current;
    setInspected(undefined); setInspectError(''); setInspecting(true);
    try {
      const result = await controlApi.platformArchiveMessage(identity, id);
      if (mounted.current && inspectRequest.current === request) setInspected(result.item);
    } catch (reason) { if (mounted.current && inspectRequest.current === request) setInspectError(errorText(reason)); }
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
      const result = await controlApi.mutatePlatformArchive(identity, pending.action, pending.revision, { message_ids: pending.message_ids, backup_id: pending.backup_id,
        ...(pending.action === 'restore' ? {} : { delete_memories: !!pending.delete_memories, cancel_reminders: !!pending.cancel_reminders }) });
      if (!mounted.current) return;
      setNotice(pending.action === 'restore' ? `已恢复 ${result.restored_count || 0} 条记录状态，跳过 ${result.skipped_count || 0} 条`
        : `已从档案删除 ${result.deleted_count || 0} 条消息，可在 ${result.expires_at ? formatTime(result.expires_at) : '备份有效期'} 前恢复`
          + (pending.delete_memories ? `；另已永久删除 ${result.deleted_memory_count || 0} 条长期记忆，清理 ${result.cleared_memory_proposal_count || 0} 条提案` : '')
          + (pending.cancel_reminders ? `；另已取消 ${result.cancelled_reminder_count || 0} 个提醒` + (result.sending_reminder_count ? `，${result.sending_reminder_count} 个已经开始发送，无法保证撤回` : '') : ''));
      setPending(undefined); setInspected(undefined); setCursors([]); setSelection([]); onChanged();
    } catch (reason) { if (mounted.current) setActionError(`${errorText(reason)}；如状态已变化，请取消并刷新后重新选择操作`); }
    finally { if (mounted.current) setBusy(false); }
  };

  return (
    <>
      <form className="grid gap-2 sm:grid-cols-2" onSubmit={(event) => {
        event.preventDefault();
        try {
          const next = { ...(keyword ? { keyword } : {}), ...(sender.trim() ? { sender_id: sender.trim() } : {}), ...(start ? { start_time: new Date(start).toISOString() } : {}), ...(end ? { end_time: new Date(end).toISOString() } : {}) };
          if (start && end && new Date(start) > new Date(end)) throw new Error('开始时间不能晚于结束时间');
          setQuery(next); setCursors([]); setNotice(''); setReadRevision((value) => value + 1);
        } catch (reason) { setError(errorText(reason)); }
      }}>
        <label className="min-w-0 text-[10.5px] text-fg-4">消息关键词
          <input aria-label="档案消息关键词" className="field mt-1 !h-8 w-full text-[12px]" value={keyword} onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label className="min-w-0 text-[10.5px] text-fg-4">发送者 ID
          <input aria-label="档案发送者 ID" className="field mt-1 !h-8 w-full text-[12px]" value={sender} onChange={(event) => setSender(event.target.value)} />
        </label>
        <label className="min-w-0 text-[10.5px] text-fg-4">开始时间
          <input aria-label="档案开始时间" type="datetime-local" className="field mt-1 !h-8 w-full text-[12px]" value={start} onChange={(event) => setStart(event.target.value)} />
        </label>
        <label className="min-w-0 text-[10.5px] text-fg-4">结束时间
          <input aria-label="档案结束时间" type="datetime-local" className="field mt-1 !h-8 w-full text-[12px]" value={end} onChange={(event) => setEnd(event.target.value)} />
        </label>
        <p className="text-[10.5px] leading-4 text-fg-4 sm:col-span-2">时间使用当前设备时区（{Intl.DateTimeFormat().resolvedOptions().timeZone}），查询会转换为带时区的时间。</p>
        <div className="flex flex-wrap gap-1.5 sm:col-span-2">
          <Button type="submit" size="sm" variant="tinted" tint="green" disabled={loading || busy} icon={<Search className="h-3.5 w-3.5" />}>查询消息</Button>
          <Button type="button" size="sm" variant="ghost" disabled={loading || busy}
            onClick={() => { setKeyword(''); setSender(''); setStart(''); setEnd(''); setQuery({}); setCursors([]); onChanged(); }}>重置消息筛选</Button>
          <Button type="button" size="sm" variant="ghost" disabled={loading || busy} onClick={onChanged} icon={<RefreshCw className="h-3.5 w-3.5" />}>刷新消息</Button>
        </div>
      </form>

      {loading && <div className="mt-2.5 space-y-2"><div className="skeleton h-20" /><div className="skeleton h-20" /></div>}
      {error && <Notice tone="error" className="mt-2.5">{error}</Notice>}
      {notice && <Notice tone="success" className="mt-2.5">{notice}</Notice>}

      {data && (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-line bg-glass px-3 py-2 text-[11px] text-fg-4">
            <span>档案保留 {data.retention_days} 天</span>
            <span className="flex items-center gap-1.5 text-green"><span className="dot tint-green !h-1.5 !w-1.5" data-live="1" />来自存档</span>
            <span className="num">实际采集范围：{data.coverage.archived_from === null ? '暂无可检索消息' : `${formatTime(data.coverage.archived_from)} 至 ${formatTime(data.coverage.archived_to ?? data.coverage.archived_from)}`}</span>
          </div>
          <div className="grid gap-1.5 rounded-xl border border-line bg-glass p-3">
            <KeyValue label="档案范围">{data.coverage.complete ? '覆盖完整' : '可能包含缺失消息'}</KeyValue>
            <KeyValue label="查询消息">{data.items.length} 条本页 · {data.has_more ? '还有更早消息' : '已到末尾'}</KeyValue>
            {data.truncated && <KeyValue label="正文预算">部分消息正文超出本次查询预算</KeyValue>}
          </div>
          {data.truncated && <Notice tone="warning">部分消息正文超出本次查询预算，查看消息详情可读取单条存档内容。</Notice>}

          <div className="flex flex-wrap items-center gap-1.5">
            <Button variant="danger" size="sm" disabled={!selection.length || busy || loading} onClick={() => prepare('delete', { message_ids: [...selection] })}>删除所选档案（{selection.length}）</Button>
            <Button variant="danger" size="sm" disabled={busy || loading} onClick={() => prepare('clear')}>清空此对话全部档案</Button>
            <span className="num ml-auto text-[10.5px] text-fg-4">修订 {data.revision}</span>
          </div>

          {!data.items.length && <p className="rounded-xl border border-dashed border-[var(--glass-border-strong)] px-4 py-6 text-center text-[12px] text-fg-4">当前条件下没有可检索消息</p>}

          <div className="space-y-2.5">
            {data.items.map((message) => (
              <div key={message.message_id} className="rounded-xl border border-line bg-glass p-3">
                <label className="flex items-center gap-2 text-[11px] text-fg-3">
                  <input type="checkbox" className="h-3.5 w-3.5 accent-green" aria-label={`选择档案消息 ${message.message_id}`} disabled={busy || loading}
                    checked={selection.includes(message.message_id)}
                    onChange={(event) => setSelection((values) => event.target.checked ? [...values, message.message_id] : values.filter((id) => id !== message.message_id))} />
                  <span className="truncate font-mono">{message.message_id}</span>
                  <Badge tone={message.status === 'active' ? 'green' : 'orange'}>{stateNames[message.status]}</Badge>
                </label>
                <div className="mt-2">
                  <ArchiveMessage message={message} onQuote={inspect} />
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => inspect(message.message_id)}>查看消息详情</Button>
                  <Button size="sm" variant="danger" disabled={busy || loading} onClick={() => prepare('delete', { message_ids: [message.message_id] })}>删除这条档案</Button>
                </div>
              </div>
            ))}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="num text-[11px] text-fg-4">{data.items.length} 条本页 · {data.has_more ? '还有更早消息' : '已到末尾'}</span>
            <div className="flex items-center gap-1.5">
              <Button size="sm" variant="ghost" disabled={!cursors.length || loading || busy} onClick={() => setCursors((values) => values.slice(0, -1))}>较新消息</Button>
              <Button size="sm" variant="ghost" disabled={!data.has_more || !data.next_cursor || loading || busy}
                onClick={() => { if (data.next_cursor) setCursors((values) => [...values, data.next_cursor!]); }}>较早消息</Button>
            </div>
          </div>

          <details className="rounded-xl border border-line bg-glass px-3 py-2">
            <summary className="cursor-pointer text-[12px] font-medium text-fg-2">可恢复的档案删除（{data.backups.length}）</summary>
            <p className="my-2 text-[11px] leading-5 text-fg-4">恢复保留删除时的记录状态，不覆盖后来收到的消息或平台撤回。被后续操作覆盖的备份需先处理后续操作。</p>
            {data.backups.length === 0 && <p className="pb-2 text-[11.5px] text-fg-4">当前没有可恢复的删除。</p>}
            {data.backups.map((backup) => (
              <div key={backup.backup_id} className="flex flex-wrap items-center justify-between gap-2 border-t border-line py-2">
                <span className="text-[11px] text-fg-3">{backup.action === 'clear' ? '清空对话' : '删除消息'} · {formatTime(backup.created_at)}<span className="ml-1 text-fg-4">有效至 {formatTime(backup.expires_at)}</span></span>
                <Button size="sm" variant="ghost" disabled={busy || loading} onClick={() => prepare('restore', { backup_id: backup.backup_id })}>恢复此删除</Button>
              </div>
            ))}
          </details>
        </div>
      )}

      <Modal open={!!pending} onClose={() => { if (!busy) setPending(undefined); }} title={pending?.action === 'restore' ? '恢复平台消息档案' : '删除平台消息档案'}
        icon={<Archive className="h-[18px] w-[18px]" />} className={pending?.action === 'restore' ? 'tint-green' : 'tint-red'}>
        <div className="space-y-3">
          <div className="break-all rounded-xl border border-line bg-glass p-2.5 text-[11.5px] text-fg-3">
            {record.platform_id} / {record.self_id} / {record.conversation_kind_label} / {record.chat_id}
          </div>
          <p className="text-[13px] leading-6 text-fg-2">{pending?.action === 'clear' ? '这会清空此对话的全部档案，不受当前消息筛选影响。' : pending?.action === 'delete' ? `这会删除选中的 ${pending.message_ids?.length || 0} 条档案消息。` : '这会恢复仍归属该删除操作且未过期的记录状态。'}</p>
          <p className="text-[11.5px] leading-5 text-fg-3">档案操作影响本地消息及工具检索，模型上下文和平台原消息保持原状。删除备份按档案保留期过期。</p>
          {pending && pending.action !== 'restore' && record.conversation_kind === 'group' && (
            <div className="space-y-2">
              <label className="flex items-start gap-2 text-[12.5px] leading-5 text-fg-2">
                <input type="checkbox" className="mt-0.5 h-3.5 w-3.5 accent-green" disabled={busy} checked={!!pending.delete_memories}
                  onChange={(event) => setPending({ ...pending, delete_memories: event.target.checked })} />
                {pending.action === 'clear' ? '同时永久删除本群全部长期记忆与记忆提案' : '同时永久删除引用所选消息的长期记忆与记忆提案'}
              </label>
              <label className="flex items-start gap-2 text-[12.5px] leading-5 text-fg-2">
                <input type="checkbox" className="mt-0.5 h-3.5 w-3.5 accent-green" disabled={busy} checked={!!pending.cancel_reminders}
                  onChange={(event) => setPending({ ...pending, cancel_reminders: event.target.checked })} />
                {pending.action === 'clear' ? '同时取消本群尚未开始发送的提醒' : '同时取消来源为所选消息且尚未开始发送的提醒'}
              </label>
              <p className="text-[11px] leading-4 text-fg-4">默认保留长期记忆和提醒，仅标注来源不可用。勾选后删除的记忆与取消的提醒不会随档案恢复；已开始发送的提醒无法保证撤回。</p>
            </div>
          )}
          {actionError && <p role="alert" className="rounded-xl border border-[rgb(var(--rgb-red)/0.25)] bg-[rgb(var(--rgb-red)/0.08)] px-3 py-2 text-[12.5px] text-red">{actionError}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" disabled={busy} onClick={() => setPending(undefined)}>取消</Button>
            <Button variant={pending?.action === 'restore' ? 'tinted' : 'danger'} tint={pending?.action === 'restore' ? 'green' : undefined}
              disabled={busy} onClick={mutate}>{busy ? '正在处理…' : pending?.action === 'restore' ? '确认恢复' : '确认删除档案'}</Button>
          </div>
        </div>
      </Modal>

      <Modal open={inspecting || !!inspected || !!inspectError}
        onClose={() => { inspectRequest.current += 1; setInspecting(false); setInspected(undefined); setInspectError(''); }}
        title="平台消息详情" size="lg" icon={<Search className="h-[18px] w-[18px]" />} className="tint-green">
        <div className="space-y-3">
          {inspecting && <p role="status" className="text-[12.5px] text-fg-3">正在读取消息详情…</p>}
          {inspectError && <Notice tone="error">{inspectError}</Notice>}
          {inspected && (
            <>
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-fg-3">
                <Badge tone={inspected.direction === 'inbound' ? 'green' : 'accent'}>{inspected.direction === 'inbound' ? '收到消息' : '机器人发出'}</Badge>
                <Badge tone={inspected.status === 'active' ? 'green' : 'orange'}>{stateNames[inspected.status]}</Badge>
                <span className="break-all font-mono">{inspected.message_id}</span>
                <span className="ml-auto">{formatTime(inspected.message_time)}</span>
              </div>
              <div className="rounded-xl border border-line bg-glass p-3">
                <ArchiveMessage message={inspected} onQuote={inspect} />
              </div>
              <div className="grid gap-1.5 rounded-xl border border-line bg-glass p-3">
                <KeyValue label="来源" mono>{inspected.source}</KeyValue>
                <KeyValue label="时间来源">{inspected.time_source === 'platform' ? '平台时间' : '本地采集时间'}</KeyValue>
                <KeyValue label="归属核验">{inspected.verified ? '已核验' : '未核验'}</KeyValue>
              </div>
              <details className="rounded-xl border border-line bg-glass px-3 py-2">
                <summary className="cursor-pointer text-[11.5px] text-fg-3">组件与媒体摘要</summary>
                <pre className="scroll mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-all font-mono text-[10.5px] leading-5 text-fg-3">{JSON.stringify({ components: inspected.components, media: inspected.media }, null, 2)}</pre>
              </details>
            </>
          )}
        </div>
      </Modal>
    </>
  );
}

function ArchiveMessage({ message, onQuote }: { message: PlatformArchiveMessage; onQuote: (id: string) => void }) {
  return (
    <div className="min-w-0 space-y-1.5">
      <p className="break-all text-[11px] text-fg-3">
        <span className="text-fg-4">{message.direction === 'outbound' ? '机器人发出' : '收到消息'} · </span>
        {message.card || message.nickname || message.sender_id || '发送者资料已擦除'}{message.sender_id && `（${message.sender_id}）`}
      </p>
      <p className="text-[10.5px] text-fg-4">{formatTime(message.message_time)} · {message.time_source === 'platform' ? '平台时间' : '本地采集时间'}</p>
      {message.status === 'active'
        ? <p className="whitespace-pre-wrap break-words text-[13px] leading-6 text-fg">{message.text || '这条消息没有文本正文'}</p>
        : <p className="text-[12.5px] leading-5 text-fg-3">{stateNames[message.status]}，正文不可检索</p>}
      <div className="flex flex-wrap items-center gap-1.5">
        {message.reply_to_message_id && (
          <Button variant="ghost" size="sm" onClick={() => onQuote(message.reply_to_message_id!)} icon={<CornerUpLeft className="h-3 w-3" />}>查看引用消息 {message.reply_to_message_id}</Button>
        )}
        {!!message.mentions?.length && <span className="break-all text-[10.5px] text-fg-4">提及对象：{message.mentions.join('、')}</span>}
        {!!message.media?.length && <span className="text-[10.5px] text-fg-4">附带 {message.media.length} 个媒体组件，档案保存引用摘要</span>}
        {message.truncated && <Badge tone="orange">正文或组件摘要已截断</Badge>}
      </div>
    </div>
  );
}
