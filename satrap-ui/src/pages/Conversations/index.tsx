import { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { Link, useSearchParams } from 'react-router-dom';
import { controlApi } from '@/api/control';
import type { ConversationDataItem, ConversationDataSnapshot, ConversationRecord } from '@/api/types';
import { PageHeader } from '@/components/common';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { Badge } from '@/components/ui/Badge';
import { useDirtyGuard, confirmDiscard } from '@/hooks/useDirtyGuard';
import { formatTime } from '@/utils/format';

const errorText = (error: unknown) => axios.isAxiosError<{ error?: string; detail?: string }>(error)
  ? error.response?.data?.error || error.response?.data?.detail || error.message
  : error instanceof Error ? error.message : String(error);
const readable = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? '';
const roles: Record<string, string> = { system: '系统提示词', developer: '开发者提示词', user: '用户', assistant: '助手', tool: '工具结果' };

export function Conversations() {
  const [search, setSearch] = useSearchParams();
  const platform = search.get('platform') || 'chat';
  const selected = search.get('conversation') || '';
  const [platforms, setPlatforms] = useState<string[]>(['chat', 'local']);
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<{ items: ConversationRecord[]; total: number }>({ items: [], total: 0 });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [recordMeta, setRecordMeta] = useState<{ platform: string; selected: string; record: ConversationRecord }>();
  useEffect(() => {
    if (!selected) return;
    let disposed = false;
    controlApi.listConversationRecords(platform, selected).then((data) => {
      if (!disposed) setRecordMeta({ platform, selected, record: data.items.find((item) => item.conversation_id === selected || item.context_ids.includes(selected)) || { conversation_id: selected, title: selected, context_ids: [selected], message_count: 0, history_count: 0 } });
    }).catch((error) => { if (!disposed) setError(errorText(error)); });
    return () => { disposed = true; };
  }, [selected, platform, refresh]);
  useEffect(() => {
    let disposed = false;
    controlApi.listConversationPlatforms().then((data) => { if (!disposed) setPlatforms(data.platforms); }).catch((error) => { if (!disposed) setError(errorText(error)); });
    return () => { disposed = true; };
  }, []);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError('');
    controlApi.listConversationRecords(platform, submitted, offset).then((data) => { if (!disposed) setResult(data); })
      .catch((error) => { if (!disposed) setError(errorText(error)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [platform, submitted, offset, refresh]);
  const record = recordMeta?.platform === platform && recordMeta.selected === selected ? recordMeta.record : result.items.find((item) => item.conversation_id === selected);
  return <div className="space-y-5">
    <PageHeader title="对话记录" description="查看和维护已保存的上下文与历史对话" actions={<Link className="text-sm text-accent" to="/conversations/instances">管理对话实例</Link>} />
    <Card className="space-y-3">
      <form className="grid grid-cols-2 gap-3 sm:flex sm:flex-wrap" onSubmit={(event) => { event.preventDefault(); setSubmitted(query); setOffset(0); }}>
        <select aria-label="记录平台" className="glass-input max-w-full" value={platform} onChange={(event) => { setSearch({ platform: event.target.value }); setOffset(0); }}>
          {[...new Set([platform, ...platforms])].map((item) => <option key={item}>{item}</option>)}
        </select>
        <input aria-label="搜索对话" className="glass-input min-w-0 sm:flex-1" placeholder="搜索对话 ID 或 Chat 首条消息" value={query} onChange={(event) => setQuery(event.target.value)} />
        <Button type="submit" disabled={loading}>搜索</Button>
        <Button type="button" onClick={() => setRefresh((value) => value + 1)} disabled={loading}>刷新列表</Button>
      </form>
      {error && <p role="alert" className="text-error">{error}</p>}
      <p className="text-xs text-text-tertiary">共 {result.total} 个对话。选择对话后可查看其主上下文、工作流上下文及独立展示历史。</p>
    </Card>
    <div className="grid items-start gap-4 xl:grid-cols-[20rem_minmax(0,1fr)]">
      <Card className="min-w-0 space-y-2">
        {loading && <p role="status" className="text-sm">正在读取列表…</p>}
        {!loading && !result.items.length && <p className="text-sm text-text-secondary">此平台暂无匹配的对话</p>}
        {result.items.map((item) => <button key={item.conversation_id} className={`w-full space-y-1 rounded-lg p-3 text-left transition-colors ${selected === item.conversation_id ? 'bg-accent/10' : 'bg-glass hover:bg-glass-hover'}`} onClick={() => setSearch({ platform, conversation: item.conversation_id })}>
          <p className="break-all font-medium">{item.title}</p><p className="break-all text-xs text-text-tertiary">{item.conversation_id}</p>
          <p className="text-xs text-text-secondary">{item.message_count} 条上下文消息 · {item.history_count} 轮展示历史</p>
        </button>)}
        <div className="flex justify-between gap-2 pt-2"><Button size="sm" disabled={offset === 0 || loading} onClick={() => setOffset((value) => Math.max(0, value - 40))}>上一页对话</Button><Button size="sm" disabled={offset + 40 >= result.total || loading} onClick={() => setOffset((value) => value + 40)}>下一页对话</Button></div>
      </Card>
      {selected ? record ? <ConversationDetail key={`${platform}:${selected}`} platform={platform} record={record} requestedContext={selected} onSaved={() => setRefresh((value) => value + 1)} /> : <Card><p role="status">正在读取对话详情…</p></Card> : <Card><p className="text-text-secondary">从列表选择一个对话</p></Card>}
    </div>
  </div>;
}

function ConversationDetail({ platform, record, requestedContext, onSaved }: { platform: string; record: ConversationRecord; requestedContext: string; onSaved: () => void }) {
  const [layer, setLayer] = useState<'context' | 'history'>('context');
  const [context, setContext] = useState(record.context_ids.includes(requestedContext) ? requestedContext : record.context_ids[0] || record.conversation_id);
  const [dirty, setDirty] = useState(false);
  useDirtyGuard(dirty);
  const switchLayer = (next: 'context' | 'history') => { if (next !== layer && (!dirty || confirmDiscard())) { setDirty(false); setLayer(next); } };
  return <div className="min-w-0 space-y-4">
    <Card className="space-y-3">
      <h2 className="break-all text-lg font-semibold">{record.title}</h2>
      <div className="flex flex-wrap gap-2"><Button variant={layer === 'context' ? 'primary' : 'ghost'} onClick={() => switchLayer('context')}>上下文</Button><Button variant={layer === 'history' ? 'primary' : 'ghost'} onClick={() => switchLayer('history')} disabled={platform !== 'chat'}>Chat 展示历史</Button>
        <Link className="self-center text-sm text-accent" to={`/conversations/versions?${new URLSearchParams({ platform, conversation: context })}`}>版本与恢复</Link>
      </div>
      {layer === 'context' && <label className="block text-sm">上下文范围<select aria-label="上下文范围" className="glass-input mt-1 w-full" value={context} onChange={(event) => { if (!dirty || confirmDiscard()) { setDirty(false); setContext(event.target.value); } }}>{[...new Set([context, ...record.context_ids])].map((item) => <option key={item}>{item}</option>)}</select></label>}
      <p className="text-sm text-text-secondary">{layer === 'context' ? '修改此处会影响机器人后续使用的上下文，不改写 Chat 展示记录。这里展示保存的完整消息；模型请求还可能应用截断、总结及临时提示。' : '修改此处只改变 Chat 页面展示的历史，不改写模型上下文。已有回复版本与工具明细分别保留。'}</p>
    </Card>
    <DataPanel key={`${layer}:${context}`} platform={platform} conversation={layer === 'context' ? context : record.conversation_id} layer={layer} onDirty={setDirty} onSaved={onSaved} />
  </div>;
}

function DataPanel({ platform, conversation, layer, onDirty, onSaved }: { platform: string; conversation: string; layer: 'context' | 'history'; onDirty: (dirty: boolean) => void; onSaved: () => void }) {
  const [snapshot, setSnapshot] = useState<ConversationDataSnapshot>();
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editor, setEditor] = useState<ConversationDataItem | null>(null);
  const [form, setForm] = useState({ content: '', mode: 'text', reasoning: '', user_input: '', answer: '', thinking: '' });
  const [initial, setInitial] = useState('');
  const [keepSystem, setKeepSystem] = useState(true);
  const mounted = useRef(true);
  const dirty = !!editor && JSON.stringify(form) !== initial;
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError('');
    controlApi.conversationData(platform, conversation, layer, { offset }).then((data) => { if (!disposed) setSnapshot(data); })
      .catch((error) => { if (!disposed) setError(errorText(error)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [platform, conversation, layer, offset, refresh]);
  const close = useCallback(() => { if (!busy && (!dirty || confirmDiscard())) setEditor(null); }, [busy, dirty]);
  const mutate = async (data: Record<string, unknown>) => {
    if (!snapshot) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await controlApi.conversationData(platform, conversation, layer, { ...data, expected_revision: snapshot.revision, offset });
      if (!mounted.current) return;
      setSnapshot(result); setEditor(null); setNotice('修改已保存，修改前的数据已备份'); onSaved();
    } catch (error) { if (mounted.current) setError(errorText(error)); }
    finally { if (mounted.current) setBusy(false); }
  };
  const openEditor = (item: ConversationDataItem) => {
    const values = { content: readable(item.content), mode: item.content === null ? 'null' : typeof item.content === 'string' ? 'text' : 'json', reasoning: item.reasoning_content || '', user_input: item.user_input || '', answer: item.answer || '', thinking: item.thinking || '' };
    setForm(values); setInitial(JSON.stringify(values)); setEditor(item); setError('');
  };
  const saveEditor = async () => {
    if (!editor) return;
    try {
      const data = layer === 'context' ? { content: form.mode === 'null' ? null : form.mode === 'json' ? JSON.parse(form.content) : form.content, reasoning_content: form.reasoning || null } : { user_input: form.user_input, answer: form.answer, thinking: form.thinking };
      await mutate({ action: 'edit', index: editor.index, ...data });
    } catch (error) { setError(`内容格式无效：${errorText(error)}`); }
  };
  return <Card className="min-w-0 space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="text-sm">共 {snapshot?.total ?? '…'} {layer === 'context' ? '条消息' : '轮历史'} · {snapshot?.source === 'memory' ? '活动实例最新上下文' : '已保存数据'}</p>
      <div className="flex flex-wrap items-center gap-3">{layer === 'context' && <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={keepSystem} onChange={(event) => setKeepSystem(event.target.checked)} disabled={busy} />清空时保留系统提示词</label>}
        <Button size="sm" disabled={busy || loading} onClick={() => { if (!dirty || confirmDiscard()) { setEditor(null); setRefresh((value) => value + 1); } }}>重新读取消息</Button>
        <Button size="sm" variant="danger" disabled={busy || !snapshot?.total} onClick={() => { if (window.confirm(layer === 'context' ? `确定清空此上下文吗？${keepSystem ? '系统提示词会保留。' : '系统提示词也会删除。'}修改前的数据会备份。` : '确定删除此对话的全部展示历史吗？模型上下文不会改变，删除前会备份。')) void mutate({ action: 'clear', keep_system: keepSystem }); }}>{layer === 'context' ? '清空上下文' : '删除全部展示历史'}</Button>
      </div>
    </div>
    {error && !editor && <p role="alert" className="text-error">{error}</p>}
    {notice && <p role="status" className="text-success">{notice}</p>}
    {loading && <p role="status">正在读取消息…</p>}
    {!loading && snapshot?.total === 0 && <p className="text-sm text-text-secondary">{layer === 'context' ? '此范围没有上下文消息；系统提示词也可能由 Agent 配置临时注入。' : '暂无独立展示历史'}</p>}
    {snapshot?.items.map((item) => <section key={item.index} className="min-w-0 space-y-2 rounded-lg bg-glass p-3">
      <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm">#{item.index + 1} <Badge variant="info">{layer === 'context' ? roles[item.role || ''] || item.role : '对话轮次'}</Badge>{item.created_at && <span className="ml-2 text-xs text-text-tertiary">{formatTime(item.created_at)}</span>}</p>
        <div className="flex gap-2"><Button size="sm" aria-label={`编辑第 ${item.index + 1} 项`} disabled={busy || loading} onClick={() => openEditor(item)}>编辑</Button><Button size="sm" variant="danger" aria-label={`删除第 ${item.index + 1} 项`} disabled={busy || loading} onClick={() => { if (window.confirm(layer === 'context' ? '确定删除此消息吗？工具调用或结果会连同对应的完整调用组删除，修改前会备份。' : '确定删除此历史轮次及其回复版本和工具明细吗？模型上下文不会改变，删除前会备份。')) void mutate({ action: 'delete', index: item.index }); }}>删除</Button></div>
      </div>
      <div className="max-h-80 overflow-y-auto whitespace-pre-wrap break-words text-sm">{layer === 'context' ? readable(item.content) || '（无文本内容）' : <><p className="mb-2 text-text-secondary">用户：{item.user_input}</p><p>助手：{item.answer}</p></>}</div>
      {layer === 'context' && item.reasoning_content && <details><summary className="cursor-pointer text-xs text-text-tertiary">思考内容</summary><pre className="mt-2 whitespace-pre-wrap break-all text-xs">{item.reasoning_content}</pre></details>}
      <details><summary className="cursor-pointer text-xs text-text-tertiary">原始数据与结构字段</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(item, null, 2)}</pre></details>
    </section>)}
    <div className="flex justify-between gap-2"><Button size="sm" disabled={!offset || busy || loading} onClick={() => setOffset((value) => Math.max(0, value - 50))}>上一页消息</Button><Button size="sm" disabled={!snapshot || offset + 50 >= snapshot.total || busy || loading} onClick={() => setOffset((value) => value + 50)}>下一页消息</Button></div>
    <details className="rounded-lg bg-glass p-3"><summary className="cursor-pointer text-sm font-medium">修改备份与恢复 ({snapshot?.backups.length || 0})</summary>
      <p className="my-2 text-xs text-text-tertiary">备份仍保存修改或删除前的内容。恢复只作用于当前数据层，并备份恢复前的状态。</p>
      {snapshot?.backups.map((backup) => <div key={backup.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-xs"><span>{formatTime(backup.created_at)} · {{ edit: '编辑前', delete: '删除前', clear: '清空前', restore: '恢复前', checkpoint: '恢复快照前' }[backup.reason] || backup.reason}</span><Button size="sm" disabled={busy} onClick={() => { if (window.confirm('确定恢复此修改备份吗？当前数据会先备份。')) void mutate({ action: 'restore', backup_id: backup.id }); }}>恢复备份</Button></div>)}
    </details>
    <Modal open={!!editor} onClose={close} title={layer === 'context' ? '编辑上下文消息' : '编辑展示历史'} size="lg">
      <div className="space-y-3">
        <p className="text-xs text-text-secondary">{layer === 'context' ? '保存会改变后续模型上下文；角色、工具调用 ID 等结构字段保持原样。' : '保存仅改变展示记录；当前回复版本同步显示修改，其他版本保留。'}</p>
        {error && <p role="alert" className="text-error">{error}</p>}
        {layer === 'context' ? <>
          <label className="block text-sm">内容格式<select aria-label="内容格式" className="glass-input mt-1 w-full" value={form.mode} disabled={busy} onChange={(event) => setForm({ ...form, mode: event.target.value })}><option value="text">文本</option><option value="json">多模态 JSON</option><option value="null">空值 null</option></select></label>
          {form.mode !== 'null' && <label className="block text-sm">消息内容<textarea aria-label="消息内容" className="glass-input mt-1 min-h-48 w-full" value={form.content} disabled={busy} onChange={(event) => setForm({ ...form, content: event.target.value })} /></label>}
          <label className="block text-sm">思考内容<textarea aria-label="思考内容" className="glass-input mt-1 w-full" value={form.reasoning} disabled={busy} onChange={(event) => setForm({ ...form, reasoning: event.target.value })} /></label>
        </> : <>{(['user_input', 'answer', 'thinking'] as const).map((key) => <label key={key} className="block text-sm">{{ user_input: '用户输入', answer: '助手回复', thinking: '思考内容' }[key]}<textarea aria-label={{ user_input: '用户输入', answer: '助手回复', thinking: '思考内容' }[key]} className="glass-input mt-1 min-h-28 w-full" value={form[key]} disabled={busy} onChange={(event) => setForm({ ...form, [key]: event.target.value })} /></label>)}</>}
        <div className="flex justify-end gap-2"><Button onClick={close} disabled={busy}>取消</Button><Button variant="primary" disabled={!dirty || busy} onClick={() => void saveEditor()}>{busy ? '保存中…' : '保存修改'}</Button></div>
      </div>
    </Modal>
  </Card>;
}
