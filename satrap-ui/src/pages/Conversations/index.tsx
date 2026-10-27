import { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  Archive, ArrowUpRight, ChevronRight, Inbox, Layers, MessageSquare, RefreshCw, RotateCcw, Users,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Button, IconButton } from '@/components/kit/Button';
import { Badge } from '@/components/kit/Badge';
import { PageHeader, EmptyState } from '@/components/kit/Feedback';
import { controlApi } from '@/api/control';
import { historyApi } from '@/api/contracts';
import type { ConversationAggregates } from '@/api/contracts';
import type { ConversationCatalog, ConversationPlatform, ConversationRecord } from '@/api/types';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';
import { formatTime, formatRelativeTime } from '@/utils/format';
import { ConversationReader } from './Reader';
import { UserView } from './UserView';
import { ArchiveView } from './ArchiveView';
import { InlineEmpty, Notice, Pager, SearchField } from './common';

type View = 'threads' | 'users' | 'archive';
const PAGE = 40;
const VIEWS: Array<{ value: View; label: string; icon: typeof MessageSquare }> = [
  { value: 'threads', label: '按对话', icon: MessageSquare },
  { value: 'users', label: '按用户', icon: Users },
  { value: 'archive', label: '平台消息档案', icon: Archive },
];
const facetNames: Record<string, string> = {
  agent: 'Agent', provider: '流程', scope: '会话范围', conversation_kind: '对话类型', target: '群 / 目标',
  user: '用户', project: '项目', model: '模型', source: '来源', kind: '记录类别', channel: '频道',
};

