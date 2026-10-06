import { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { memoryApi, type MemoryKind, type MemoryPage, type MemoryProposal, type ScopedMemory } from '@/api/memory';
import { controlApi } from '@/api/control';
import type { PlatformArchiveMessage, PlatformArchiveRecord } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Modal } from '@/components/ui/Modal';
import { confirmDiscard } from '@/hooks/useDirtyGuard';

const errorText = (error: unknown) => axios.isAxiosError<{ error?: string }>(error)
  ? error.response?.data?.error || error.message : error instanceof Error ? error.message : String(error);
const proposalStates: Record<string, string> = { pending: '待审批', approved: '已批准', rejected: '已拒绝', expired: '已过期', conflicted: '内容已变化，审批冲突' };
type Draft = { memory?: ScopedMemory; kind: MemoryKind; owner: string; key: string; title: string; content: string; operationKey: string };

export function GroupMemories({ record, refresh, onDirty }: { record: PlatformArchiveRecord; refresh: number; onDirty: (dirty: boolean) => void }) {
  const [tab, setTab] = useState<MemoryKind | 'proposals'>('group_rule');
  const [keyword, setKeyword] = useState('');
  const [query, setQuery] = useState('');
  const [member, setMember] = useState('');
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors[cursors.length - 1];
  const [page, setPage] = useState<MemoryPage>();
  const [proposals, setProposals] = useState<MemoryProposal[]>([]);
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [draft, setDraft] = useState<Draft>();
  const [draftError, setDraftError] = useState('');
  const [pendingDelete, setPendingDelete] = useState<{ memory: ScopedMemory; key: string }>();
  const [pendingDecision, setPendingDecision] = useState<MemoryProposal>();
  const [source, setSource] = useState<PlatformArchiveMessage>();
  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourceError, setSourceError] = useState('');
  const sourceSequence = useRef(0);

  useEffect(() => { onDirty(!!draft); return () => onDirty(false); }, [draft, onDirty]);
  useEffect(() => {
    let live = true;
    setLoading(true); setError('');
    const task = tab === 'proposals' ? memoryApi.proposals(record) : memoryApi.list(record, { kind: tab, keyword, user_id: tab === 'member_preference' ? member : undefined, cursor });
    task.then((result) => {
      if (!live) return;
      if (tab === 'proposals') setProposals(result.items as MemoryProposal[]); else setPage(result as MemoryPage);
    }).catch((reason) => { if (live) setError(errorText(reason)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [record, tab, keyword, member, cursor, refresh, version]);

  const beginEdit = (memory?: ScopedMemory) => {
    setDraftError('');
    setDraft({ memory, kind: memory?.kind || (tab === 'member_preference' ? tab : 'group_rule'), owner: memory?.owner_user_id || '',
      key: memory?.key || '', title: memory?.title || '', content: memory?.content || '', operationKey: crypto.randomUUID() });
  };
  const closeDraft = () => { if (!busy && confirmDiscard()) { setDraft(undefined); setDraftError(''); } };
  const updateDraft = (field: keyof Pick<Draft, 'kind' | 'owner' | 'key' | 'title' | 'content'>, value: string) => {
    setDraft((old) => old ? { ...old, [field]: value, operationKey: crypto.randomUUID() } : old);
  };
  const save = async () => {
    if (!draft || busyRef.current) return;
    busyRef.current = true; setBusy(true); setDraftError('');
    try {
      const values = { title: draft.title.trim(), content: draft.content.trim(), idempotency_key: draft.operationKey };
      const result = draft.memory
        ? await memoryApi.update(record, draft.memory.memory_id, { ...values, expected_revision: draft.memory.revision })
        : await memoryApi.create(record, { ...values, kind: draft.kind, key: draft.key.trim(), owner_user_id: draft.kind === 'member_preference' ? draft.owner.trim() : '' });
      if (result.status === 'already_exists') { setDraftError('相同用途的记忆已存在，请关闭草稿后查询并编辑现有记录'); return; }
      setDraft(undefined); setNotice('记忆已保存，下一轮读取生效；聊天原文和已有模型上下文未修改'); setCursors([]); setVersion((value) => value + 1);
    } catch (reason) { setDraftError(errorText(reason)); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const remove = async () => {
    if (!pendingDelete || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    try { await memoryApi.remove(record, pendingDelete.memory, pendingDelete.key); setPendingDelete(undefined); setNotice('记忆已删除，聊天原文保持不变'); setCursors([]); setVersion((value) => value + 1); }
    catch (reason) { setError(errorText(reason)); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const decide = async (proposal: MemoryProposal, approve: boolean) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    try {
      const result = await memoryApi.decide(record, proposal, approve);
      setPendingDecision(undefined); setNotice(proposalStates[result.status] || result.status); setVersion((value) => value + 1);
    } catch (reason) { setError(errorText(reason)); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const inspectSource = async (id: string) => {
    const sequence = ++sourceSequence.current;
    setSourceOpen(true); setSource(undefined); setSourceError('');
    try {
      const result = await controlApi.platformArchiveMessage({ platform_id: record.platform_id, self_id: record.self_id, conversation_kind: record.conversation_kind, chat_id: record.chat_id }, id);
      if (sequence === sourceSequence.current) setSource(result.item);
    } catch (reason) { if (sequence === sourceSequence.current) setSourceError(errorText(reason)); }
  };

  return <Card className="min-w-0 space-y-4">
    <h2 className="break-all text-lg font-medium">{record.label || record.chat_id} · 长期记忆</h2>
    <p className="text-sm text-text-secondary">群记忆对本群生效，成员偏好按成员 ID 分开保存。修改在下一轮读取生效；清空模型上下文不会删除这里的记录。人工修改直接生效，模型提交的群记忆需要审批。</p>
    <div className="flex flex-wrap gap-2">{([['group_rule', '群记忆'], ['member_preference', '成员偏好'], ['proposals', '待审批与处理记录']] as const).map(([kind, label]) => <Button key={kind} variant={tab === kind ? 'primary' : 'ghost'} onClick={() => { setTab(kind); setCursors([]); }}>{label}</Button>)}</div>
    {tab !== 'proposals' && <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); setKeyword(query); setCursors([]); setVersion((value) => value + 1); }}>
      <label className="min-w-0 flex-1 text-sm">标题或正文<input className="glass-input mt-1 w-full" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      {tab === 'member_preference' && <label className="min-w-0 text-sm">成员 ID<input className="glass-input mt-1 w-full" value={member} onChange={(event) => { setMember(event.target.value); setCursors([]); }} /></label>}
      <Button type="submit" disabled={loading}>查找记忆</Button><Button type="button" disabled={busy} onClick={() => beginEdit()}>新增记忆</Button>
    </form>}
    <Button size="sm" disabled={loading} onClick={() => { setCursors([]); setVersion((value) => value + 1); }}>刷新</Button>
    {loading && <p role="status">正在读取长期记忆…</p>}{error && <p role="alert" className="text-error">{error}</p>}{notice && <p role="status" className="text-success">{notice}</p>}
    {tab !== 'proposals' && !loading && !page?.items.length && <p className="text-text-secondary">暂无匹配的长期记忆</p>}
    {tab !== 'proposals' && page?.items.map((memory) => <div key={memory.memory_id} className="space-y-2 rounded-lg bg-glass p-3">
      <p className="break-words font-medium">{memory.title}</p><p className="whitespace-pre-wrap break-words text-sm">{memory.content}</p>
      <p className="break-all text-xs text-text-tertiary">{memory.kind === 'member_preference' ? `成员 ${memory.owner_user_id}` : '本群记忆'} · 用途 {memory.key} · 修订 {memory.revision}</p>
      <p className="text-xs text-text-secondary">{memory.source_status === 'operator' ? '人工维护，没有平台消息出处' : memory.source_status === 'unavailable' ? '来源已不可用，主动保存的记忆仍保留' : '附有效来源消息'}</p>
      <div className="flex flex-wrap gap-2"><Button size="sm" disabled={busy} onClick={() => beginEdit(memory)}>编辑记忆</Button><Button size="sm" variant="danger" disabled={busy} onClick={() => setPendingDelete({ memory, key: crypto.randomUUID() })}>删除记忆</Button>{memory.source_message_ids.map((id) => <Button key={id} size="sm" variant="ghost" onClick={() => inspectSource(id)}>查看出处 {id}</Button>)}</div>
    </div>)}
    {tab !== 'proposals' && <div className="flex justify-between gap-2"><Button disabled={!cursors.length || loading} onClick={() => setCursors((values) => values.slice(0, -1))}>上一页记忆</Button><Button disabled={!page?.next_cursor || loading} onClick={() => { if (page?.next_cursor) setCursors((values) => [...values, page.next_cursor!]); }}>下一页记忆</Button></div>}
    {tab === 'proposals' && !loading && !proposals.length && <p className="text-text-secondary">暂无记忆提案</p>}
    {tab === 'proposals' && proposals.map((proposal) => <div key={proposal.proposal_id} className="space-y-2 rounded-lg bg-glass p-3"><p className="break-words font-medium">{proposal.proposed.title || proposal.proposal_id} · {proposal.operation === 'create' ? '新增' : proposal.operation === 'update' ? '修改' : '删除'} · {proposalStates[proposal.state] || proposal.state}</p><p className="whitespace-pre-wrap break-words text-sm">{proposal.proposed.content}</p><p className="break-all text-xs text-text-secondary">发起者 {proposal.actor_id} · 基准修订 {proposal.base_revision}</p><div className="flex flex-wrap gap-2">{proposal.proposed.source_message_ids?.map((id) => <Button key={id} size="sm" variant="ghost" onClick={() => inspectSource(id)}>查看提案出处 {id}</Button>)}</div>{proposal.state === 'pending' && <div className="flex gap-2"><Button disabled={busy} onClick={() => setPendingDecision(proposal)}>审阅并批准</Button><Button disabled={busy} variant="danger" onClick={() => decide(proposal, false)}>拒绝</Button></div>}</div>)}
    <Modal open={!!draft} title={draft?.memory ? '编辑长期记忆' : '新增长期记忆'} onClose={closeDraft}>
      {draft && <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <p className="text-sm text-text-secondary">人工保存直接生效，标记为人工维护；不伪造群消息来源。</p>
        {!draft.memory && <><label className="block text-sm">类型<select className="glass-input mt-1 w-full" value={draft.kind} onChange={(event) => updateDraft('kind', event.target.value)}><option value="group_rule">本群记忆</option><option value="member_preference">成员在本群的偏好</option></select></label>{draft.kind === 'member_preference' && <label className="block text-sm">成员 ID<input required maxLength={256} className="glass-input mt-1 w-full" value={draft.owner} onChange={(event) => updateDraft('owner', event.target.value)} /></label>}<label className="block text-sm">用途键<input required maxLength={64} placeholder="例如 preferred_name" className="glass-input mt-1 w-full" value={draft.key} onChange={(event) => updateDraft('key', event.target.value)} /></label></>}
        <label className="block text-sm">标题<input required maxLength={120} className="glass-input mt-1 w-full" value={draft.title} onChange={(event) => updateDraft('title', event.target.value)} /></label>
        <label className="block text-sm">内容<textarea required maxLength={2000} rows={5} className="glass-input mt-1 w-full" value={draft.content} onChange={(event) => updateDraft('content', event.target.value)} /></label>
        {draftError && <p role="alert" className="text-error">{draftError}</p>}<div className="flex justify-end gap-2"><Button type="button" disabled={busy} onClick={closeDraft}>取消</Button><Button type="submit" variant="primary" disabled={busy || !draft.title.trim() || !draft.content.trim()}>{busy ? '正在保存…' : '保存记忆'}</Button></div>
      </form>}
    </Modal>
    <Modal open={!!pendingDelete} title="删除长期记忆" onClose={() => { if (!busy) setPendingDelete(undefined); }}><div className="space-y-4"><p className="break-words">删除「{pendingDelete?.memory.title}」？下一轮将不再读取该记忆，聊天原文与已有模型上下文不会因此删除。</p>{error && <p role="alert" className="text-error">{error}</p>}<div className="flex justify-end gap-2"><Button disabled={busy} onClick={() => setPendingDelete(undefined)}>取消</Button><Button disabled={busy} variant="danger" onClick={remove}>确认删除记忆</Button></div></div></Modal>
    <Modal open={!!pendingDecision} title="批准群记忆提案" onClose={() => { if (!busy) setPendingDecision(undefined); }}><div className="space-y-4"><p className="whitespace-pre-wrap break-words">{pendingDecision?.proposed.content}</p><p className="text-sm text-text-secondary">批准后应用此提案，下一轮读取生效；来源失效或版本冲突时不会覆盖当前记忆。</p>{error && <p role="alert" className="text-error">{error}</p>}<div className="flex justify-end gap-2"><Button disabled={busy} onClick={() => setPendingDecision(undefined)}>取消</Button><Button disabled={busy} variant="primary" onClick={() => { if (pendingDecision) void decide(pendingDecision, true); }}>确认批准</Button></div></div></Modal>
    <Modal open={sourceOpen} title="记忆来源消息" onClose={() => { sourceSequence.current += 1; setSourceOpen(false); }}><div className="space-y-3">{sourceError && <p role="alert" className="text-error">{sourceError}</p>}{!source && !sourceError && <p role="status">正在读取来源…</p>}{source && <><p className="break-all text-sm">{source.card || source.nickname || source.sender_id}（{source.sender_id}） · {source.message_id}</p><p className="whitespace-pre-wrap break-words">{source.status === 'active' ? source.text || '无文本正文' : '来源已删除、撤回或过期，正文不可用'}</p></>}</div></Modal>
  </Card>;
}
