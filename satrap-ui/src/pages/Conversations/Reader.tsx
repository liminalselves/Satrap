import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Bot, Brain, ChevronLeft, ChevronRight, Download,
  MessageSquare, Pencil, RefreshCw, RotateCw, Trash2, Wrench,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Button, IconButton } from '@/components/kit/Button';
import { Badge } from '@/components/kit/Badge';
import { Field, Textarea } from '@/components/kit';
import { Modal } from '@/components/kit/Modal';
import { ConfirmModal } from '@/components/common/ConfirmModal';
import { controlApi } from '@/api/control';
import { historyApi } from '@/api/contracts';
import type { ConversationDataItem, ConversationDataSnapshot, ConversationRecord } from '@/api/types';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';
import { ExportDialog, InlineEmpty, LayerNote, Notice, Pager, SearchField, StatTile } from './common';
import { Select as KitSelect } from '@/components/kit/Select';

type Layer = 'context' | 'history';
type Mode = 'manage' | 'browse';
type Pending = { kind: 'clear' } | { kind: 'delete'; index: number } | { kind: 'restore'; backupId: string };

const PAGE = 50;
const BACKUP_REASONS: Record<string, string> = {
  edit: '编辑前', delete: '删除前', clear: '清空前', restore: '恢复前', checkpoint: '版本快照',
};
const ROLES: Array<{ value: string; label: string }> = [
  { value: 'system', label: '系统提示词' },
  { value: 'developer', label: '开发者提示词' },
  { value: 'user', label: '用户' },
  { value: 'assistant', label: '助手' },
  { value: 'tool', label: '工具结果' },
];
const ROLE_LABELS: Record<string, string> = Object.fromEntries(ROLES.map((item) => [item.value, item.label]));
const roleTint = (role?: string): 'green' | 'purple' | 'teal' | 'neutral' =>
  role === 'user' ? 'green' : role === 'developer' ? 'purple' : role === 'tool' ? 'teal' : role === 'assistant' ? 'green' : 'neutral';

const readable = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';

interface BrowseItem { record_id?: string; id?: number; role?: string; content?: unknown; reasoning_content?: string | null; user_input?: string; answer?: string; thinking?: string | null; variants?: unknown[]; tool_call_records?: unknown[]; created_at?: number; [key: string]: unknown }

