import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { AlertCircle, ChevronLeft, ChevronRight, Database, FileText, FolderTree, Info, Layers, Plus, RefreshCw, RotateCw, Search, ShieldAlert, Trash2 } from 'lucide-react';
import { cn } from '@/utils/cn';
import { controlApi } from '@/api/control';
import { ragApi, ragErrorMessage, type KnowledgeBase, type RagContext, type RagResult } from '@/api/rag';
import { ragJobsApi, type RagJob, type RagScope } from '@/api/contracts';
import { ApiError } from '@/api/client';
import type { ChatHistoryItem } from '@/api/chat';
import type { RuntimeSession } from '@/api/types';
import { Button, IconButton } from '@/components/kit/Button';import { Card } from '@/components/kit/Card';
import { CountUp, EmptyState, PageHeader, Skeleton } from '@/components/kit/Feedback';
import { Input } from '@/components/kit/Field';
import { Modal } from '@/components/kit/Modal';
import { Tabs } from '@/components/kit/Tabs';
import { toast } from '@/components/kit/Toast';
import { ConfigModal } from '@/components/rag/ConfigModal';
import { DetailDrawer } from '@/components/rag/DetailDrawer';
import { ImportModal } from '@/components/rag/ImportModal';
import { KnowledgeBaseCard, type KbAction } from '@/components/rag/KnowledgeBaseCard';
import { isJobPayload, jobsUnavailable, readDocuments, readLatestJob, waitForJob } from '@/components/rag/jobs';
import {
  emptyCapabilities, newOperationId, toDoc, toKb, toLegacyDoc,
  type Doc, type ImportSummary, type Kb, type KBConfig, type KBDraft,
} from '@/components/rag/types';

type Filter = 'all' | 'global' | 'session' | 'issue';

interface ScopeOption { id: string; label: string; platformId: string; sessionId?: string; description?: string }

const pageSize = 50;

