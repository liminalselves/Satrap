import { useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import { controlApi } from '@/api/control';
import type { GroupChatSummary, GroupChatSummaryPage, PlatformArchiveMessage, PlatformArchiveRecord } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Modal } from '@/components/ui/Modal';
import { formatTime } from '@/utils/format';

const errorText = (error: unknown) => axios.isAxiosError<{ error?: string }>(error)
  ? error.response?.data?.error || error.message : error instanceof Error ? error.message : String(error);

export function GroupSummaries({ record, refresh }: { record: PlatformArchiveRecord; refresh: number }) {
  const { platform_id, self_id, conversation_kind, chat_id } = record;
  const identity = useMemo(() => ({ platform_id, self_id, conversation_kind, chat_id }), [platform_id, self_id, conversation_kind, chat_id]);
  const [query, setQuery] = useState('');
  const [keyword, setKeyword] = useState('');
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors[cursors.length - 1];
  const [version, setVersion] = useState(0);
  const [page, setPage] = useState<GroupChatSummaryPage>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [detail, setDetail] = useState<GroupChatSummary>();
  const [source, setSource] = useState<PlatformArchiveMessage>();
  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState('');
  const [pending, setPending] = useState<{ summary: GroupChatSummary; key: string }>();
  const [busy, setBusy] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const mounted = useRef(true);
  const sourceRequest = useRef(0);
  const detailRequest = useRef(0);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError(''); setPage(undefined);
    controlApi.listGroupSummaries(identity, { keyword, limit: 20, ...(cursor ? { cursor } : {}) })
      .then((data) => { if (!disposed) setPage(data); })
      .catch((error) => { if (!disposed) setError(errorText(error)); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [identity, keyword, cursor, refresh, version]);
  const inspect = async (summary: GroupChatSummary) => {
    const request = ++detailRequest.current;
    setError('');
    try {
      const data = await controlApi.groupSummary(identity, summary.summary_id);
      if (mounted.current && request === detailRequest.current) setDetail(data.summary);
    } catch (error) { if (mounted.current && request === detailRequest.current) setError(errorText(error)); }
  };
  const inspectSource = async (messageId: string) => {
    const request = ++sourceRequest.current;
    setSourceOpen(true); setSourceLoading(true); setSourceError(''); setSource(undefined);
    try {
      const data = await controlApi.platformArchiveMessage(identity, messageId);
      if (mounted.current && request === sourceRequest.current) setSource(data.item);
    } catch (error) { if (mounted.current && request === sourceRequest.current) setSourceError(errorText(error)); }
    finally { if (mounted.current && request === sourceRequest.current) setSourceLoading(false); }
  };
  const remove = async () => {
    if (!pending) return;
    setBusy(true); setDeleteError('');
    try {
      await controlApi.deleteGroupSummary(identity, pending.summary, pending.key);
      if (!mounted.current) return;
      setPending(undefined); setDetail(undefined); setCursors([]); setVersion((value) => value + 1); setNotice('摘要已删除，原始消息和模型上下文保持原状');
    } catch (error) { if (mounted.current) setDeleteError(`${errorText(error)}；状态变化时请取消并刷新后重试`); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <Card className="min-w-0 space-y-4">
    <h2 className="break-all text-lg font-medium">{record.label || chat_id} · 群摘要</h2>
    <p className="text-sm text-text-secondary">由群聊 Agent 根据指定时段生成。每条结论附来源；本地记录可能不完整。来源删除、撤回或过期后，关联摘要正文不可用。</p>
    <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); setKeyword(query); setCursors([]); setVersion((value) => value + 1); }}>
      <label className="min-w-0 flex-1 text-sm">标题或摘要文字<input aria-label="摘要关键词" className="glass-input mt-1 w-full" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <Button type="submit" disabled={loading}>查询摘要</Button>
      <Button type="button" disabled={loading} onClick={() => { setDetail(undefined); setVersion((value) => value + 1); }}>刷新摘要</Button>
    </form>
    {loading && <p role="status">正在读取摘要…</p>}
    {error && <p role="alert" className="text-error">{error}</p>}
    {notice && <p role="status" className="text-success">{notice}</p>}
    {page && !page.items.length && <p className="text-sm text-text-secondary">暂无匹配摘要，可在群里请机器人总结指定时段的讨论</p>}
    {page?.items.map((summary) => <div key={summary.summary_id} className="space-y-2 rounded-lg bg-glass p-3">
      <p className="break-words font-medium">{summary.title || '来源已失效的摘要'}</p>
      <p className="text-xs text-text-tertiary">{formatTime(summary.created_at)} · {summary.selection?.selected_count ?? 0} 条来源记录</p>
      <p className={`text-sm ${summary.state === 'active' ? 'text-text-secondary' : 'text-warning'}`}>{summary.state === 'active' ? summary.selection.truncated ? '部分记录摘要' : '已保存；平台历史可能不完整' : '来源已删除、撤回或过期，摘要正文不可用'}</p>
      <div className="flex flex-wrap gap-2"><Button size="sm" onClick={() => inspect(summary)}>查看摘要</Button><Button size="sm" variant="danger" disabled={busy} onClick={() => { setPending({ summary, key: crypto.randomUUID() }); setDeleteError(''); }}>删除摘要</Button></div>
    </div>)}
    <div className="flex justify-between gap-2"><Button size="sm" disabled={!cursors.length || loading} onClick={() => setCursors((values) => values.slice(0, -1))}>上一页摘要</Button><Button size="sm" disabled={!page?.next_cursor || loading} onClick={() => { if (page?.next_cursor) setCursors((values) => [...values, page.next_cursor!]); }}>下一页摘要</Button></div>
    <Modal open={!!detail} title="群摘要详情" size="lg" onClose={() => { detailRequest.current += 1; setDetail(undefined); }}>
      {detail && <div className="space-y-4">
        <h3 className="break-words font-medium">{detail.title || '来源已失效'}</h3>
        {detail.resolved_range && <p className="break-all text-xs text-text-tertiary">讨论范围：{detail.resolved_range.start_time} 至 {detail.resolved_range.end_time}</p>}
        {detail.state !== 'active' ? <p className="text-warning">来源不可用，派生正文已清除</p> : <>
          <p className="text-sm text-warning">{detail.selection.truncated ? '这份摘要只覆盖部分已保存记录。' : '这份摘要基于选取的本地记录。'}平台历史可能存在缺失，概括由模型生成。</p>
          {detail.points.map((point, index) => <div key={index} className="space-y-2 rounded-lg bg-glass p-3"><p className="whitespace-pre-wrap break-words text-sm">{point.text}</p><div className="flex flex-wrap gap-2">{point.source_message_ids.map((id) => <Button key={id} size="sm" variant="ghost" onClick={() => inspectSource(id)}>查看出处 {id}</Button>)}</div></div>)}
        </>}
      </div>}
    </Modal>
    <Modal open={sourceOpen} title="摘要来源消息" size="lg" onClose={() => { sourceRequest.current += 1; setSourceOpen(false); setSource(undefined); }}>
      {sourceLoading && <p role="status">正在读取来源…</p>}{sourceError && <p role="alert" className="text-error">{sourceError}</p>}
      {source && <div className="space-y-3"><p className="break-all text-sm">消息 {source.message_id} · {source.card || source.nickname || source.sender_id}（{source.sender_id}）</p><p className="text-xs text-text-tertiary">{formatTime(source.message_time)}</p><p className="whitespace-pre-wrap break-words text-sm">{source.status === 'active' ? source.text || '无文本正文' : '来源正文已删除、撤回或过期'}</p>{source.truncated && <p className="text-warning">来源记录已截断</p>}</div>}
    </Modal>
    <Modal open={!!pending} title="删除群摘要" onClose={() => { if (!busy) setPending(undefined); }}>
      <div className="space-y-4"><p className="break-words">删除「{pending?.summary.title || '来源已失效的摘要'}」？仅删除派生摘要，原始消息和模型上下文保持原状。</p>{deleteError && <p role="alert" className="text-error">{deleteError}</p>}<div className="flex justify-end gap-2"><Button disabled={busy} onClick={() => setPending(undefined)}>取消</Button><Button variant="danger" disabled={busy} onClick={remove}>{busy ? '正在删除…' : '确认删除摘要'}</Button></div></div>
    </Modal>
  </Card>;
}
