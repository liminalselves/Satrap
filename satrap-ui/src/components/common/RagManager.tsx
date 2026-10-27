import { useCallback, useEffect, useMemo, useState } from 'react';
import { BookOpen, FileText, Plus, RefreshCw, RotateCw, Search, Settings2, Star, Trash2, Upload, Wand2 } from 'lucide-react';
import { cn } from '@/utils/cn';
import { formatRelativeTime } from '@/utils/format';
import { ApiError } from '@/api/client';
import { ragApi, ragErrorMessage, type KnowledgeBase, type RagContext, type RagResult } from '@/api/rag';
import { ragJobsApi, type RagJob, type RagScope } from '@/api/contracts';
import { useConfirm } from '@/hooks/useConfirm';
import { Badge } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { Field, Input } from '@/components/kit/Field';
import { Select } from '@/components/kit/Select';
import { toast } from '@/components/kit/Toast';
import { ConfigModal } from '@/components/rag/ConfigModal';
import { ImportModal } from '@/components/rag/ImportModal';
import { isJobPayload, jobsUnavailable, newOperationId, readDocuments, readLatestJob, waitForJob } from '@/components/rag/jobs';
import {
  emptyCapabilities, rerankOptions, toDoc, toKb, toLegacyDoc,
  type Doc, type ImportSummary, type Kb, type KBConfig, type KBDraft,
} from '@/components/rag/types';