export function Conversations() {
  const [search, setSearch] = useSearchParams();
  const rawView = search.get('view');
  const view: View = rawView === 'users' || rawView === 'archive' ? rawView : 'threads';
  const platform = search.get('platform') || '';
  const platformType = search.get('type') || '';
  const selected = search.get('conversation') || '';
  const recordPlatform = search.get('record_platform') || platform;
  const filters = Object.fromEntries([...search.entries()].filter(([key]) => key.startsWith('f.')).map(([key, value]) => [key.slice(2), value]));
  const filtersKey = JSON.stringify(filters);

  const [platforms, setPlatforms] = useState<ConversationPlatform[]>([]);
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<ConversationCatalog>({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [recordMeta, setRecordMeta] = useState<{ platform: string; selected: string; record?: ConversationRecord; error?: string }>();
  const [aggregates, setAggregates] = useState<{ counts?: ConversationAggregates; complete?: boolean } | null>(null);
  const detailRef = useRef<HTMLElement>(null);
  const dirty = useRef({ user: false, context: false });
  const setUserDirty = (value: boolean) => { dirty.current.user = value; };
  const setContextDirty = (value: boolean) => { dirty.current.context = value; };
  const hasUnsaved = () => dirty.current.user || dirty.current.context;
  useDirtyGuard(hasUnsaved);

  useEffect(() => {
    const timer = window.setTimeout(() => { setSubmitted(query); setOffset(0); }, 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  const chooseFilter = (key: string, value: string) => {
    const next = new URLSearchParams(search);
    if (value) next.set(key, value); else next.delete(key);
    next.delete('conversation');
    next.delete('record_platform');
    next.delete('user'); next.delete('user_platform');
    if (key === 'type') next.delete('platform');
    if (key === 'type' || key === 'platform') [...next.keys()].filter((name) => name.startsWith('f.')).forEach((name) => next.delete(name));
    setSearch(next); setOffset(0);
  };

  const chooseView = (next: View) => {
    const params = new URLSearchParams(search);
    if (next === 'threads') params.delete('view'); else params.set('view', next);
    ['conversation', 'record_platform', 'user', 'user_platform', 'archive_platform', 'archive_self', 'archive_kind', 'archive_chat',
      'archive_filter_kind', 'archive_filter_self'].forEach((key) => params.delete(key));
    [...params.keys()].filter((name) => name.startsWith('f.')).forEach((name) => params.delete(name));
    setSearch(params); setOffset(0); setQuery(''); setError('');
  };

  const resetFilters = () => {
    const params = new URLSearchParams(search);
    ['type', 'platform', 'conversation', 'record_platform', 'user', 'user_platform',
      ...Object.keys(filters).map((key) => `f.${key}`)].forEach((key) => params.delete(key));
    setSearch(params); setQuery(''); setSubmitted(''); setOffset(0); setError('');
  };

  const openRecord = (item: ConversationRecord) => {
    const next = new URLSearchParams(search);
    next.set('record_platform', item.platform_id || platform);
    next.set('conversation', item.conversation_id);
    setSearch(next);
  };

  useEffect(() => {
    let disposed = false;
    controlApi.listConversationPlatforms().then((data) => {
      if (disposed) return;
      setPlatforms(data.items || data.platforms.map((id) => ({ id, type: 'unknown', type_label: '未知类型', label: id, supports_history: false })));
      if (!data.items && data.platforms.length) setSearch((current) => {
        const next = new URLSearchParams(current);
        if (!next.get('platform')) next.set('platform', data.platforms[0]);
        return next;
      });
    }).catch((reason) => { if (!disposed) setError(errorText(reason)); });
    return () => { disposed = true; };
  }, [setSearch]);

  useEffect(() => {
    if (view !== 'threads') { setLoading(false); return; }
    let disposed = false;
    setLoading(true); setError('');
    controlApi.listConversationRecords(platform, submitted, offset, { type: platformType, filters: JSON.parse(filtersKey) })
      .then((data) => { if (!disposed) setResult({ ...data, items: data.items.map((item) => ({ ...item, platform_id: item.platform_id || platform })) }); })
      .catch((reason) => { if (!disposed) setError(errorText(reason)); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [platform, platformType, submitted, offset, filtersKey, refresh, view]);

  // 目录聚合: 覆盖全部匹配结果; aggregates_complete=false 时明确只覆盖成功来源
  useEffect(() => {
    if (view !== 'threads') { setAggregates(null); return; }
    let disposed = false;
    const params: Record<string, string | number | undefined> = {
      platform_id: platform || undefined,
      scope: platform ? undefined : 'all',
      platform_type: platformType || undefined,
      q: submitted || undefined,
    };
    Object.entries(filters).forEach(([key, value]) => { if (value) params[`filter.${key}`] = value; });
    historyApi.aggregates(params).then((data) => {
      if (!disposed) setAggregates({ counts: data.aggregates, complete: data.aggregates_complete });
    }).catch(() => { if (!disposed) setAggregates({ counts: undefined, complete: false }); });
    return () => { disposed = true; };
  }, [view, platform, platformType, submitted, filtersKey]);

  useEffect(() => {
    if (view === 'archive' || !selected || !recordPlatform) return;
    let disposed = false;
    setRecordMeta((current) => current?.platform === recordPlatform && current.selected === selected
      ? { ...current, error: undefined } : { platform: recordPlatform, selected });
    controlApi.listConversationRecords(recordPlatform, '', 0, { filters: { kind: 'all' }, conversation: selected }).then((data) => {
      const record = data.items.find((item) => item.conversation_id === selected || item.context_ids.includes(selected));
      if (!record) throw new Error('对话不存在或已被移除，请刷新后重试');
      if (!disposed) setRecordMeta({ platform: recordPlatform, selected, record });
    }).catch((reason) => {
      if (!disposed) setRecordMeta((current) => ({
        ...(current?.platform === recordPlatform && current.selected === selected ? current : {}),
        platform: recordPlatform, selected, error: errorText(reason),
      }));
    });
    return () => { disposed = true; };
  }, [selected, recordPlatform, refresh, view]);

  useEffect(() => {
    if (view === 'users' && selected && recordPlatform) {
      detailRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
      detailRef.current?.focus({ preventScroll: true });
    }
  }, [view, selected, recordPlatform]);

  const detail = recordMeta?.platform === recordPlatform && recordMeta.selected === selected ? recordMeta : undefined;
  const record = detail?.record;
  const names = { ...facetNames, ...result.facet_names };
  const types = [...new Map(platforms.map((item) => [item.type, item.type_label])).entries()];
  const page = Math.max(0, Math.floor(offset / PAGE));

  const viewTabs = (
    <div className="flex flex-wrap items-center gap-2 animate-rise" style={{ animationDelay: '40ms' }}>
      <div className="flex items-center gap-1 rounded-[12px] border border-line bg-glass p-1" aria-label="记录查看方式">
        {VIEWS.map((item) => {
          const Icon = item.icon;
          const active = view === item.value;
          return (
            <Button key={item.value} size="sm" variant={active ? 'tinted' : 'ghost'} tint={active ? 'green' : undefined}
              aria-pressed={active} onClick={() => chooseView(item.value)} icon={<Icon className="h-3.5 w-3.5" />}>
              {item.label}
            </Button>
          );
        })}
      </div>
      <div className="ml-auto hidden items-center gap-3 text-[11px] text-fg-4 sm:flex">
        {view === 'threads' && <>
          <span className="num">平台 <span className="font-mono text-fg-2">{aggregates?.counts ? aggregates.counts.platforms : '—'}</span></span>
          <span className="num">对话 <span className="font-mono text-fg-2">{aggregates?.counts ? aggregates.counts.conversations : '—'}</span></span>
          <span className="num">上下文 <span className="font-mono text-fg-2">{aggregates?.counts ? aggregates.counts.contexts : '—'}</span></span>
          <span className="num">展示轮次 <span className="font-mono text-fg-2">{aggregates?.counts ? aggregates.counts.display_turns : '—'}</span></span>
          {aggregates && aggregates.complete === false && <Badge tone="orange">部分来源</Badge>}
        </>}
      </div>
    </div>
  );

  if (view === 'archive') {
    return (
      <div className="tint-green space-y-4">
        <PageHeader title="对话记录" description="按平台与归属查看和维护模型上下文、Chat 展示历史、长期记忆与平台消息档案" />
        {viewTabs}
        {platforms.length === 0 && error && <Notice tone="error">{error}</Notice>}
        <ArchiveView platforms={platforms} />
      </div>
    );
  }

  const rail = (
    <FilterRail
      platforms={platforms} types={types} platformType={platformType} platformId={platform}
      onType={(value) => chooseFilter('type', value)} onPlatform={(value) => chooseFilter('platform', value)}
      onReset={resetFilters} showRecordFilters={view === 'threads'}
    />
  );

  if (view === 'users') {
    return (
      <div className="tint-green space-y-4">
        <PageHeader title="对话记录" description="按平台与归属查看和维护模型上下文、Chat 展示历史、长期记忆与平台消息档案"
          actions={<Link to="/conversations/instances" className="inline-flex">
            <Button variant="ghost" icon={<ArrowUpRight className="h-4 w-4" />}>管理对话实例</Button>
          </Link>} />
        {viewTabs}
        <div className="min-w-0 space-y-3">
          <div className="grid items-start gap-3 lg:grid-cols-[326px_minmax(0,1fr)]">
            <UserView platform={platform} platformType={platformType} platforms={platforms}
              onType={(value) => chooseFilter('type', value)} onPlatform={(value) => chooseFilter('platform', value)}
              onDirty={setUserDirty}
              onOpen={(targetPlatform, conversation) => {
                const next = new URLSearchParams(search);
                next.set('record_platform', targetPlatform); next.set('conversation', conversation);
                setSearch(next);
              }} />
          </div>
          {selected && recordPlatform && detailPanel()}
        </div>
      </div>
    );
  }

  function detailPanel() {
    return (
      <section ref={detailRef} tabIndex={-1} aria-label="对话详情" className="min-w-0 scroll-mt-4 outline-none lg:h-full">
        {record && detail?.error && (
          <Notice tone="error" className="mb-3">详情刷新失败：{detail.error}
            <Button className="ml-2" size="sm" onClick={() => setRefresh((value) => value + 1)}>重试读取对话</Button>
          </Notice>
        )}
        {record ? (
          <ConversationReader key={JSON.stringify([recordPlatform, selected])} platform={recordPlatform}
            record={record} requestedContext={selected} onDirty={setContextDirty} onSaved={() => setRefresh((value) => value + 1)} />
        ) : detail?.error ? (
          <div className="surface !rounded-[16px] p-6">
            <p role="alert" className="text-[13px] text-red">{detail.error}</p>
            <Button className="mt-3" variant="tinted" tint="green" onClick={() => setRefresh((value) => value + 1)}>重试读取对话</Button>
          </div>
        ) : (
          <div className="surface !rounded-[16px] p-6"><p role="status" className="text-[13px] text-fg-3">正在读取对话详情…</p></div>
        )}
      </section>
    );
  }

  return (
    <div className="tint-green space-y-4">
      <PageHeader
        title="对话记录"
        description="按平台与归属查看和维护模型上下文、Chat 展示历史、长期记忆与平台消息档案"
        actions={<>
          <Badge tone="green" dot live>数据同步正常</Badge>
          <Button icon={<RefreshCw className={cn('h-4 w-4', loading && 'animate-spin-slow')} />}
            onClick={() => { setRefresh((value) => value + 1); }} disabled={loading}>刷新</Button>
          <Link to="/conversations/instances" className="inline-flex">
            <Button variant="ghost" icon={<ArrowUpRight className="h-4 w-4" />}>管理对话实例</Button>
          </Link>
        </>}
      />

      {viewTabs}

      <div className="grid items-start gap-3 lg:h-[calc(100vh-250px)] lg:min-h-[540px] lg:grid-cols-[212px_326px_minmax(0,1fr)]">
        {rail}

        <div className="surface tint-green flex min-h-[420px] min-w-0 flex-col overflow-hidden !rounded-[16px] lg:h-full">
          <div className="flex-none space-y-2 border-b border-line p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <h2 className="flex items-center gap-2 text-[14px] font-semibold text-fg"><MessageSquare className="h-4 w-4 text-tint" />对话</h2>
                <p className="mt-0.5 text-[11px] text-fg-4">共 {result.total} 个 · 按最近活动排序{aggregates && aggregates.complete === false ? ' · 部分来源计数不完整' : ''}</p>
              </div>
              <IconButton label="刷新对话列表" size="sm" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>
                <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin-slow')} />
              </IconButton>
            </div>
            <SearchField value={query} onChange={setQuery} placeholder="搜索 ID、群名、用户或元数据" ariaLabel="搜索对话" />
            <div className="grid grid-cols-2 gap-1.5">
              <label className="min-w-0 text-[10.5px] text-fg-4">平台类型
                <select aria-label="平台类型" className="field mt-1 !h-8 w-full text-[11px]" value={platformType}
                  onChange={(event) => chooseFilter('type', event.target.value)}>
                  <option value="">全部类型</option>
                  {types.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
              <label className="min-w-0 text-[10.5px] text-fg-4">平台实例
                <select aria-label="记录平台" className="field mt-1 !h-8 w-full text-[11px]" value={platform}
                  onChange={(event) => chooseFilter('platform', event.target.value)}>
                  <option value="">全部实例</option>
                  {platform && !platforms.some((item) => item.id === platform) && <option value={platform}>{platform}</option>}
                  {platforms.filter((item) => !platformType || item.type === platformType).map((item) => (
                    <option key={item.id} value={item.id}>{item.label} · {item.type_label}</option>
                  ))}
                </select>
              </label>
            </div>
            {Object.keys(result.facets || {}).length > 0 && (
              <div className="grid grid-cols-2 gap-1.5">
                {Object.entries(result.facets || {}).map(([key, values]) => (
                  <label key={key} className="min-w-0 text-[10.5px] text-fg-4">{names[key] || key}
                    <select aria-label={`筛选${names[key] || key}`} className="field mt-1 !h-8 w-full text-[11px]"
                      value={filters[key] || ''} onChange={(event) => chooseFilter(`f.${key}`, event.target.value)}>
                      <option value="">{key === 'kind' ? '默认（隐藏旧子代理）' : '全部'}</option>
                      {key === 'kind' && <option value="all">全部类别（含旧子代理）</option>}
                      {filters[key] && filters[key] !== 'all' && !values.some((item) => item.value === filters[key]) && (
                        <option value={filters[key]}>{filters[key]}</option>
                      )}
                      {values.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                    </select>
                  </label>
                ))}
              </div>
            )}
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-[10px] text-fg-4">旧数据无法确定的归属与时间会标明未知；旧子代理记录默认隐藏。</span>
              <Button size="sm" variant="ghost" onClick={resetFilters} icon={<RotateCcw className="h-3 w-3" />}>重置全部筛选</Button>
            </div>
          </div>

          <div className="scroll min-h-0 flex-1 space-y-0.5 p-2">
            {error && <div className="px-1 pb-1"><Notice tone="error">{error}</Notice></div>}
            {result.warnings?.map((warning) => <div key={warning} className="px-1 pb-1"><Notice tone="warning">{warning}</Notice></div>)}
            {loading && <div className="space-y-2 p-1"><div className="skeleton h-[74px]" /><div className="skeleton h-[74px]" /><div className="skeleton h-[74px]" /></div>}
            {!loading && !result.items.length && (
              <InlineEmpty icon={<Inbox className="h-6 w-6" />} title="暂无匹配的对话"
                description="调整平台、类型或搜索条件，或重置筛选后重试。旧子代理记录默认隐藏。"
                action={<Button size="sm" variant="tinted" tint="green" onClick={resetFilters}>重置全部筛选</Button>} />
            )}
            {!loading && result.items.map((item) => {
              const active = item.conversation_id === selected && (item.platform_id || platform) === recordPlatform;
              const lastAt = item.last_activity_at ?? null;
              return (
                <button key={JSON.stringify([item.platform_id, item.conversation_id])} type="button" onClick={() => openRecord(item)}
                  aria-current={active ? 'true' : undefined}
                  title={`${item.platform_id || platform} · ${item.conversation_id}`}
                  className={cn('group relative flex w-full items-start gap-2.5 rounded-xl px-2.5 py-2.5 text-left transition-colors',
                    active ? 'bg-[rgb(var(--rgb-green)/0.14)] text-fg' : 'text-fg-2 hover:bg-glass-hover hover:text-fg')}>
                  {active && <span className="absolute left-0 top-1/2 h-6 w-[3px] -translate-y-1/2 rounded-r-full bg-tint" />}
                  <span className={cn('grid h-9 w-9 flex-none place-items-center rounded-xl border border-[rgb(var(--rgb-green)/0.24)] bg-[rgb(var(--rgb-green)/0.14)] text-[13px] font-semibold text-green')}>
                    {item.title.trim().slice(0, 1) || '?'}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-[13px] font-medium text-fg">{item.title}</span>
                      {item.supports_history === false && <Badge tone="neutral">仅上下文</Badge>}
                      <span className="ml-auto"><ChevronRight className="h-3.5 w-3.5 flex-none text-fg-4 transition-transform group-hover:translate-x-0.5" /></span>
                    </span>
                    <span className="mt-0.5 block truncate font-mono text-[10px] text-fg-4">{item.platform_id || platform} · {item.conversation_id}</span>
                    {Object.keys(item.facets || {}).length > 0 && (
                      <span className="mt-1 flex flex-wrap gap-1">
                        {Object.entries(item.facets || {}).flatMap(([key, values]) => values.map((value) => (
                          <Badge key={`${key}:${value}`} tone="neutral">{item.facet_names?.[key] || names[key] || key}: {item.facet_labels?.[`${key}:${value}`] || value}</Badge>
                        )))}
                      </span>
                    )}
                    <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-fg-4">
                      <span className="num">{item.message_count} 条上下文</span>
                      <span>·</span>
                      <span className="num">{item.history_count} 轮历史</span>
                      <span>·</span>
                      {lastAt === null
                        ? <span>活动时间未知</span>
                        : <span className="num" title={formatTime(lastAt)}>{formatRelativeTime(lastAt)}</span>}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>

          <div className="flex-none border-t border-line px-3 py-2">
            <Pager page={page} pageCount={Math.max(1, Math.ceil(result.total / PAGE))} total={result.total} pageSize={PAGE}
              label="个对话" onPage={(next) => setOffset(next * PAGE)} disabled={loading} />
            {aggregates && aggregates.complete === false && (
              <p className="mt-1 text-[10px] leading-4 text-orange">聚合计数不完整：部分平台读取失败，只统计成功读取的来源。</p>
            )}
          </div>
        </div>

        <div className="min-w-0 lg:h-full">
          {selected && recordPlatform ? detailPanel() : (
            <div className="surface tint-green flex h-full min-h-[320px] flex-col items-center justify-center !rounded-[16px] p-6">
              <EmptyState tint="green" icon={<MessageSquare className="h-6 w-6" />} title="从列表选择一个对话"
                description="左侧可按平台、对象与记录类别筛选，选中后在这里查看两层数据与侧栏数据域。" />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function FilterRail({ platforms, types, platformType, platformId, onType, onPlatform, onReset, showRecordFilters }: {
  platforms: ConversationPlatform[]; types: Array<[string, string]>; platformType: string; platformId: string;
  onType: (value: string) => void; onPlatform: (value: string) => void; onReset: () => void; showRecordFilters: boolean;
}) {
  return (
    <div className="surface tint-green hidden min-h-[420px] flex-col overflow-hidden !rounded-[16px] lg:flex lg:h-full">
      <div className="flex-none border-b border-line p-3">
        <div className="flex items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 text-[13.5px] font-semibold text-fg"><Layers className="h-4 w-4 text-tint" />记录范围</h2>
          <span className="num text-[10.5px] text-fg-4">{platforms.length} 个实例</span>
        </div>
        <p className="mt-0.5 text-[10.5px] leading-4 text-fg-4">平台与对象共同决定读取哪个实例的数据。</p>
      </div>
      <div className="scroll min-h-0 flex-1 space-y-3.5 p-3">
        <section>
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-fg-4">平台类型</p>
          <div className="space-y-1">
            <button type="button" onClick={() => onType('')} aria-pressed={!platformType}
              className={cn('flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[12px] transition-colors',
                !platformType ? 'bg-[rgb(var(--rgb-green)/0.12)] font-medium text-green' : 'text-fg-2 hover:bg-glass-hover hover:text-fg')}>
              <span className={cn('h-1.5 w-1.5 rounded-full', !platformType ? 'bg-green' : 'bg-[var(--text-4)]')} />
              全部平台
            </button>
            {types.map(([type, label]) => {
              const active = platformType === type;
              return (
                <button key={type} type="button" onClick={() => onType(type)} aria-pressed={active}
                  className={cn('flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[12px] transition-colors',
                    active ? 'bg-[rgb(var(--rgb-green)/0.12)] font-medium text-green' : 'text-fg-2 hover:bg-glass-hover hover:text-fg')}>
                  <span className={cn('h-1.5 w-1.5 rounded-full', active ? 'bg-green' : 'bg-[var(--text-4)]')} />
                  {label}
                </button>
              );
            })}
          </div>
        </section>

        <section>
          <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-fg-4">平台实例</p>
          <div className="space-y-0.5">
            {platforms.filter((item) => !platformType || item.type === platformType).map((item) => {
              const active = platformId === item.id;
              return (
                <button key={item.id} type="button" onClick={() => onPlatform(active ? '' : item.id)} aria-pressed={active}
                  className={cn('flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors',
                    active ? 'bg-[rgb(var(--rgb-green)/0.12)]' : 'hover:bg-glass-hover')}>
                  <span className={cn('grid h-7 w-7 flex-none place-items-center rounded-lg border border-[rgb(var(--rgb-green)/0.22)] bg-[rgb(var(--rgb-green)/0.14)] text-[10px] font-semibold text-green')}>
                    {item.label.slice(0, 1)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className={cn('block truncate text-[11.5px] font-medium', active ? 'text-green' : 'text-fg-2')}>{item.label}</span>
                    <span className="block truncate font-mono text-[9.5px] text-fg-4">{item.type_label} · {item.supports_history ? '含展示历史' : '仅上下文'}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </section>

        <Button size="sm" variant="ghost" className="w-full" onClick={onReset} icon={<RotateCcw className="h-3.5 w-3.5" />}>
          {showRecordFilters ? '重置全部筛选' : '重置筛选'}
        </Button>
      </div>
      <div className="flex-none border-t border-line px-3 py-2.5">
        <p className="text-[9.5px] leading-4 text-fg-4">
          {showRecordFilters
            ? <>在群或私聊里发送 <code className="rounded bg-glass-hover px-1 font-mono text-fg-3">/debug on</code> 也可开启记录。</>
            : '档案与用户资料按平台实例隔离，切换实例会重新读取目录。'}
        </p>
        <Link to="/conversations/instances" className="mt-1.5 inline-flex items-center gap-1 text-[10.5px] font-medium text-tint hover:underline">对话实例<ArrowUpRight className="h-3 w-3" /></Link>
      </div>
    </div>
  );
}