export function Rag() {
  const [sessions, setSessions] = useState<RuntimeSession[]>([]);
  const [scopeId, setScopeId] = useState('');
  const [chatSearch, setChatSearch] = useState('');
  const [chatSession, setChatSession] = useState('');
  const [chatPage, setChatPage] = useState(1);
  const [chatSessions, setChatSessions] = useState<ChatHistoryItem[]>([]);
  const [chatTotal, setChatTotal] = useState(0);
  const [data, setData] = useState<RagResult | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  const [jobs, setJobs] = useState<Record<string, RagJob>>({});
  const [filter, setFilter] = useState<Filter>('all');
  const [q, setQ] = useState('');
  const [detailId, setDetailId] = useState<string | null>(null);
  const [configTarget, setConfigTarget] = useState<Kb | 'new' | null>(null);
  const [importTargetId, setImportTargetId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Kb | null>(null);
  const [docDelete, setDocDelete] = useState<{ kb: Kb; doc: Doc } | null>(null);
  const [rebuildTarget, setRebuildTarget] = useState<{ kb: Kb; config?: KBConfig } | null>(null);
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState('');
  const [docs, setDocs] = useState<Doc[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);
  const [docsError, setDocsError] = useState('');

  const selectedSession = sessions.find((item) => `${item.platform_id}:${item.session_id}` === scopeId);
  const platformId = selectedSession?.platform_id || (scopeId.startsWith('platform:') ? scopeId.slice(9) : 'local');
  const sessionId = selectedSession?.session_id || (platformId === 'chat' ? chatSession || undefined : undefined);

  const context: RagContext = useMemo(() => ({ platformId, sessionId, via: 'control' }), [platformId, sessionId]);

  const scopeOptions = useMemo<ScopeOption[]>(() => {
    const platforms = [...new Set(['chat', ...sessions.map((item) => item.platform_id)])].filter((item) => item !== 'local');
    return [
      { id: '', label: '全局与 local 平台', platformId: 'local', description: '全局库 + 本机工作台会话库' },
      ...platforms.map((item) => ({ id: `platform:${item}`, label: `全局与 ${item} 平台`, platformId: item, description: `全局库 + 全部 ${item} 会话库` })),
      ...sessions.map((item) => ({ id: `${item.platform_id}:${item.session_id}`, label: `${item.platform_id} / ${item.session_id}`, platformId: item.platform_id, sessionId: item.session_id })),
    ];
  }, [sessions]);

  const scope = scopeOptions.find((option) => option.id === scopeId) ?? scopeOptions[0];
  const contextLabel = sessionId ? `${scope.label} · ${sessionId}` : scope.label;

  useEffect(() => {
    controlApi.listSessionInstances().then((result) => setSessions(result.sessions)).catch((reason: Error) => setLoadError(reason.message));
  }, []);

  useEffect(() => {
    if (platformId !== 'chat') return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      controlApi.queryChatHistory({ search: chatSearch, page: chatPage, page_size: pageSize }).then((result) => {
        if (!cancelled) { setChatSessions(result.items); setChatTotal(result.total); }
      }).catch((reason: Error) => { if (!cancelled) setLoadError(reason.message); });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [platformId, chatSearch, chatPage]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const result = await ragApi.list(context);
      setData(result);
      setLoadError('');
    } catch (reason) {
      setLoadError(ragErrorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [context]);

  useEffect(() => { void reload(); }, [reload, refreshKey]);

  useEffect(() => {
    setDetailId(null);
    setImportTargetId(null);
    setConfigTarget(null);
    setDeleteTarget(null);
    setFilter('all');
    setQ('');
    setNotice('');
  }, [platformId, sessionId]);

  useEffect(() => {
    if (!data) return;
    let cancelled = false;
    const current = data.knowledge_bases;
    void Promise.all(current.map(async (item) => {
      const job = await readLatestJob({
        platform_id: platformId,
        session_id: item.scope === 'session' ? item.session_id : sessionId || '',
        kb_id: item.id,
      }, 'control');
      return [item.id, job] as const;
    })).then((entries) => {
      if (cancelled) return;
      const next: Record<string, RagJob> = {};
      for (const [id, job] of entries) if (job) next[id] = job;
      setJobs(next);
    });
    return () => { cancelled = true; };
  }, [data, platformId, sessionId]);

  const kbs = useMemo(() => (data?.knowledge_bases ?? []).map((item) => toKb(item, jobs[item.id])), [data, jobs]);
  const modelOptions = data?.model_options;
  const capabilities = data?.upload ?? emptyCapabilities;

  const visibleKbs = kbs;
  const counts = {
    all: visibleKbs.length,
    global: visibleKbs.filter((kb) => kb.scope === 'global').length,
    session: visibleKbs.filter((kb) => kb.scope === 'session').length,
    issue: visibleKbs.filter((kb) => kb.status === 'error').length,
  };
  const indexing = visibleKbs.filter((kb) => kb.status === 'indexing').length;
  const totals = visibleKbs.reduce((sum, kb) => ({ docs: sum.docs + kb.documentCount, chunks: sum.chunks + kb.chunkCount }), { docs: 0, chunks: 0 });

  const list = visibleKbs
    .filter((kb) => filter === 'all' || kb.scope === filter || (filter === 'issue' && kb.status === 'error'))
    .filter((kb) => `${kb.name} ${kb.sessionId}`.toLowerCase().includes(q.trim().toLowerCase()));

  const detailKb = detailId ? kbs.find((kb) => kb.id === detailId) ?? null : null;
  const importTarget = importTargetId ? kbs.find((kb) => kb.id === importTargetId) ?? null : null;
  const docsTargetId = detailId ?? importTargetId;

  useEffect(() => {
    if (!docsTargetId) { setDocs([]); setDocsError(''); return; }
    let cancelled = false;
    setDocsLoading(true);
    setDocsError('');
    const raw = data?.knowledge_bases.find((item) => item.id === docsTargetId);
    const scopeForDocs: RagScope = {
      platform_id: platformId,
      session_id: raw?.scope === 'session' ? raw.session_id : sessionId || '',
      kb_id: docsTargetId,
    };
    void (async () => {
      try {
        const catalog = await readDocuments(scopeForDocs, 'control');
        if (cancelled) return;
        if (catalog) setDocs(catalog.map(toDoc));
        else {
          const result = await ragApi.list(context, docsTargetId);
          if (!cancelled) setDocs(result.documents.map(toLegacyDoc));
        }
      } catch (reason) {
        if (!cancelled) setDocsError(ragErrorMessage(reason));
      } finally {
        if (!cancelled) setDocsLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [docsTargetId, context, platformId, sessionId, refreshKey, data]);

  // 不在此处清理失效 id: 创建知识库后目标 id 会先于列表刷新到达, 用旧 data 清理会把刚打开的目标关掉。
  // 失效目标在渲染时自然解析为 null, 不影响后续刷新。

  const changeScope = (id: string) => {
    setScopeId(id);
    setChatSession('');
    setChatSearch('');
    setChatPage(1);
    setNotice('');
  };

  const refresh = () => {
    setRefreshKey((key) => key + 1);
    setNotice('');
    toast.info('正在刷新知识库…');
  };

  const saveConfig = async (draft: KBDraft, expectedRevision: number): Promise<string | null> => {
    if (configTarget === 'new') {
      if (draft.scope === 'session' && !sessionId) return '当前管理范围没有具体会话，无法创建会话库';
      try {
        const result = await ragApi.action(context, { action: 'create', name: draft.name, scope: draft.scope, config: draft.config });
        if (result.ok === false) throw new Error(String(result.error || '创建未完成'));
        const created = result.knowledge_base as KnowledgeBase | undefined;
        if (created) {
          // 打开详情以便导入完成后立即看到文档目录, 同时打开导入弹窗
          setDetailId(created.id);
          setImportTargetId(created.id);
        }
        setNotice(`知识库「${draft.name}」已创建，现在可以导入文档`);
        setRefreshKey((key) => key + 1);
        toast.success('知识库已创建', `${draft.name} · 现在可以导入文档`);
        return null;
      } catch (reason) {
        return ragErrorMessage(reason);
      }
    }

    if (!configTarget) return '编辑目标已失效，请重新打开配置';
    const current = kbs.find((kb) => kb.id === configTarget.id);
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
      setNotice(`知识库「${draft.name}」配置已保存 · v${expectedRevision + 1}`);
      setRefreshKey((key) => key + 1);
      toast.success('知识库配置已保存', `${draft.name} · v${expectedRevision + 1}`);
      return null;
    } catch (reason) {
      return ragErrorMessage(reason);
    }
  };

  const startRebuild = async () => {
    const target = rebuildTarget;
    if (!target) return;
    if (target.kb.status === 'indexing') {
      setRebuildTarget(null);
      toast.info('索引正在构建中', '请等待当前任务完成后再重建');
      return;
    }
    setRebuildTarget(null);
    setWorking(true);
    const config = target.config ?? target.kb.config;
    const scopeForJob: RagScope = {
      platform_id: platformId,
      session_id: target.kb.scope === 'session' ? target.kb.sessionId : sessionId || '',
      kb_id: target.kb.id,
    };
    const operationId = newOperationId();
    try {
      let job: RagJob | null = null;
      try {
        const response = await ragJobsApi.submit(scopeForJob, {
          operation_id: operationId, expected_revision: target.kb.revision, action: 'rebuild', config,
        }, 'control');
        if (isJobPayload(response)) job = response.job;
      } catch (reason) {
        if (!(jobsUnavailable(reason) || (reason instanceof ApiError && reason.status === 404))) throw reason;
      }
      if (job) {
        toast.info('开始重建索引', '后台重新向量化全部文档，期间旧索引仍可检索');
        const finished = await waitForJob(scopeForJob, job, 'control', (current) => {
          setJobs((previous) => ({ ...previous, [target.kb.id]: current }));
        });
        if (finished.status !== 'succeeded') throw new Error(finished.error?.message || '重建未完成');
        toast.success('索引重建完成', '新索引已原子切换，旧版本已清理');
      } else {
        const result = await ragApi.action(context, { action: 'rebuild', kb_id: target.kb.id, config, expected_revision: target.kb.revision });
        if (result.ok === false) throw new Error(String(result.error || '重建未完成'));
        toast.success('索引重建完成', '新索引已原子切换，旧版本已清理');
      }
      setNotice('索引重建完成，已切换到新版本');
      setRefreshKey((key) => key + 1);
    } catch (reason) {
      toast.error('重建索引失败', ragErrorMessage(reason));
    } finally {
      setWorking(false);
    }
  };

  const confirmDelete = async () => {
    const target = deleteTarget;
    if (!target || working) return;
    setWorking(true);
    try {
      const result = await ragApi.action(context, { action: 'delete', kb_id: target.id });
      if (result.ok === false) throw new Error(String(result.error || '删除未完成'));
      if (detailId === target.id) setDetailId(null);
      if (importTargetId === target.id) setImportTargetId(null);
      setDeleteTarget(null);
      setNotice(`知识库「${target.name}」及其文档与索引已移除`);
      setRefreshKey((key) => key + 1);
      toast.success('知识库已删除', `${target.name} 的文档与索引已移除`);
    } catch (reason) {
      toast.error('删除知识库失败', ragErrorMessage(reason));
    } finally {
      setWorking(false);
    }
  };

  const confirmDeleteDoc = async () => {
    const target = docDelete;
    if (!target || working) return;
    setWorking(true);
    try {
      const result = await ragApi.action(context, { action: 'delete_document', kb_id: target.kb.id, source_id: target.doc.sourceId });
      if (result.ok === false) throw new Error(String(result.error || '删除未完成'));
      setDocDelete(null);
      setNotice(`已删除文档 ${target.doc.source}`);
      setRefreshKey((key) => key + 1);
      toast.success('文档已删除', `${target.doc.source} · 移除 ${target.doc.chunkCount} 个片段`);
    } catch (reason) {
      toast.error('删除文档失败', ragErrorMessage(reason));
    } finally {
      setWorking(false);
    }
  };

  const handleImport = (summary: ImportSummary) => {
    if (summary.failed > 0) toast.warning('导入完成（部分失败）', `成功 ${summary.indexed} · 跳过 ${summary.skipped} · 失败 ${summary.failed}`);
    else if (summary.indexed === 0) toast.info('没有新增文档', `${summary.skipped} 个重复来源已跳过`);
    else toast.success('导入完成', `成功 ${summary.indexed} · 跳过 ${summary.skipped} · 新增 ${summary.chunks} 个片段`);
    setNotice(`本次导入：成功 ${summary.indexed}，跳过 ${summary.skipped}，失败 ${summary.failed}`);
    setRefreshKey((key) => key + 1);
  };

  const actions = (kb: Kb, action: KbAction) => {
    if (action === 'open') setDetailId(kb.id);
    else if (action === 'import') setImportTargetId(kb.id);
    else if (action === 'edit') setConfigTarget(kb);
    else if (action === 'rebuild') setRebuildTarget({ kb });
    else setDeleteTarget(kb);
  };

  const summary = [
    { label: '知识库', value: counts.all, sub: `全局 ${counts.global} · 会话 ${counts.session}`, icon: <Database className="h-4 w-4" />, tint: 'purple', tone: 'text-fg-4' },
    { label: '文档', value: totals.docs, sub: '当前管理范围', icon: <FileText className="h-4 w-4" />, tint: 'purple', tone: 'text-fg-4' },
    { label: '片段', value: totals.chunks, sub: '已完成向量化', icon: <Layers className="h-4 w-4" />, tint: 'purple', tone: 'text-fg-4' },
    { label: '索引异常', value: counts.issue, sub: indexing ? `${indexing} 个正在构建` : '全部可用', icon: <ShieldAlert className="h-4 w-4" />, tint: counts.issue ? 'red' : 'green', tone: counts.issue ? 'text-red' : 'text-green' },
  ];

  const chatPageCount = Math.max(1, Math.ceil(chatTotal / pageSize));

  return (
    <div className="space-y-5">
      <PageHeader
        title="知识库管理"
        description="管理全局与会话知识库，导入文档并测试检索召回"
        actions={<>
          <Button
            icon={<RefreshCw className={cn('h-4 w-4', loading && 'animate-spin-slow')} />}
            disabled={loading}
            onClick={refresh}
          >
            刷新
          </Button>
          <Button variant="primary" tint="purple" icon={<Plus className="h-4 w-4" />} onClick={() => setConfigTarget('new')}>新建知识库</Button>
        </>}
      />

      {loadError && <p role="alert" className="flex items-center gap-2 rounded-xl border border-[rgb(var(--rgb-red)/0.26)] bg-[rgb(var(--rgb-red)/0.08)] px-3 py-2 text-[13px] text-red"><AlertCircle className="h-4 w-4 flex-none" />{loadError}</p>}
      {notice && <p role="status" className="rounded-xl border border-line bg-glass px-3 py-2 text-[12.5px] text-fg-3">{notice}</p>}

      <Card tint="purple" padding="md" className="animate-rise overflow-hidden" style={{ animationDelay: '30ms' }}>
        <div aria-hidden className="pointer-events-none absolute -right-10 -top-20 h-52 w-52 rounded-full bg-[radial-gradient(circle,rgb(var(--rgb-purple)/0.28),transparent_65%)] blur-2xl" />
        <div className="relative grid grid-cols-2 gap-2.5 sm:grid-cols-4">
          {summary.map((item) => (
            <div key={item.label} className="flex items-start gap-2.5 rounded-xl border border-line bg-glass px-3 py-2.5">
              <span className={cn('grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint', `tint-${item.tint}`)}>{item.icon}</span>
              <div className="min-w-0">
                <div className="text-[10.5px] text-fg-4">{item.label}</div>
                <div className="mt-0.5 text-xl font-semibold leading-none text-fg"><CountUp value={item.value} /></div>
                <div className={cn('num mt-1 truncate text-[10.5px]', item.tone)}>{item.sub}</div>
              </div>
            </div>
          ))}
        </div>
      </Card>

      <Card tint="purple" padding="sm" className="animate-rise" style={{ animationDelay: '65ms' }}>
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
          <span className="flex items-center gap-2 pl-1 text-xs font-medium text-fg-2"><FolderTree className="h-4 w-4 text-purple" />管理范围</span>
          <div className="w-full sm:w-[280px]">
            <select aria-label="管理范围" className="field" value={scopeId} onChange={(event) => changeScope(event.target.value)}>
              {scopeOptions.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
            </select>
          </div>

          {platformId === 'chat' && !selectedSession && (
            <>
              <div className="w-full sm:w-44">
                <Input
                  leading={<Search className="h-3.5 w-3.5" />}
                  aria-label="搜索 Chat 会话"
                  placeholder="搜索 Chat 会话"
                  value={chatSearch}
                  className="!h-9 !text-[12.5px]"
                  onChange={(event) => { setChatSearch(event.target.value); setChatPage(1); }}
                />
              </div>
              <div className="w-full sm:w-56">
                <select aria-label="Chat 会话" className="field" value={chatSession} onChange={(event) => { setChatSession(event.target.value); setChatPage(1); }}>
                  <option value="">全部 Chat 会话知识库</option>
                  {chatSession && !chatSessions.some((session) => session.conversation_id === chatSession) && <option value={chatSession}>{chatSession}</option>}
                  {chatSessions.map((session) => <option key={session.conversation_id} value={session.conversation_id}>{session.title || session.conversation_id}</option>)}
                </select>
              </div>
              <div className="flex items-center gap-0.5 text-[11.5px] text-fg-3">
                <IconButton label="上一页会话" size="sm" disabled={chatPage <= 1} onClick={() => setChatPage((page) => Math.max(1, page - 1))}><ChevronLeft className="h-3.5 w-3.5" /></IconButton>
                <span className="num px-1">第 {chatPage} 页 · {chatTotal} 个会话</span>
                <IconButton label="下一页会话" size="sm" disabled={chatPage >= chatPageCount} onClick={() => setChatPage((page) => Math.min(chatPageCount, page + 1))}><ChevronRight className="h-3.5 w-3.5" /></IconButton>
              </div>
            </>
          )}

          <span className="ml-auto hidden text-[11px] text-fg-4 lg:block">
            全局库始终可见 · {sessionId ? `当前会话 ${sessionId}` : `${platformId} 平台全部会话`}
          </span>
        </div>
      </Card>

      <div className="flex flex-wrap items-center gap-3 animate-rise" style={{ animationDelay: '100ms' }}>
        <Tabs
          label="知识库筛选"
          value={filter}
          onChange={setFilter}
          items={[
            { value: 'all', label: '全部', count: counts.all },
            { value: 'global', label: '全局', count: counts.global },
            { value: 'session', label: '会话', count: counts.session },
            { value: 'issue', label: '异常', count: counts.issue },
          ]}
        />
        <div className="ml-auto flex min-w-0 flex-wrap items-center gap-2">
          <div className="w-52 min-w-[150px]">
            <Input leading={<Search className="h-4 w-4" />} aria-label="搜索知识库" placeholder="搜索知识库…" value={q} onChange={(event) => setQ(event.target.value)} />
          </div>
          <span className="num hidden text-[11px] text-fg-4 sm:block">{list.length} / {visibleKbs.length}</span>
        </div>
      </div>

      {loading && !data ? (
        <div className="stagger grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, index) => (
            <Card key={index} padding="md" style={{ ['--i' as string]: index } as CSSProperties}>
              <div className="flex items-start gap-3">
                <Skeleton className="h-10 w-10 !rounded-xl" />
                <div className="flex-1 space-y-2"><Skeleton className="h-4 w-2/3" /><Skeleton className="h-3 w-1/2" /></div>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-2"><Skeleton className="h-14 !rounded-xl" /><Skeleton className="h-14 !rounded-xl" /></div>
              <div className="mt-3 flex gap-1.5"><Skeleton className="h-5 w-16 !rounded-full" /><Skeleton className="h-5 w-24 !rounded-full" /></div>
              <div className="mt-4 flex items-center justify-between border-t border-line pt-3"><Skeleton className="h-3 w-24" /><Skeleton className="h-7 w-28 !rounded-lg" /></div>
            </Card>
          ))}
        </div>
      ) : list.length === 0 ? (
        <Card padding="none" className="animate-rise">
          {visibleKbs.length === 0 ? (
            <EmptyState
              tint="purple"
              icon={<Database className="h-6 w-6" />}
              title="当前范围还没有知识库"
              description={sessionId ? `为会话 ${sessionId} 创建全局或会话知识库，导入文档后即可被检索。` : '创建一个全局库供所有会话检索，或在具体会话中选择“会话库”。'}
              actions={<Button variant="primary" tint="purple" icon={<Plus className="h-4 w-4" />} aria-label="新建知识库（空状态）" onClick={() => setConfigTarget('new')}>新建知识库</Button>}
            />
          ) : (
            <EmptyState
              tint="purple"
              icon={<Search className="h-6 w-6" />}
              title={q ? `没有匹配 “${q}” 的知识库` : '没有符合条件的知识库'}
              description="调整筛选条件，或清空搜索关键字后重试。"
              actions={<Button onClick={() => { setQ(''); setFilter('all'); }}>清除筛选</Button>}
            />
          )}
        </Card>
      ) : (
        <div key={`${scopeId}-${sessionId ?? ''}-${filter}`} className="stagger grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {list.map((kb, index) => (
            <KnowledgeBaseCard key={kb.id} kb={kb} index={index} onAction={(action) => actions(kb, action)} />
          ))}
          <button
            type="button"
            aria-label="在当前范围新建知识库"
            onClick={() => setConfigTarget('new')}
            className="group grid min-h-[206px] place-items-center rounded-[var(--radius-lg)] border-2 border-dashed border-[var(--glass-border-strong)] text-fg-3 transition-all duration-300 hover:border-[rgb(var(--rgb-purple)/0.5)] hover:bg-[rgb(var(--rgb-purple)/0.06)] hover:text-purple"
            style={{ ['--i' as string]: list.length } as CSSProperties}
          >
            <span className="flex flex-col items-center gap-2 text-sm font-medium">
              <span className="grid h-10 w-10 place-items-center rounded-xl border border-current/20 transition-transform duration-300 group-hover:rotate-90 group-hover:scale-110"><Plus className="h-5 w-5" /></span>
              新建知识库
            </span>
          </button>
        </div>
      )}

      {detailKb && (
        <DetailDrawer
          kb={detailKb}
          docs={docs}
          context={context}
          capabilities={capabilities}
          modelOptions={modelOptions}
          onClose={() => setDetailId(null)}
          onImport={() => setImportTargetId(detailKb.id)}
          onEdit={() => setConfigTarget(detailKb)}
          onRebuild={() => setRebuildTarget({ kb: detailKb })}
          onDelete={() => setDeleteTarget(detailKb)}
          onDeleteDocument={(doc) => setDocDelete({ kb: detailKb, doc })}
          onSearch={() => setNotice('')}
        />
      )}

      {docsLoading && detailKb && <span className="sr-only" aria-live="polite">正在读取文档目录</span>}
      {docsError && detailKb && <p role="alert" className="sr-only">{docsError}</p>}

      {configTarget && (
        <ConfigModal
          key={configTarget === 'new' ? 'new' : configTarget.id}
          target={configTarget}
          context={{ platformId, sessionId, label: contextLabel }}
          modelOptions={modelOptions}
          onClose={() => setConfigTarget(null)}
          onSave={saveConfig}
          onRebuild={(config) => {
            if (configTarget === 'new') return;
            setRebuildTarget({ kb: configTarget, config });
            setConfigTarget(null);
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
          onClose={() => setImportTargetId(null)}
          onComplete={handleImport}
        />
      )}

      {deleteTarget && (
        <Modal
          open
          onClose={() => { if (!working) setDeleteTarget(null); }}
          title="删除知识库"
          description="此操作不可撤销"
          icon={<Trash2 className="h-[18px] w-[18px]" />}
          size="sm"
          className="tint-red"
          footer={<>
            <Button variant="ghost" disabled={working} onClick={() => setDeleteTarget(null)}>取消</Button>
            <Button variant="danger" loading={working} onClick={() => void confirmDelete()}>删除</Button>
          </>}
        >
          <p className="text-sm leading-6 text-fg-2">
            将删除知识库 <strong className="text-fg">{deleteTarget.name}</strong>
            及其 <span className="num text-fg">{deleteTarget.documentCount}</span> 份文档、
            <span className="num text-fg">{deleteTarget.chunkCount}</span> 个片段。
          </p>
          <div className="mt-3 flex items-start gap-2 rounded-xl border border-[rgb(var(--rgb-red)/0.28)] bg-[rgb(var(--rgb-red)/0.08)] px-3 py-2.5 text-[12px] leading-5 tint-red">
            <AlertCircle className="mt-0.5 h-4 w-4 flex-none text-tint" />
            <span className="text-fg-2">已有引用将立即失效，索引目录会被清理。正在进行的检索会失败，请确认没有 Agent 或会话依赖此库。</span>
          </div>
        </Modal>
      )}

      {docDelete && (
        <Modal
          open
          onClose={() => { if (!working) setDocDelete(null); }}
          title="删除文档"
          description={`来自「${docDelete.kb.name}」`}
          icon={<Trash2 className="h-[18px] w-[18px]" />}
          size="sm"
          className="tint-red"
          footer={<>
            <Button variant="ghost" disabled={working} onClick={() => setDocDelete(null)}>取消</Button>
            <Button variant="danger" loading={working} onClick={() => void confirmDeleteDoc()}>删除文档</Button>
          </>}
        >
          <p className="text-sm leading-6 text-fg-2">
            将移除 <strong className="text-fg">{docDelete.doc.source}</strong> 及其 <span className="num text-fg">{docDelete.doc.chunkCount}</span> 个片段，其他文档不受影响。
          </p>
          <p className="mt-2 text-[12px] leading-5 text-fg-3">来源名称可以重新导入；如果重复策略为“跳过”，请先删除后重新导入。</p>
        </Modal>
      )}

      {rebuildTarget && (
        <Modal
          open
          onClose={() => setRebuildTarget(null)}
          title="重建索引"
          description="使用当前参数重新向量化全部文档"
          icon={<RotateCw className="h-[18px] w-[18px]" />}
          size="md"
          className="tint-purple"
          footer={<>
            <Button variant="ghost" onClick={() => setRebuildTarget(null)}>取消</Button>
            <Button variant="primary" tint="purple" icon={<RotateCw className="h-4 w-4" />} onClick={() => void startRebuild()}>开始重建</Button>
          </>}
        >
          <p className="text-sm leading-6 text-fg-2">
            将重建 <strong className="text-fg">{rebuildTarget.kb.name}</strong> 的索引，涉及
            <span className="num text-fg"> {rebuildTarget.kb.documentCount} </span>份文档、
            <span className="num text-fg"> {rebuildTarget.kb.chunkCount} </span>个片段。
          </p>
          {rebuildTarget.config && (
            rebuildTarget.config.embed !== rebuildTarget.kb.config.embed
            || rebuildTarget.config.chunk_size !== rebuildTarget.kb.config.chunk_size
            || rebuildTarget.config.chunk_overlap !== rebuildTarget.kb.config.chunk_overlap
          ) && (
            <div className="mt-3 space-y-1.5 rounded-xl border border-line bg-glass px-3 py-2.5 text-[11.5px] text-fg-3">
              {rebuildTarget.config.embed !== rebuildTarget.kb.config.embed && (
                <div className="flex flex-wrap gap-x-2"><span className="w-16 flex-none text-fg-4">Embedding</span><span className="font-mono text-fg-2">{rebuildTarget.kb.config.embed} → {rebuildTarget.config.embed}</span></div>
              )}
              {(rebuildTarget.config.chunk_size !== rebuildTarget.kb.config.chunk_size || rebuildTarget.config.chunk_overlap !== rebuildTarget.kb.config.chunk_overlap) && (
                <div className="flex flex-wrap gap-x-2"><span className="w-16 flex-none text-fg-4">分块 / 重叠</span><span className="num text-fg-2">{rebuildTarget.kb.config.chunk_size}/{rebuildTarget.kb.config.chunk_overlap} → {rebuildTarget.config.chunk_size}/{rebuildTarget.config.chunk_overlap}</span></div>
              )}
            </div>
          )}
          <div className="mt-3 flex items-start gap-2 rounded-xl border border-[rgb(var(--rgb-purple)/0.26)] bg-[rgb(var(--rgb-purple)/0.08)] px-3 py-2.5 text-[12px] leading-5">
            <Info className="mt-0.5 h-4 w-4 flex-none text-purple" />
            <span className="text-fg-2">重建在后台执行，期间旧索引仍可检索。成功后新索引原子切换并清理旧版本；失败时保留原配置与数据，不中断现有检索。</span>
          </div>
        </Modal>
      )}
    </div>
  );
}