export function RagManager({ context }: { context: RagContext }) {
  const confirm = useConfirm(`${context.platformId}:${context.sessionId || ''}:${context.via || ''}`);
  const [data, setData] = useState<RagResult | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [configTarget, setConfigTarget] = useState<Kb | 'new' | null>(null);
  const [importTarget, setImportTarget] = useState<Kb | null>(null);
  const [catalog, setCatalog] = useState<Doc[] | null>(null);
  const [job, setJob] = useState<RagJob | null>(null);
  const [query, setQuery] = useState('');
  const [rerank, setRerank] = useState('');
  const [searchResult, setSearchResult] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);

  const platformId = context.platformId;
  const sessionId = context.sessionId;
  const via = context.via;

  const reload = useCallback(async () => {
    const result = await ragApi.list(context, selectedId);
    setData(result);
  }, [context, selectedId]);

  useEffect(() => {
    let cancelled = false;
    setCatalog(null);
    ragApi.list(context, selectedId).then((result) => {
      if (cancelled) return;
      setData(result);
      setError('');
      if (!selectedId) setSelectedId(result.knowledge_bases.find((item) => item.is_default)?.id ?? result.knowledge_bases[0]?.id ?? '');
    }).catch((reason) => { if (!cancelled) setError(ragErrorMessage(reason)); });
    return () => { cancelled = true; };
  }, [context, selectedId, refreshKey]);

  useEffect(() => {
    let cancelled = false;
    setJob(null);
    if (!selectedId) return;
    const raw = data?.knowledge_bases.find((item) => item.id === selectedId);
    const scope: RagScope = { platform_id: platformId, session_id: raw?.scope === 'session' ? raw.session_id : sessionId || '', kb_id: selectedId };
    void readLatestJob(scope, via).then((found) => { if (!cancelled) setJob(found); });
    void readDocuments(scope, via).then((items) => {
      if (!cancelled) setCatalog(items ? items.map(toDoc) : null);
    });
    return () => { cancelled = true; };
  }, [data, selectedId, platformId, sessionId, via, refreshKey]);

  const kbs = useMemo(() => (data?.knowledge_bases ?? []).map((item) => toKb(item, item.id === selectedId ? job ?? undefined : undefined)), [data, selectedId, job]);
  const selected = kbs.find((item) => item.id === selectedId);
  const docs = catalog ?? (data?.documents ?? []).map(toLegacyDoc);
  const capabilities = data?.upload ?? emptyCapabilities;
  const modelOptions = data?.model_options;

  const refresh = async () => {
    setBusy('刷新');
    try {
      await reload();
      setRefreshKey((key) => key + 1);
      setNotice('知识库列表已刷新');
    } catch (reason) {
      setError(ragErrorMessage(reason));
    } finally {
      setBusy('');
    }
  };

  const saveConfig = async (draft: KBDraft, expectedRevision: number): Promise<string | null> => {
    if (configTarget === 'new') {
      if (draft.scope === 'session' && !sessionId) return '当前上下文没有具体会话，无法创建会话库';
      try {
        const result = await ragApi.action(context, { action: 'create', name: draft.name, scope: draft.scope, config: draft.config });
        if (result.ok === false) throw new Error(String(result.error || '创建未完成'));
        const created = result.knowledge_base as KnowledgeBase | undefined;
        const createdKb = created ? toKb(created) : null;
        if (createdKb) { setSelectedId(createdKb.id); setImportTarget(createdKb); }
        setNotice('知识库已创建, 现在可以导入文档');
        setRefreshKey((key) => key + 1);
        toast.success('知识库已创建', draft.name);
        return null;
      } catch (reason) {
        return ragErrorMessage(reason);
      }
    }
    if (!configTarget) return '编辑目标已失效，请重新打开配置';
    const current = kbs.find((item) => item.id === configTarget.id);
    if (!current) return '知识库不存在，可能已被删除';
    if (current.revision !== expectedRevision) return '知识库配置已被其他操作修改，请关闭后重新打开配置';
    if (draft.config.embed !== current.config.embed) return 'Embedding 模型变更需要显式重建知识库';
    if (draft.config.chunk_size !== current.config.chunk_size || draft.config.chunk_overlap !== current.config.chunk_overlap) return '分块参数变更需要显式重建知识库';
    try {
      const result = await ragApi.action(context, {
        action: 'update', kb_id: current.id, name: draft.name, config: draft.config,
        expected_revision: expectedRevision, is_default: draft.isDefault,
      });
      if (result.ok === false) throw new Error(String(result.error || '保存未完成'));
      setNotice(`已保存 ${draft.name} · v${expectedRevision + 1}`);
      setRefreshKey((key) => key + 1);
      toast.success('知识库配置已保存', `${draft.name} · v${expectedRevision + 1}`);
      return null;
    } catch (reason) {
      return ragErrorMessage(reason);
    }
  };

  const rebuild = async (kb: Kb, config?: KBConfig) => {
    if (kb.status === 'indexing') {
      toast.info('索引正在构建中', '请等待当前任务完成后再重建');
      return;
    }
    if (!(await confirm(`使用这些参数重新向量化“${kb.name}”的全部文档？重建期间旧索引仍可检索。`))) return;
    setBusy('重建');
    const scopeForJob: RagScope = { platform_id: platformId, session_id: kb.scope === 'session' ? kb.sessionId : sessionId || '', kb_id: kb.id };
    const operationId = newOperationId();
    try {
      let nextJob: RagJob | null = null;
      try {
        const response = await ragJobsApi.submit(scopeForJob, {
          operation_id: operationId, expected_revision: kb.revision, action: 'rebuild', config: config ?? kb.config,
        }, via);
        if (isJobPayload(response)) nextJob = response.job;
      } catch (reason) {
        if (!(jobsUnavailable(reason) || (reason instanceof ApiError && reason.status === 404))) throw reason;
      }
      if (nextJob) {
        setJob(nextJob);
        const finished = await waitForJob(scopeForJob, nextJob, via, setJob);
        if (finished.status !== 'succeeded') throw new Error(finished.error?.message || '重建未完成');
      } else {
        const result = await ragApi.action(context, { action: 'rebuild', kb_id: kb.id, config: config ?? kb.config, expected_revision: kb.revision });
        if (result.ok === false) throw new Error(String(result.error || '重建未完成'));
      }
      setNotice('重建完成, 已切换到新索引');
      setSearchResult(null);
      setRefreshKey((key) => key + 1);
      toast.success('索引重建完成', kb.name);
    } catch (reason) {
      toast.error('重建索引失败', ragErrorMessage(reason));
    } finally {
      setBusy('');
    }
  };

  const removeKb = async (kb: Kb) => {
    if (!(await confirm(`删除知识库“${kb.name}”？已有引用将失效。`))) return;
    setBusy('删除');
    try {
      const result = await ragApi.action(context, { action: 'delete', kb_id: kb.id });
      if (result.ok === false) throw new Error(String(result.error || '删除未完成'));
      if (selectedId === kb.id) setSelectedId('');
      setNotice(`知识库「${kb.name}」已删除`);
      setRefreshKey((key) => key + 1);
      toast.success('知识库已删除', kb.name);
    } catch (reason) {
      toast.error('删除知识库失败', ragErrorMessage(reason));
    } finally {
      setBusy('');
    }
  };

  const removeDoc = async (kb: Kb, doc: Doc) => {
    if (!(await confirm(`删除文档“${doc.source}”？`))) return;
    setBusy('删除文档');
    try {
      const result = await ragApi.action(context, { action: 'delete_document', kb_id: kb.id, source_id: doc.sourceId });
      if (result.ok === false) throw new Error(String(result.error || '删除未完成'));
      setNotice(`已删除文档 ${doc.source}`);
      setRefreshKey((key) => key + 1);
      toast.success('文档已删除', doc.source);
    } catch (reason) {
      toast.error('删除文档失败', ragErrorMessage(reason));
    } finally {
      setBusy('');
    }
  };

  const runSearch = async () => {
    if (!selected || !query.trim()) return;
    setBusy('检索');
    setError('');
    try {
      const response = await ragApi.action(context, {
        action: 'search', query: query.trim(),
        config: {
          db_scope: selected.scope,
          global_db_ids: selected.scope === 'global' ? [selected.id] : [],
          session_db_ids: selected.scope === 'session' ? [selected.id] : [],
          rerank,
        },
      });
      if (response.ok === false) throw new Error(String(response.error || '检索失败'));
      setSearchResult(response);
    } catch (reason) {
      setError(ragErrorMessage(reason));
      setSearchResult(null);
    } finally {
      setBusy('');
    }
  };

  const handleImport = (summary: ImportSummary) => {
    setNotice(`本次导入：成功 ${summary.indexed}，跳过 ${summary.skipped}，失败 ${summary.failed}`);
    setRefreshKey((key) => key + 1);
    if (summary.failed > 0) toast.warning('导入完成（部分失败）', `成功 ${summary.indexed} · 跳过 ${summary.skipped} · 失败 ${summary.failed}`);
    else if (summary.indexed === 0) toast.info('没有新增文档', `${summary.skipped} 个重复来源已跳过`);
    else toast.success('导入完成', `成功 ${summary.indexed} · 跳过 ${summary.skipped} · 新增 ${summary.chunks} 个片段`);
  };

  return <div className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="min-w-0">
        <h2 className="text-lg font-semibold text-fg">RAG 知识库</h2>
        <p className="text-xs text-fg-3">{sessionId ? `全局库与当前会话库 · ${sessionId}` : `全局库与 ${platformId} 平台会话库`}</p>
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="ghost" icon={<RefreshCw className={cn('h-3.5 w-3.5', busy === '刷新' && 'animate-spin-slow')} />} disabled={Boolean(busy)} onClick={() => void refresh()}>刷新</Button>
        <Button size="sm" variant="tinted" tint="green" icon={<Plus className="h-3.5 w-3.5" />} disabled={Boolean(busy)} onClick={() => setConfigTarget('new')}>新建知识库</Button>
      </div>
    </div>

    {error && <p role="alert" className="flex items-start gap-2 rounded-xl border border-[rgb(var(--rgb-red)/0.25)] bg-[rgb(var(--rgb-red)/0.08)] px-3 py-2 text-xs text-red"><span className="mt-0.5">●</span>{error}</p>}
    {(notice || busy) && <p role="status" className="rounded-xl border border-line bg-glass px-3 py-2 text-xs text-fg-3">{busy ? `${busy}处理中, 完成后会更新结果` : notice}</p>}

    <div className="scroll overflow-x-auto rounded-xl border border-line">
      <table className="w-full min-w-[560px] text-left text-[12.5px]">
        <thead>
          <tr className="border-b border-line text-[10.5px] text-fg-4">
            <th className="px-3 py-2 font-medium">名称</th>
            <th className="px-3 py-2 font-medium">层级</th>
            <th className="px-3 py-2 font-medium">文档 / 块</th>
            <th className="px-3 py-2 font-medium">状态</th>
            <th className="px-3 py-2 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {kbs.map((kb) => (
            <tr key={kb.id} className={cn('border-b border-line transition-colors last:border-0 hover:bg-glass-hover', selectedId === kb.id && 'bg-[rgb(var(--rgb-green)/0.07)]')}>
              <td className="px-3 py-2">
                <button type="button" onClick={() => { setSelectedId(kb.id); setSearchResult(null); }} className="flex items-center gap-1.5 text-left font-medium text-fg transition hover:text-green">
                  {kb.name}
                  {kb.isDefault && <Star className="h-3 w-3 fill-current text-green" aria-label="会话默认知识库" />}
                </button>
              </td>
              <td className="px-3 py-2"><Badge tone={kb.scope === 'global' ? 'neutral' : 'green'}>{kb.scope === 'global' ? '全局' : '会话'}</Badge></td>
              <td className="num px-3 py-2 font-mono text-fg-2">{kb.documentCount} / {kb.chunkCount}</td>
              <td className="px-3 py-2">
                <span title={kb.lastError}>
                  <Badge tone={kb.status === 'error' ? 'red' : kb.status === 'indexing' ? 'orange' : 'green'} dot live={kb.status === 'indexing'}>
                    {kb.status === 'error' ? '上次操作失败' : kb.status === 'indexing' ? `索引中${kb.progress === null ? '' : ` ${Math.round(kb.progress)}%`}` : '可用'}
                  </Badge>
                </span>
              </td>
              <td className="px-3 py-2">
                <div className="flex items-center justify-end gap-0.5">
                  <IconButton label="上传文档" size="sm" disabled={Boolean(busy)} onClick={() => { setSelectedId(kb.id); setImportTarget(kb); }}><Upload className="h-3.5 w-3.5" /></IconButton>
                  <IconButton label="配置知识库" size="sm" disabled={Boolean(busy)} onClick={() => { setSelectedId(kb.id); setConfigTarget(kb); }}><Wand2 className="h-3.5 w-3.5" /></IconButton>
                  <IconButton label="删除知识库" size="sm" disabled={Boolean(busy)} onClick={() => void removeKb(kb)} className="text-red hover:text-red"><Trash2 className="h-3.5 w-3.5" /></IconButton>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!kbs.length && !busy && <p className="p-6 text-center text-xs text-fg-3">暂无知识库, 创建后可导入文档</p>}
    </div>

    {selected ? (
      <div className="space-y-3 rounded-2xl border border-line p-3.5 animate-fade">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <h3 className="text-[13.5px] font-semibold text-fg">{selected.name}</h3>
              <Badge tone={selected.scope === 'global' ? 'neutral' : 'green'}>{selected.scope === 'global' ? '全局库' : '会话库'}</Badge>
              {selected.isDefault && <Badge tone="green">默认</Badge>}
              <span className="num font-mono text-[10.5px] text-fg-4">rev {selected.revision}</span>
            </div>
            <p className="mt-0.5 text-[11px] text-fg-3">
              Embedding <span className="font-mono text-fg-2">{selected.config.embed || '未配置'}</span> · 分块 {selected.config.chunk_size}/{selected.config.chunk_overlap} · {selected.documentCount} 文档 / {selected.chunkCount} 块
              {selected.lastIndexedAt ? ` · 最近索引 ${formatRelativeTime(selected.lastIndexedAt)}` : ''}
            </p>
            {selected.lastError && <p className="mt-1 text-[11px] text-red">{selected.lastError}</p>}
            {selected.status === 'indexing' && (
              <div className="mt-2 h-1.5 w-40 overflow-hidden rounded-full bg-glass-hover">
                {selected.progress === null
                  ? <div className="h-full w-1/3 animate-pulse rounded-full bg-[rgb(var(--rgb-green))]" />
                  : <div className="h-full rounded-full bg-[rgb(var(--rgb-green))] transition-[width] duration-700" style={{ width: `${selected.progress}%` }} />}
              </div>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" icon={<Upload className="h-3.5 w-3.5" />} disabled={Boolean(busy)} onClick={() => setImportTarget(selected)}>导入文档</Button>
            <Button size="sm" variant="ghost" icon={<Settings2 className="h-3.5 w-3.5" />} disabled={Boolean(busy)} onClick={() => setConfigTarget(selected)}>配置</Button>
            <Button size="sm" variant="ghost" icon={<RotateCw className="h-3.5 w-3.5" />} disabled={Boolean(busy) || selected.documentCount === 0 || selected.status === 'indexing'} onClick={() => void rebuild(selected)}>重建</Button>
          </div>
        </div>

        <div className="rounded-xl border border-line">
          <div className="flex items-center justify-between border-b border-line px-3 py-2 text-[11px] text-fg-4"><span>已索引文档</span><span>来源 / 块</span></div>
          {!docs.length && <p className="px-3 py-5 text-center text-xs text-fg-3">还没有文档, 导入后会显示在这里</p>}
          {docs.map((doc) => (
            <div key={doc.sourceId} className="flex items-center justify-between gap-3 border-b border-line px-3 py-2 text-[12px] last:border-0">
              <span className="flex min-w-0 items-center gap-2"><FileText className="h-3.5 w-3.5 flex-none text-fg-3" /><span className="truncate text-fg-2">{doc.source}</span></span>
              <span className="flex flex-none items-center gap-3">
                <span className="num font-mono text-[10.5px] text-fg-4">{doc.chunkCount} 块{doc.updatedAt ? ` · ${formatRelativeTime(doc.updatedAt)}` : ''}</span>
                {doc.status === 'failed' && <Badge tone="red">失败</Badge>}
                <IconButton label={`删除文档 ${doc.source}`} size="sm" disabled={Boolean(busy)} onClick={() => void removeDoc(selected, doc)}><Trash2 className="h-3 w-3" /></IconButton>
              </span>
            </div>
          ))}
        </div>

        <div className="space-y-2 border-t border-line pt-3">
          <h4 className="flex items-center gap-1.5 text-[12.5px] font-medium text-fg"><Search className="h-3.5 w-3.5 text-fg-3" />测试检索</h4>
          <Field label="检索问题">
            <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入问题测试检索效果" />
          </Field>
          <Field label="重排配置">
            <Select value={rerank} onChange={setRerank} options={[{ value: '', label: '不使用重排' }, ...rerankOptions(modelOptions)]} />
          </Field>
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10.5px] text-fg-4">当前库: {selected.scope === 'global' ? '全局' : '会话'} · 支持 {capabilities.extensions.join(' / ') || '由后端声明'}</span>
            <Button size="sm" variant="tinted" tint="green" icon={<Search className="h-3.5 w-3.5" />} loading={busy === '检索'} disabled={!query.trim() || Boolean(busy)} onClick={() => void runSearch()}>测试检索</Button>
          </div>
          {searchResult && (
            <pre className="scroll max-h-64 rounded-xl border border-line bg-[rgb(0_0_0/0.22)] p-3 font-mono text-[11px] leading-5 text-fg-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)] animate-pop">
              {JSON.stringify(searchResult, null, 2)}
            </pre>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
          <p className="text-[10.5px] text-fg-4">模型或分块参数变化需重建; 重建成功后切换索引, 失败时保留原配置和数据.</p>
          <Button size="sm" variant="danger" icon={<Trash2 className="h-3.5 w-3.5" />} disabled={Boolean(busy)} onClick={() => void removeKb(selected)}>删除知识库</Button>
        </div>
      </div>
    ) : (
      <div className="rounded-2xl border border-line">
        <div className="flex flex-col items-center px-6 py-8 text-center tint-green">
          <BookOpen className="h-6 w-6 text-tint" />
          <p className="mt-2 text-[13px] font-medium text-fg">选择一个知识库</p>
          <p className="mt-1 text-xs text-fg-3">或新建一个知识库并导入文档</p>
        </div>
      </div>
    )}

    {configTarget && (
      <ConfigModal
        key={configTarget === 'new' ? 'new' : configTarget.id}
        target={configTarget}
        context={{ platformId, sessionId, label: sessionId ? `${platformId} / ${sessionId}` : `全局与 ${platformId} 平台` }}
        modelOptions={modelOptions}
        onClose={() => setConfigTarget(null)}
        onSave={saveConfig}
        onRebuild={(config) => {
          if (configTarget === 'new') return;
          const kb = configTarget;
          setConfigTarget(null);
          void rebuild(kb, config);
        }}
      />
    )}

    {importTarget && (
      <ImportModal
        key={importTarget.id}
        context={context}
        target={importTarget}
        capabilities={capabilities}
        existingSources={docs.filter((doc) => doc.status === 'indexed' || doc.status === 'indexing').map((doc) => doc.source)}
        onClose={() => setImportTarget(null)}
        onComplete={handleImport}
      />
    )}
  </div>;
}