export function ConversationReader({ platform, record, requestedContext, onDirty, onSaved }: {
  platform: string; record: ConversationRecord; requestedContext: string;
  onDirty: (dirty: boolean) => void; onSaved: () => void;
}) {
  const supportsHistory = record.supports_history ?? record.history_count > 0;
  const defaultContext = requestedContext !== record.conversation_id && record.context_ids.includes(requestedContext)
    ? requestedContext : record.context_ids[0] || record.conversation_id;
  const [layer, setLayer] = useState<Layer>('context');
  const [context, setContext] = useState(defaultContext);
  const contextChosen = useRef(false);
  const [mode, setMode] = useState<Mode>('manage');
  const [snapshot, setSnapshot] = useState<ConversationDataSnapshot>();
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [query, setQuery] = useState('');
  const [roleFilter, setRoleFilter] = useState('all');
  const [order, setOrder] = useState<'asc' | 'desc'>('asc');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [cursors, setCursors] = useState<string[]>([]);
  const [browse, setBrowse] = useState<{ items: BrowseItem[]; asOf: number | null; hasMore: boolean; nextCursor: string | null }>();
  const [browseLoading, setBrowseLoading] = useState(false);
  const [browseError, setBrowseError] = useState('');
  const [browseVersion, setBrowseVersion] = useState(0);
  const [pending, setPending] = useState<Pending>();
  const [keepSystem, setKeepSystem] = useState(true);
  const [editor, setEditor] = useState<number | null>(null);
  const [form, setForm] = useState({ content: '', format: 'text', reasoning: '', userInput: '', answer: '', thinking: '' });
  const [initial, setInitial] = useState('');
  const [dialogError, setDialogError] = useState('');
  const [exportOpen, setExportOpen] = useState(false);
  const [variantMap, setVariantMap] = useState<Record<number, number>>({});
  const mounted = useRef(true);
  const currentKey = `${platform}:${layer === 'context' ? context : record.conversation_id}:${layer}:${offset}`;
  const keyRef = useRef(currentKey);
  keyRef.current = currentKey;

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => () => onDirty(false), [onDirty]);
  useEffect(() => { if (!contextChosen.current) setContext(defaultContext); }, [defaultContext]);

  const conversation = layer === 'context' ? context : record.conversation_id;
  const dirty = editor !== null && JSON.stringify(form) !== initial;
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);

  // 编辑/删除/清空仍走原管理接口; 只读浏览使用新的存储浏览契约
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError('');
    controlApi.conversationData(platform, conversation, layer, { offset }).then((data) => {
      if (!disposed) setSnapshot(data);
    }).catch((reason) => { if (!disposed) setError(errorText(reason)); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [platform, conversation, layer, offset, refresh]);

  useEffect(() => {
    const timer = window.setTimeout(() => setAppliedQuery(query.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => { setCursors([]); }, [layer, conversation, appliedQuery, roleFilter, order, mode]);

  const browseCursor = cursors[cursors.length - 1];
  useEffect(() => {
    if (mode !== 'browse') return;
    let disposed = false;
    setBrowseLoading(true); setBrowseError('');
    historyApi.messages<BrowseItem>(platform, conversation, {
      layer,
      q: appliedQuery || undefined,
      role: layer === 'context' && roleFilter !== 'all' ? roleFilter : undefined,
      order, limit: 20, cursor: browseCursor || undefined,
    }).then((data) => {
      if (disposed) return;
      if (!data || !Array.isArray(data.items)) throw new Error('存储浏览接口未返回消息列表');
      setBrowse({ items: data.items, asOf: typeof data.as_of === 'number' ? data.as_of : null, hasMore: !!data.has_more, nextCursor: data.next_cursor ?? null });
    }).catch((reason) => { if (!disposed) { setBrowse(undefined); setBrowseError(errorText(reason, '存储浏览不可用')); } })
      .finally(() => { if (!disposed) setBrowseLoading(false); });
    return () => { disposed = true; };
  }, [mode, platform, conversation, layer, appliedQuery, roleFilter, order, browseCursor, browseVersion]);

  const pageCount = Math.max(1, Math.ceil((snapshot?.total ?? 0) / PAGE));
  const safePage = Math.min(Math.floor(offset / PAGE), pageCount - 1);
  const filteredItems = useMemo(() => {
    const items = snapshot?.items ?? [];
    const q = query.trim().toLowerCase();
    return items.filter((item) => {
      if (layer === 'context' && roleFilter !== 'all' && item.role !== roleFilter) return false;
      if (!q) return true;
      const text = layer === 'context'
        ? `${readable(item.content)} ${item.reasoning_content ?? ''}`.toLowerCase()
        : `${item.user_input ?? ''} ${item.answer ?? ''} ${item.thinking ?? ''}`.toLowerCase();
      return text.includes(q);
    });
  }, [snapshot, query, roleFilter, layer]);

  const layerBackups = snapshot?.backups ?? [];
  const sourceLabel = snapshot?.source === 'memory' ? '活动实例最新上下文' : '已保存数据';

  const switchLayer = (next: Layer) => {
    if (next === layer) return;
    if (dirty && !confirmDiscard()) return;
    setEditor(null); setLayer(next); setOffset(0); setQuery(''); setRoleFilter('all'); setNotice(''); setError('');
  };
  const switchContext = (next: string) => {
    if (next === context) return;
    if (dirty && !confirmDiscard()) return;
    setEditor(null); contextChosen.current = true; setContext(next); setOffset(0); setQuery(''); setRoleFilter('all'); setNotice(''); setError('');
  };
  const reload = () => {
    if (dirty && !confirmDiscard()) return;
    setEditor(null); setError(''); setNotice('');
    setRefresh((value) => value + 1);
    if (mode === 'browse') { setBrowseVersion((value) => value + 1); }
    else setOffset(0);
  };

  const mutate = async (data: Record<string, unknown>) => {
    if (!snapshot) return;
    const startKey = keyRef.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await controlApi.conversationData(platform, conversation, layer, { ...data, expected_revision: snapshot.revision, offset });
      if (!mounted.current || keyRef.current !== startKey) return;
      setSnapshot(result); setEditor(null); setNotice('修改已保存，修改前的数据已备份'); onSaved(); setPending(undefined);
      if (mode === 'browse') setBrowseVersion((value) => value + 1);
    } catch (reason) { if (mounted.current && keyRef.current === startKey) setError(errorText(reason)); }
    finally { if (mounted.current) setBusy(false); }
  };

  const openEditor = (index: number) => {
    const item = snapshot?.items.find((entry) => entry.index === index);
    if (!item) return;
    const values = layer === 'context'
      ? { content: readable(item.content), format: item.content === null ? 'null' : typeof item.content === 'string' ? 'text' : 'json', reasoning: item.reasoning_content || '', userInput: '', answer: '', thinking: '' }
      : { content: '', format: 'text', reasoning: '', userInput: item.user_input || '', answer: item.answer || '', thinking: item.thinking || '' };
    setForm(values); setInitial(JSON.stringify(values)); setEditor(index); setDialogError(''); setError('');
  };
  const closeEditor = () => {
    if (busy) return;
    if (dirty && !confirmDiscard()) return;
    setEditor(null); setDialogError(''); setError('');
  };
  const saveEditor = async () => {
    if (editor === null) return;
    let parsed: string | Record<string, unknown> | null = form.content;
    if (layer === 'context') {
      if (form.format === 'null') parsed = null;
      else if (form.format === 'json') {
        try { parsed = JSON.parse(form.content) as Record<string, unknown>; }
        catch { setDialogError('内容不是有效的 JSON，请检查括号与引号'); return; }
      }
    }
    setDialogError('');
    const payload = layer === 'context'
      ? { action: 'edit', index: editor, content: parsed, reasoning_content: form.reasoning || null }
      : { action: 'edit', index: editor, user_input: form.userInput, answer: form.answer, thinking: form.thinking };
    await mutate(payload);
    if (mounted.current) setDialogError('');
  };

  const confirmDelete = () => {
    if (!pending || pending.kind !== 'delete') return;
    void mutate({ action: 'delete', index: pending.index });
  };
  const confirmClear = () => {
    void mutate({ action: 'clear', keep_system: keepSystem });
  };
  const confirmRestore = () => {
    if (!pending || pending.kind !== 'restore') return;
    void mutate({ action: 'restore', backup_id: pending.backupId });
  };

  const displayItems = snapshot?.items ?? [];
  const browseStats = browse ? `${browse.items.length} 条本页 · ${browse.hasMore ? '还有更多' : '已到末尾'}` : '';

  return (
    <div className="flex min-h-0 flex-col gap-3 lg:h-full">
      <div className="tint-green flex-none animate-rise">
        <div className="surface tint-green relative overflow-hidden !rounded-[16px] p-4">
          <div aria-hidden className="pointer-events-none absolute -right-16 -top-20 h-52 w-52 rounded-full bg-[radial-gradient(circle,rgb(var(--rgb-green)/0.3),transparent_68%)] blur-2xl" />
          <div className="relative flex flex-wrap items-start gap-3">
            <div className="grid h-12 w-12 flex-none place-items-center rounded-[15px] border border-[rgb(var(--rgb-green)/0.28)] bg-[linear-gradient(140deg,rgb(var(--rgb-green)/0.32),rgb(var(--rgb-green)/0.08))] text-[18px] font-semibold text-green shadow-[inset_0_1px_0_var(--glass-highlight)]">
              {record.title.slice(0, 1)}
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-fg-3">
                <span className="text-tint">{record.platform_type || platform}</span>
                <span className="text-fg-4">·</span>
                <span>{record.facets?.conversation_kind?.join(' / ') || '对话'}</span>
                <span className="text-fg-4">·</span>
                <span className="font-mono">{record.conversation_id}</span>
              </div>
              <h2 className="mt-0.5 flex flex-wrap items-center gap-2 text-[19px] font-semibold leading-tight text-fg">
                <span className="break-all">{record.title}</span>
                {!supportsHistory && <Badge tone="neutral">仅上下文</Badge>}
                {snapshot?.source === 'memory' && <Badge tone="green" dot live>正在记录</Badge>}
              </h2>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {Object.entries(record.facets || {}).flatMap(([key, values]) => values.map((value) => (
                  <Badge key={`${key}:${value}`} tone="neutral">{record.facet_names?.[key] || key}: {record.facet_labels?.[`${key}:${value}`] || value}</Badge>
                )))}
                {record.tags?.map((tag) => <Badge key={tag} tone="orange">{tag}</Badge>)}
              </div>
            </div>
            <div className="flex flex-none flex-wrap items-center gap-1.5">
              <Button size="sm" variant="tinted" tint="green" icon={<Download className="h-3.5 w-3.5" />}
                onClick={() => setExportOpen(true)} disabled={!snapshot && !browse}>导出</Button>
              <IconButton label="刷新对话" size="sm" onClick={reload} disabled={busy || loading}><RefreshCw className={cn('h-4 w-4', (loading || browseLoading) && 'animate-spin-slow')} /></IconButton>
            </div>
          </div>
          <div className="relative mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <StatTile label="上下文消息" value={<span className="num">{record.message_count}</span>} hint={`${record.context_ids.length} 个范围`} tint="green" />
            <StatTile label="展示历史" value={<span className="num">{record.history_count}</span>} hint={supportsHistory ? '可独立维护' : '平台未提供'} tint="green" />
            <StatTile label="数据类型" value={<span className="text-[13px]">{layer === 'context' ? '模型上下文' : 'Chat 展示历史'}</span>} hint={mode === 'browse' ? '只读浏览' : '可编辑'} />
            <StatTile label="最近活动" value={<span className="text-[13px]">{record.last_activity_at ? formatTime(record.last_activity_at) : '未知'}</span>} hint={record.last_activity_at ? '本地记录时间' : '旧记录缺少时间信息'} />
          </div>
        </div>
      </div>

      <div className="tint-green flex-none animate-rise" style={{ animationDelay: '60ms' }}>
        <div className="surface tint-green !rounded-[16px] p-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1 rounded-[12px] border border-line bg-glass p-1" aria-label="对话数据类型">
              <Button size="sm" variant={layer === 'context' ? 'tinted' : 'ghost'} tint={layer === 'context' ? 'green' : undefined}
                onClick={() => switchLayer('context')} aria-pressed={layer === 'context'}>上下文</Button>
              <Button size="sm" variant={layer === 'history' ? 'tinted' : 'ghost'} tint={layer === 'history' ? 'green' : undefined}
                disabled={!supportsHistory} onClick={() => switchLayer('history')} aria-pressed={layer === 'history'}>Chat 展示历史</Button>
            </div>
            {!supportsHistory && <Badge tone="neutral">平台未提供展示历史</Badge>}
            {layer === 'context' && (
              <label className="min-w-[190px] max-w-full text-[11.5px] text-fg-3">上下文范围
                <KitSelect aria-label="上下文范围" className="field mt-1 !h-9 w-full text-[12.5px]" value={context}
                  onChange={(value) => switchContext(value)}>
                  {[...new Set([context, ...record.context_ids])].map((id) => {
                    const meta = record.contexts?.find((item) => item.id === id);
                    const label = meta?.kind === 'main' ? `主上下文 · ${id}` : meta?.kind === 'shared' ? `会话共享上下文 · ${id}` : meta?.name ? `${meta.name} · ${id}` : id;
                    return <option key={id} value={id}>{label}</option>;
                  })}
                </KitSelect>
              </label>
            )}
            <span className="ml-auto flex flex-wrap items-center gap-2">
              <Link className="text-[11.5px] text-tint hover:underline"
                to={`/conversations/versions?${new URLSearchParams({ platform, conversation: context, context })}`}>版本与恢复</Link>
              <Badge tone={snapshot?.source === 'memory' ? 'green' : 'accent'} dot live={snapshot?.source === 'memory'}>{sourceLabel}</Badge>
            </span>
          </div>

          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            <div className="min-w-[160px] flex-1">
              <SearchField value={query} onChange={setQuery} onEnter={() => { if (mode === 'browse') setBrowseVersion((value) => value + 1); }}
                placeholder={layer === 'context' ? '在当前上下文内搜索内容、思考或工具名' : '在展示历史内搜索输入、回复或思考'} ariaLabel="会话内搜索" />
            </div>
            {layer === 'context' && (
              <KitSelect aria-label="筛选消息角色" className="field !h-9 w-28 text-[12px]" value={roleFilter} onChange={(value) => { setRoleFilter(value); setOffset(0); }}>
                <option value="all">全部角色</option>
                {ROLES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </KitSelect>
            )}
            {mode === 'browse' && (
              <KitSelect aria-label="浏览排序" className="field !h-9 w-28 text-[12px]" value={order} onChange={(value) => setOrder(value as 'asc' | 'desc')}>
                <option value="asc">正序</option>
                <option value="desc">倒序</option>
              </KitSelect>
            )}
            <div className="flex items-center gap-1 rounded-[12px] border border-line bg-glass p-1" aria-label="浏览方式">
              <Button size="sm" variant={mode === 'manage' ? 'tinted' : 'ghost'} tint={mode === 'manage' ? 'green' : undefined} onClick={() => setMode('manage')} aria-pressed={mode === 'manage'}>可编辑</Button>
              <Button size="sm" variant={mode === 'browse' ? 'tinted' : 'ghost'} tint={mode === 'browse' ? 'green' : undefined} onClick={() => setMode('browse')} aria-pressed={mode === 'browse'}>只读浏览</Button>
            </div>
            <Button size="sm" variant="ghost" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={reload} disabled={busy || loading || browseLoading}>重新读取</Button>
            <Button size="sm" variant="danger" icon={<Trash2 className="h-3.5 w-3.5" />}
              disabled={busy || loading || (snapshot?.total ?? 0) === 0}
              onClick={() => { setError(''); setPending({ kind: 'clear' }); }}>
              {layer === 'context' ? '清空上下文' : '删除全部展示历史'}
            </Button>
          </div>
          <LayerNote>
            {layer === 'context'
              ? '修改此处会影响机器人后续使用的上下文，不改写 Chat 展示记录。这里展示保存的完整消息；模型请求还可能应用截断、总结及临时提示。'
              : '修改此处只改变 Chat 页面展示的历史，不改写模型上下文。已有回复版本与工具明细分别保留。'}
          </LayerNote>
        </div>
      </div>

      <div className="surface tint-green flex min-h-[420px] min-w-0 flex-1 flex-col overflow-hidden !rounded-[16px] lg:min-h-0">
        <div className="scroll min-h-0 flex-1 p-3">
          {notice && <Notice tone="success" className="mb-2.5">{notice}</Notice>}
          {error && <Notice tone="error" className="mb-2.5">{error}</Notice>}
          {!supportsHistory && layer === 'context' && (
            <Notice tone="info" className="mb-2.5">平台没有提供 Chat 展示历史；平台聊天原文请在平台消息档案查看。
              <Link className="ml-1 text-tint hover:underline" to={`/conversations?${new URLSearchParams({ view: 'archive', platform })}`}>查看平台消息档案</Link>
            </Notice>
          )}
          {mode === 'browse' && (
            <div className="mb-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-line bg-glass px-3 py-2 text-[11px] text-fg-4">
              <span className="flex items-center gap-1.5 text-green"><span className="dot tint-green !h-1.5 !w-1.5" data-live="1" />来自存储</span>
              <span className="num" title="as_of">截至 {browse?.asOf ? formatTime(browse.asOf) : '—'}</span>
              <span>{browseStats || '只读浏览'}</span>
              <span className="ml-auto">只读浏览不会生成编辑修订号；切换“可编辑”后仍通过原管理接口保存。</span>
            </div>
          )}
          {browseError && <Notice tone="error" className="mb-2.5">{browseError}</Notice>}
          {(mode === 'manage' ? loading : browseLoading) && (
            <div className="space-y-2.5"><div className="skeleton h-24" /><div className="skeleton h-24" /><div className="skeleton h-24" /></div>
          )}

          {mode === 'manage' && !loading && (
            <>
              <p className="num mb-2 text-[11px] text-fg-4">
                共 {snapshot?.total ?? '—'} {layer === 'context' ? '条上下文消息' : '轮展示历史'} · {sourceLabel}
                {snapshot?.read_notice ? ` · ${snapshot.read_notice}` : ''}
              </p>
              {snapshot?.total === 0 && (
                <InlineEmpty icon={<MessageSquare className="h-6 w-6" />}
                  title={layer === 'context' ? '此范围没有上下文消息' : '暂无独立展示历史'}
                  description={layer === 'context' ? '系统提示词也可能由 Agent 配置临时注入。' : '新的对话轮次产生后会显示在这里。'} />
              )}
              {!!snapshot?.total && filteredItems.length === 0 && (
                <InlineEmpty icon={<MessageSquare className="h-6 w-6" />} title="没有匹配的消息"
                  description="本页没有匹配内容；切换只读浏览可检索全部已保存消息。"
                  action={<Button size="sm" variant="tinted" tint="green" onClick={() => { setQuery(''); setRoleFilter('all'); }}>清除筛选</Button>} />
              )}
              {filteredItems.map((item) => layer === 'context'
                ? <ContextRow key={item.index} message={item} busy={busy} onEdit={() => openEditor(item.index)} onDelete={() => { setError(''); setPending({ kind: 'delete', index: item.index }); }} />
                : <HistoryRow key={item.index} turn={item} busy={busy}
                    variant={variantMap[item.index] ?? 0}
                    onVariant={(next) => setVariantMap((map) => ({ ...map, [item.index]: next }))}
                    onEdit={() => openEditor(item.index)}
                    onDelete={() => { setError(''); setPending({ kind: 'delete', index: item.index }); }} />)}
              {!!snapshot?.total && (
                <div className="mt-3">
                  <Pager page={safePage} pageCount={pageCount} total={snapshot.total} pageSize={PAGE}
                    label={layer === 'context' ? '条消息' : '轮历史'}
                    onPage={(page) => setOffset(page * PAGE)} disabled={busy || loading} />
                </div>
              )}
            </>
          )}

          {mode === 'browse' && !browseLoading && browse && (
            <>
              {browse.items.length === 0 && (
                <InlineEmpty icon={<MessageSquare className="h-6 w-6" />} title="没有匹配的消息"
                  description="存储浏览按筛选条件直接读取数据库；清空关键词或角色后再试。" />
              )}
              {browse.items.map((item, index) => layer === 'context'
                ? <ContextRow key={item.record_id || index} message={{ ...(item as unknown as ConversationDataItem), index }} busy onEdit={() => undefined} onDelete={() => undefined} readOnly />
                : <HistoryRow key={item.record_id || index} turn={{ ...(item as unknown as ConversationDataItem), index }} busy
                    variant={0} onVariant={() => undefined} onEdit={() => undefined} onDelete={() => undefined} readOnly />)}
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                <span className="num text-[11px] text-fg-4">{browseStats}</span>
                <div className="flex items-center gap-1.5">
                  <Button size="sm" variant="ghost" disabled={!cursors.length || browseLoading} onClick={() => setCursors((values) => values.slice(0, -1))} icon={<ChevronLeft className="h-3.5 w-3.5" />}>较新消息</Button>
                  <Button size="sm" variant="ghost" disabled={!browse.hasMore || !browse.nextCursor || browseLoading}
                    onClick={() => { if (browse.nextCursor) setCursors((values) => [...values, browse.nextCursor!]); }}>较早消息<ChevronRight className="h-3.5 w-3.5" /></Button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      <div className="surface tint-green flex-none !rounded-[16px] px-3 py-2">
        <details>
          <summary className="cursor-pointer text-[12px] font-medium text-fg-2">修改备份与恢复 ({layerBackups.length})</summary>
          <p className="my-2 text-[11px] leading-5 text-fg-4">备份保存修改或删除前的内容。恢复只作用于当前数据层，并会先备份恢复前的状态。</p>
          {layerBackups.length === 0 && <p className="pb-2 text-[11.5px] text-fg-4">当前层暂无备份。</p>}
          {layerBackups.map((backup) => (
            <div key={backup.id} className="flex flex-wrap items-center justify-between gap-2 border-t border-line py-2">
              <span className="text-[11px] text-fg-3">{formatTime(backup.created_at)} · {BACKUP_REASONS[backup.reason] || backup.reason}</span>
              <Button size="sm" variant="ghost" disabled={busy || !snapshot} onClick={() => { setError(''); setPending({ kind: 'restore', backupId: backup.id }); }} icon={<RotateCw className="h-3.5 w-3.5" />}>恢复备份</Button>
            </div>
          ))}
        </details>
      </div>

      <Modal open={editor !== null} onClose={closeEditor} size="lg"
        title={layer === 'history' ? '编辑展示历史' : '编辑上下文消息'}
        description={layer === 'history' ? '保存仅改变展示记录；当前回复版本同步显示修改，其他版本保留。' : '保存会改变后续模型上下文；角色、工具调用 ID 等结构字段保持原样。'}
        icon={<Pencil className="h-[18px] w-[18px]" />} className="tint-green"
        footer={<><Button variant="ghost" onClick={closeEditor} disabled={busy}>取消</Button>
          <Button variant="tinted" tint="green" loading={busy} disabled={!dirty || busy} onClick={() => void saveEditor()}>保存修改</Button></>}>
        <div className="space-y-3">
          {(dialogError || (error && editor !== null)) && <Notice tone="error">{dialogError || error}</Notice>}
          {layer === 'context' ? (
            <>
              <Field label="内容格式">
                <KitSelect aria-label="内容格式" className="field !h-9 w-full" value={form.format} disabled={busy}
                  onChange={(value) => setForm({ ...form, format: value })}>
                  <option value="text">文本</option>
                  <option value="json">多模态 JSON</option>
                  <option value="null">空值 null</option>
                </KitSelect>
              </Field>
              {form.format !== 'null' && (
                <Field label="消息内容" hint={form.format === 'json' ? '必须是合法 JSON，例如 {"type":"image","url":"…"}' : '支持换行'}>
                  <Textarea rows={8} value={form.content} disabled={busy} onChange={(event) => setForm({ ...form, content: event.target.value })} className="font-mono text-[12.5px]" />
                </Field>
              )}
              <Field label="思考内容" hint="可留空；清空后模型上下文不再包含该段思考">
                <Textarea rows={3} value={form.reasoning} disabled={busy} onChange={(event) => setForm({ ...form, reasoning: event.target.value })} />
              </Field>
            </>
          ) : (
            <>
              <Field label="用户输入"><Textarea rows={3} value={form.userInput} disabled={busy} onChange={(event) => setForm({ ...form, userInput: event.target.value })} /></Field>
              <Field label="助手回复"><Textarea rows={5} value={form.answer} disabled={busy} onChange={(event) => setForm({ ...form, answer: event.target.value })} /></Field>
              <Field label="思考内容"><Textarea rows={3} value={form.thinking} disabled={busy} onChange={(event) => setForm({ ...form, thinking: event.target.value })} /></Field>
            </>
          )}
        </div>
      </Modal>

      <ConfirmModal open={pending?.kind === 'delete'} title={layer === 'context' ? '删除上下文消息' : '删除历史轮次'} busy={busy} error={pending?.kind === 'delete' ? error : undefined}
        message={layer === 'context'
          ? '确定删除此消息吗？工具调用或结果会连同对应的完整调用组删除，修改前会自动备份。'
          : '确定删除此历史轮次及其回复版本和工具明细吗？模型上下文不会改变，删除前会自动备份。'}
        confirmText="确认" onCancel={() => setPending(undefined)} onConfirm={confirmDelete} />

      <ConfirmModal open={pending?.kind === 'clear'} title={layer === 'context' ? '清空上下文' : '删除全部展示历史'} busy={busy} error={pending?.kind === 'clear' ? error : undefined}
        message={layer === 'context'
          ? `确定清空此上下文吗？${keepSystem ? '系统提示词会保留。' : '系统提示词也会删除。'}修改前的数据会备份。`
          : '确定删除此对话的全部展示历史吗？模型上下文不会改变，删除前会备份。'}
        confirmText="确认" onCancel={() => setPending(undefined)} onConfirm={confirmClear}>
        {layer === 'context' && (
          <label className="flex items-start gap-2 rounded-xl border border-line bg-glass px-3 py-2 text-[12.5px] text-fg-2">
            <input type="checkbox" className="mt-0.5 h-3.5 w-3.5 accent-green" checked={keepSystem}
              onChange={(event) => setKeepSystem(event.target.checked)} />
            <span>清空时保留系统提示词<span className="ml-1 text-[11px] text-fg-4">保留 role 为 system 与 developer 的消息</span></span>
          </label>
        )}
        <Notice tone="info">长期记忆、提醒、群摘要与平台档案不会受到影响。</Notice>
      </ConfirmModal>

      <ConfirmModal open={pending?.kind === 'restore'} title="恢复修改备份" busy={busy} variant="primary" error={pending?.kind === 'restore' ? error : undefined}
        message="确定恢复此备份吗？当前数据会先备份，恢复只作用于当前数据层。"
        confirmText="确认" onCancel={() => setPending(undefined)} onConfirm={confirmRestore} />

      <ExportDialog open={exportOpen} onClose={() => setExportOpen(false)} title={record.title} layer={layer}
        payload={mode === 'browse' ? (browse?.items ?? []) : displayItems} />
    </div>
  );
}

function ContextRow({ message, busy, onEdit, onDelete, readOnly }: {
  message: ConversationDataItem; busy?: boolean; onEdit: () => void; onDelete: () => void; readOnly?: boolean;
}) {
  const role = message.role || '';
  const label = ROLE_LABELS[role] || role || '消息';
  const isUser = role === 'user';
  const isTool = role === 'tool' || role === 'function';
  const isSystem = role === 'system' || role === 'developer';
  const tokens = typeof message.tokens === 'number' ? message.tokens : undefined;
  const time = typeof message.created_at === 'number' && message.created_at > 0 ? formatTime(message.created_at) : '时间未知';

  const actions = !readOnly ? (
    <span className="flex items-center gap-0.5 opacity-0 transition-opacity duration-200 group-hover:opacity-100 group-focus-within:opacity-100">
      <IconButton label={`编辑第 ${message.index + 1} 项`} size="sm" onClick={onEdit} disabled={busy}><Pencil className="h-3.5 w-3.5" /></IconButton>
      <IconButton label={`删除第 ${message.index + 1} 项`} size="sm" onClick={onDelete} disabled={busy}><Trash2 className="h-3.5 w-3.5" /></IconButton>
    </span>
  ) : undefined;

  const body = (
    <>
      {message.reasoning_content && (
        <details className="mb-2 rounded-lg border border-[rgb(var(--rgb-purple)/0.2)] bg-[rgb(var(--rgb-purple)/0.06)]">
          <summary className="flex cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-medium text-purple"><Brain className="h-3.5 w-3.5" />思考内容</summary>
          <p className="whitespace-pre-wrap break-words border-t border-[rgb(var(--rgb-purple)/0.16)] px-2.5 py-2 text-[12px] leading-5 text-fg-3">{message.reasoning_content}</p>
        </details>
      )}
      {typeof message.content === 'object' && message.content !== null ? (
        <div className="rounded-xl border border-line bg-[rgb(0_0_0/0.14)] p-2.5 [[data-theme=light]_&]:bg-white/45">
          <pre className="scroll max-h-56 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-fg-3">{readable(message.content)}</pre>
        </div>
      ) : message.content === null ? (
        <p className="text-[12.5px] italic text-fg-4">（空值 null）</p>
      ) : (
        <p className="whitespace-pre-wrap break-words text-[13px] leading-6 text-fg">{String(message.content ?? '') || '（无文本内容）'}</p>
      )}
      <details className="mt-2">
        <summary className="cursor-pointer text-[10.5px] text-fg-4 transition hover:text-fg-2">原始数据与结构字段</summary>
        <pre className="scroll mt-1.5 max-h-52 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-line bg-[rgb(0_0_0/0.14)] p-2 font-mono text-[10.5px] leading-5 text-fg-3 [[data-theme=light]_&]:bg-white/45">
          {JSON.stringify(message, null, 2)}
        </pre>
      </details>
    </>
  );

  if (isSystem) {
    return (
      <article className="group mb-2.5 overflow-hidden rounded-xl border border-[rgb(var(--rgb-purple)/0.22)] bg-[rgb(var(--rgb-purple)/0.06)] p-3">
        <div className="mb-1.5 flex flex-wrap items-center gap-2">
          <Badge tone={role === 'developer' ? 'purple' : 'neutral'}>{label}</Badge>
          <span className="num text-[10.5px] text-fg-4">#{message.index + 1} · {time}{tokens ? ` · ${tokens} tokens` : ''}</span>
          <span className="ml-auto">{actions}</span>
        </div>
        {body}
      </article>
    );
  }

  if (isUser) {
    return (
      <article className="group mb-2.5 flex justify-end">
        <div className="max-w-[86%] min-w-0">
          <div className="mb-1 flex flex-wrap items-center justify-end gap-1.5">
            <span className="num text-[10.5px] text-fg-4">#{message.index + 1} · {time}</span>
            <Badge tone="green">{label}</Badge>
            {actions}
          </div>
          <div className="rounded-[18px] rounded-br-md border border-[rgb(var(--rgb-green)/0.35)] bg-[linear-gradient(135deg,rgb(var(--rgb-green)/0.8),rgb(var(--rgb-green)/0.55))] px-4 py-2.5 text-[13px] leading-6 text-white shadow-[0_10px_28px_-14px_rgb(var(--rgb-green)/0.9),inset_0_1px_0_rgba(255,255,255,0.25)]">
            {typeof message.content === 'object' && message.content !== null
              ? <pre className="scroll max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-black/20 p-2 font-mono text-[10.5px] leading-5 text-white/85">{readable(message.content)}</pre>
              : message.content === null ? <span className="italic text-white/70">（空值 null）</span> : String(message.content ?? '') || '（无文本内容）'}
          </div>
          {message.reasoning_content && (
            <details className="mt-1"><summary className="cursor-pointer text-[10.5px] text-fg-4">思考内容</summary>
              <p className="mt-1 whitespace-pre-wrap break-words rounded-lg border border-line bg-glass p-2 text-[11px] text-fg-3">{message.reasoning_content}</p></details>
          )}
        </div>
      </article>
    );
  }

  return (
    <article className={cn('group mb-2.5 flex gap-2.5', isTool && 'tint-teal')}>
      <div className={cn('mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-[10px] border border-[rgb(var(--tint)/0.24)] bg-[rgb(var(--tint)/0.14)] text-tint', isTool ? 'tint-teal' : 'tint-green')}>
        {isTool ? <Wrench className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex flex-wrap items-center gap-1.5">
          <Badge tone={roleTint(role)}>{label}</Badge>
          <span className="num text-[10.5px] text-fg-4">#{message.index + 1} · {time}</span>
          {tokens ? <span className="num text-[10.5px] text-fg-4">· {tokens} tokens</span> : null}
          <span className="ml-auto">{actions}</span>
        </div>
        <div className={cn('rounded-2xl rounded-tl-md border border-line bg-glass px-3.5 py-2.5', isTool && 'border-[rgb(var(--rgb-teal)/0.22)] bg-[rgb(var(--rgb-teal)/0.05)]')}>
          {body}
        </div>
      </div>
    </article>
  );
}

function HistoryRow({ turn, busy, variant, onVariant, onEdit, onDelete, readOnly }: {
  turn: ConversationDataItem; busy?: boolean; variant: number; onVariant: (next: number) => void;
  onEdit: () => void; onDelete: () => void; readOnly?: boolean;
}) {
  const variants = Array.isArray(turn.variants) ? turn.variants as Array<Record<string, unknown>> : [];
  const answers = [turn.answer || '', ...variants.map((item) => typeof item.answer === 'string' ? item.answer : '')].filter((value, index) => index === 0 || value !== '');
  const current = Math.min(variant, Math.max(0, answers.length - 1));
  const tools = Array.isArray(turn.tool_call_records) ? turn.tool_call_records : [];
  const time = typeof turn.created_at === 'number' && turn.created_at > 0 ? formatTime(turn.created_at) : '';
  return (
    <article className="group mb-3 rounded-2xl border border-line bg-glass p-3">
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <Badge tone="accent">对话轮次 #{turn.index + 1}</Badge>
        {time && <span className="num text-[10.5px] text-fg-4">{time}</span>}
        {answers.length > 1 && (
          <span className="ml-1 flex items-center rounded-lg border border-line bg-glass">
            <IconButton label="上一个回复版本" size="sm" disabled={busy} onClick={() => onVariant((current - 1 + answers.length) % answers.length)}><ChevronLeft className="h-3 w-3" /></IconButton>
            <span className="num px-1 text-[10.5px] text-fg-3">{current + 1}/{answers.length}</span>
            <IconButton label="下一个回复版本" size="sm" disabled={busy} onClick={() => onVariant((current + 1) % answers.length)}><ChevronRight className="h-3 w-3" /></IconButton>
          </span>
        )}
        {!readOnly && (
          <span className="ml-auto flex items-center gap-0.5 opacity-0 transition-opacity duration-200 group-hover:opacity-100 group-focus-within:opacity-100">
            <IconButton label={`编辑第 ${turn.index + 1} 项`} size="sm" onClick={onEdit} disabled={busy}><Pencil className="h-3.5 w-3.5" /></IconButton>
            <IconButton label={`删除第 ${turn.index + 1} 项`} size="sm" onClick={onDelete} disabled={busy}><Trash2 className="h-3.5 w-3.5" /></IconButton>
          </span>
        )}
      </div>

      <div className="mb-2 flex justify-end">
        <div className="max-w-[86%] rounded-[16px] rounded-br-md border border-[rgb(var(--rgb-green)/0.32)] bg-[linear-gradient(135deg,rgb(var(--rgb-green)/0.72),rgb(var(--rgb-green)/0.5))] px-3.5 py-2 text-[12.5px] leading-6 text-white">
          <p className="whitespace-pre-wrap break-words">用户：{turn.user_input}</p>
        </div>
      </div>

      <div className="flex gap-2.5">
        <div className="mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-[10px] tint-green border border-[rgb(var(--tint)/0.24)] bg-[rgb(var(--tint)/0.14)] text-tint"><Bot className="h-3.5 w-3.5" /></div>
        <div className="min-w-0 flex-1">
          {turn.thinking && (
            <details className="mb-2 rounded-lg border border-[rgb(var(--rgb-purple)/0.2)] bg-[rgb(var(--rgb-purple)/0.06)]">
              <summary className="flex cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-medium text-purple"><Brain className="h-3.5 w-3.5" />思考内容</summary>
              <p className="whitespace-pre-wrap break-words border-t border-[rgb(var(--rgb-purple)/0.16)] px-2.5 py-2 text-[12px] leading-5 text-fg-3">{turn.thinking}</p>
            </details>
          )}
          {tools.length > 0 && (
            <details className="mb-2 rounded-lg border border-[rgb(var(--rgb-teal)/0.2)] bg-[rgb(var(--rgb-teal)/0.05)]">
              <summary className="flex cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-medium text-teal"><Wrench className="h-3.5 w-3.5" />记录 {tools.length} 条工具调用</summary>
              <pre className="scroll max-h-48 overflow-auto whitespace-pre-wrap break-all border-t border-[rgb(var(--rgb-teal)/0.16)] px-2.5 py-2 font-mono text-[10.5px] text-fg-3">{JSON.stringify(tools, null, 2)}</pre>
            </details>
          )}
          <div className="rounded-2xl rounded-tl-md border border-line bg-glass px-3.5 py-2.5">
            <p className="whitespace-pre-wrap break-words text-[13px] leading-6 text-fg">助手：{answers[current] || '（无回复内容）'}</p>
          </div>
          {current > 0 && <p className="mt-1 text-[10.5px] text-fg-4">当前展示版本 {current + 1}；其他版本与工具明细分别保留。</p>}
        </div>
      </div>
    </article>
  );
}
