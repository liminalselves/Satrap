import { useCallback, useEffect, useState } from 'react';
import { ragApi, ragErrorMessage, type KnowledgeBase, type RagContext, type RagResult } from '@/api/rag';
import type { EdictumPluginConfigField } from '@/api/types';
import { RagImportModal } from './RagImportModal';
import { PluginConfigFields } from './PluginConfigFields';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';

const schema: Record<string, EdictumPluginConfigField> = {
  embed: { type: 'embed', required: true, default: '', description: '后端 Embedding 配置' },
  chunk_size: { type: 'number', integer: true, minimum: 1, default: 800, description: '每块字符数' },
  chunk_overlap: { type: 'number', integer: true, minimum: 0, default: 120, description: '相邻块重叠字符数' },
  batch_size: { type: 'number', integer: true, minimum: 1, default: 32, description: '每批向量化片段数' },
  duplicate_policy: { type: 'select', default: 'skip', options: ['skip', 'replace'], description: '重复来源: skip 跳过, replace 替换' },
};
const defaults = Object.fromEntries(Object.entries(schema).map(([key, field]) => [key, field.default]));

export function RagManager({ context }: { context: RagContext }) {
  const [data, setData] = useState<RagResult | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [editing, setEditing] = useState<KnowledgeBase | 'new' | null>(null);
  const [name, setName] = useState('');
  const [scope, setScope] = useState('session');
  const [isDefault, setIsDefault] = useState(false);
  const [config, setConfig] = useState<Record<string, unknown>>(defaults);
  const [importTarget, setImportTarget] = useState<KnowledgeBase | null>(null);
  const [query, setQuery] = useState('');
  const [rerank, setRerank] = useState('');
  const [searchResult, setSearchResult] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const platformId = context.platformId, sessionId = context.sessionId, via = context.via;
  const selected = data?.knowledge_bases.find((item) => item.id === selectedId);

  const reload = useCallback(async () => {
    const result = await ragApi.list({ platformId, sessionId, via }, selectedId);
    setData(result);
  }, [platformId, sessionId, via, selectedId]);

  useEffect(() => { setSelectedId(''); setSearchResult(null); setData(null); }, [platformId, sessionId, via]);
  useEffect(() => {
    let cancelled = false;
    ragApi.list({ platformId, sessionId, via }, selectedId).then((result) => { if (!cancelled) { setData(result); setError(''); } })
      .catch((reason) => { if (!cancelled) setError(ragErrorMessage(reason)); });
    return () => { cancelled = true; };
  }, [platformId, sessionId, via, selectedId]);

  const run = async (label: string, payload: Record<string, unknown>) => {
    setBusy(label); setError(''); setNotice('');
    try {
      const result = await ragApi.action({ platformId, sessionId, via }, payload);
      if (result.ok === false) throw new Error(String(result.error || '操作未完成'));
      if (payload.action === 'search') setSearchResult(result);
      else if (payload.action === 'delete') { setSelectedId(''); setData(await ragApi.list({ platformId, sessionId, via })); }
      else if (payload.action === 'create' && result.knowledge_base) {
        const created = result.knowledge_base as KnowledgeBase;
        setSelectedId(created.id);
        setData(await ragApi.list({ platformId, sessionId, via }, created.id));
        setImportTarget(created);
      } else await reload();
      if (payload.action !== 'search') setNotice(`${label}完成${result.status === 'skipped' ? ', 已跳过重复来源' : ''}`);
      setEditing(null);
    } catch (reason) { setError(ragErrorMessage(reason)); }
    finally { setBusy(''); }
  };

  const edit = (item: KnowledgeBase | 'new') => {
    setEditing(item);
    setName(item === 'new' ? '' : item.name);
    setScope(item === 'new' ? sessionId ? 'session' : 'global' : item.scope);
    setConfig(item === 'new' ? { ...defaults } : { ...item.config });
    setIsDefault(item !== 'new' && !!item.is_default);
    setError('');
  };

  return <div className="space-y-4">
    <div className="flex items-center justify-between gap-3">
      <div><h2 className="text-lg font-semibold">RAG 知识库</h2><p className="text-xs text-text-tertiary">{sessionId ? `全局库与当前会话库 · ${sessionId}` : `全局库与 ${platformId} 平台会话库`}</p></div>
      <div className="flex gap-2"><Button variant="ghost" disabled={!!busy} onClick={() => reload().catch((reason) => setError(ragErrorMessage(reason)))}>刷新</Button><Button disabled={!!busy} onClick={() => edit('new')}>新建知识库</Button></div>
    </div>
    {error && <p role="alert" className="text-sm text-error">{error}</p>}
    {(notice || busy) && <p role="status" className="text-sm text-text-secondary">{busy ? `${busy}处理中, 完成后会更新结果` : notice}</p>}
    <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">名称</th><th>层级</th><th>文档 / 块</th><th>状态</th><th>操作</th></tr></thead><tbody>
      {data?.knowledge_bases.map((item) => <tr key={item.id} className="border-t border-glass-border">
        <td className="p-2"><button className="text-accent" onClick={() => setSelectedId(item.id)}>{item.name}{item.is_default ? ' (默认)' : ''}</button></td>
        <td>{item.scope === 'global' ? '全局' : `会话 ${item.session_id}`}</td><td>{item.document_count} / {item.chunk_count}</td>
        <td title={item.last_error}>{item.status === 'error' ? '上次操作失败' : '可用'}</td>
        <td><Button size="sm" variant="ghost" disabled={!!busy || !data?.upload} onClick={() => { setSelectedId(item.id); setImportTarget(item); }}>上传文档</Button><Button size="sm" variant="ghost" disabled={!!busy} onClick={() => edit(item)}>配置</Button><Button size="sm" variant="ghost" disabled={!!busy} onClick={() => { if (window.confirm(`删除知识库“${item.name}”？已有引用将失效。`)) void run('删除', { action: 'delete', kb_id: item.id }); }}>删除</Button></td>
      </tr>)}
    </tbody></table>{data && !data.knowledge_bases.length && <p className="p-6 text-center text-sm text-text-tertiary">暂无知识库，创建后可导入文档</p>}</div>
    {selected && <div className="space-y-4 rounded-xl border border-glass-border p-4">
      <h3 className="font-medium">{selected.name}</h3>
      {selected.last_error && <p className="text-xs text-error">{selected.last_error}</p>}
      <Button disabled={!!busy || !data?.upload} onClick={() => setImportTarget(selected)}>导入文档</Button>
      <ul className="space-y-2">{data?.documents.map((document) => <li key={document.source_id} className="flex items-center justify-between text-sm"><span>{document.source} · {document.chunk_count} 块</span><Button size="sm" variant="ghost" disabled={!!busy} onClick={() => { if (window.confirm(`删除文档“${document.source}”？`)) void run('删除文档', { action: 'delete_document', kb_id: selected.id, source_id: document.source_id }); }}>删除</Button></li>)}</ul>
      <div className="space-y-2 border-t border-glass-border pt-4">
        <input aria-label="检索问题" className="glass-input w-full" placeholder="输入问题测试检索" value={query} onChange={(event) => setQuery(event.target.value)} />
        <select aria-label="重排配置" className="glass-input w-full" value={rerank} onChange={(event) => setRerank(event.target.value)}><option value="">不使用重排</option>{data?.model_options.rerank?.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
        <Button disabled={!!busy || !query.trim()} onClick={() => run('检索', { action: 'search', query, config: { db_scope: selected.scope, global_db_ids: selected.scope === 'global' ? [selected.id] : [], session_db_ids: selected.scope === 'session' ? [selected.id] : [], rerank } })}>测试检索</Button>
        {searchResult && <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded bg-black/10 p-3 text-xs">{JSON.stringify(searchResult, null, 2)}</pre>}
      </div>
    </div>}
    {importTarget && data?.upload && <RagImportModal key={importTarget.id} context={context} target={importTarget} capabilities={data.upload}
      onClose={() => setImportTarget(null)}
      onComplete={(result) => {
        setNotice(typeof result.summary === 'string' ? result.summary : result.status === 'skipped' ? '已跳过重复来源：' + result.source : '已导入 ' + result.source + ' · ' + result.chunks + ' 块');
        void reload().catch((reason) => setError(ragErrorMessage(reason)));
      }} />}
    <Modal open={editing !== null} onClose={() => { if (!busy) setEditing(null); }} title={editing === 'new' ? '新建知识库' : '知识库配置'} size="lg">
      <div className="space-y-4">
        {error && <p role="alert" className="text-sm text-error">{error}</p>}
        <input aria-label="知识库名称" className="glass-input w-full" placeholder="知识库名称" value={name} onChange={(event) => setName(event.target.value)} />
        {editing === 'new' && <select aria-label="知识库层级" className="glass-input w-full" value={scope} onChange={(event) => setScope(event.target.value)}><option value="global">全局库</option>{sessionId && <option value="session">当前会话库</option>}</select>}
        <PluginConfigFields schema={schema} values={config} modelOptions={data?.model_options} disabled={!!busy} onChange={(key, value) => setConfig((current) => ({ ...current, [key]: value }))} />
        {editing && editing !== 'new' && scope === 'session' && <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={!!busy} checked={isDefault} onChange={(event) => setIsDefault(event.target.checked)} />作为此会话的默认知识库</label>}
        {editing && editing !== 'new' && <p className="text-xs text-text-secondary">模型或分块参数变化需重建。重建成功后切换索引，失败时保留原配置和数据。</p>}
        <div className="flex justify-end gap-2">
          {editing && editing !== 'new' && <Button variant="subtle" disabled={!!busy} onClick={() => { if (window.confirm('使用这些参数重新向量化全部文档？')) void run('重建', { action: 'rebuild', kb_id: editing.id, config, expected_revision: editing.revision }); }}>重建索引</Button>}
          <Button disabled={!!busy || !name.trim() || !config.embed} onClick={() => run('保存', editing === 'new' ? { action: 'create', name, scope, config } : { action: 'update', kb_id: editing?.id, name, config, expected_revision: editing?.revision, is_default: isDefault })}>保存</Button>
        </div>
      </div>
    </Modal>
  </div>;
}
