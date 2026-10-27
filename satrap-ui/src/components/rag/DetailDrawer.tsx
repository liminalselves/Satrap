import { useMemo, useState } from 'react';
import {
  AlertCircle, AlertTriangle, ChevronDown, Copy, Database, FileText, Loader2,
  RotateCw, ScanSearch, Search, Settings2, Star, Trash2, Upload, X,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { formatRelativeTime } from '@/utils/format';
import { ragApi, ragErrorMessage, type RagContext, type UploadCapabilities } from '@/api/rag';
import { ragJobsApi, type RagChunk } from '@/api/contracts';
import { Badge, StatusDot } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { Drawer } from './Drawer';
import { EmptyState } from '@/components/kit/Feedback';
import { Field, Input, Textarea } from '@/components/kit/Field';
import { Tabs } from '@/components/kit/Tabs';
import { toast } from '@/components/kit/Toast';
import type { ModelOptions } from '@/components/common/PluginConfigFields';
import { isRagPage } from './jobs';
import { docTint, jobPhaseLabel, rerankOptions, scopeLabel, type Doc, type DocStatus, type Kb } from './types';

const docStatusMeta: Record<DocStatus, { label: string; tone: 'green' | 'purple' | 'neutral' | 'red'; live?: boolean }> = {
  indexed: { label: '已索引', tone: 'green' },
  indexing: { label: '索引中', tone: 'purple', live: true },
  pending: { label: '待处理', tone: 'neutral' },
  failed: { label: '失败', tone: 'red' },
};

function DocumentsPane({ kb, docs, onDeleteDocument, onImport, context }: {
  kb: Kb;
  docs: Doc[];
  onDeleteDocument: (doc: Doc) => void;
  onImport: () => void;
  context: RagContext;
}) {
  const [q, setQ] = useState('');
  const [sort, setSort] = useState('recent');
  const [filter, setFilter] = useState<'all' | 'indexed' | 'working' | 'failed'>('all');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [chunks, setChunks] = useState<Record<string, { loading: boolean; error: string; items: RagChunk[] }>>({});

  const counts = {
    all: docs.length,
    indexed: docs.filter((doc) => doc.status === 'indexed').length,
    working: docs.filter((doc) => doc.status === 'indexing' || doc.status === 'pending').length,
    failed: docs.filter((doc) => doc.status === 'failed').length,
  };

  const list = useMemo(() => {
    const filtered = docs
      .filter((doc) => filter === 'all'
        || (filter === 'indexed' && doc.status === 'indexed')
        || (filter === 'working' && (doc.status === 'indexing' || doc.status === 'pending'))
        || (filter === 'failed' && doc.status === 'failed'))
      .filter((doc) => doc.source.toLowerCase().includes(q.trim().toLowerCase()));
    const sorted = [...filtered];
    if (sort === 'name') sorted.sort((a, b) => a.source.localeCompare(b.source, 'zh-CN'));
    else if (sort === 'chunks') sorted.sort((a, b) => b.chunkCount - a.chunkCount);
    else sorted.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));
    return sorted;
  }, [docs, filter, q, sort]);

  const loadChunks = async (doc: Doc) => {
    if (!doc.documentId) {
      setChunks((current) => ({ ...current, [doc.sourceId]: { loading: false, error: '当前文档来自旧接口，暂无片段 ID', items: [] } }));
      return;
    }
    setChunks((current) => ({ ...current, [doc.sourceId]: { loading: true, error: '', items: current[doc.sourceId]?.items ?? [] } }));
    try {
      const page = await ragJobsApi.chunks({ platform_id: context.platformId, session_id: context.sessionId || kb.sessionId || '', kb_id: kb.id }, doc.documentId, { include_text: true, limit: 20 }, context.via);
      if (!isRagPage<RagChunk>(page)) throw new Error('片段接口不可用');
      setChunks((current) => ({ ...current, [doc.sourceId]: { loading: false, error: '', items: page.items } }));
    } catch (reason) {
      setChunks((current) => ({ ...current, [doc.sourceId]: { loading: false, error: ragErrorMessage(reason), items: [] } }));
    }
  };

  return (
    <div className="flex min-h-0 flex-col border-line lg:border-r">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-3">
        <Tabs
          size="sm"
          label="文档状态筛选"
          value={filter}
          onChange={setFilter}
          items={[
            { value: 'all', label: '全部', count: counts.all },
            { value: 'indexed', label: '已索引', count: counts.indexed },
            { value: 'working', label: '处理中', count: counts.working },
            { value: 'failed', label: '失败', count: counts.failed },
          ]}
        />
        <div className="ml-auto flex min-w-0 flex-wrap items-center gap-2">
          <div className="w-44 min-w-[130px]"><Input leading={<Search className="h-3.5 w-3.5" />} aria-label="筛选文档" placeholder="筛选文档…" value={q} onChange={(event) => setQ(event.target.value)} className="!h-8 !text-[12.5px]" /></div>
          <div className="w-28">
            <select aria-label="文档排序" className="field !h-8 !text-[12.5px]" value={sort} onChange={(event) => setSort(event.target.value)}>
              <option value="recent">最近更新</option>
              <option value="name">名称</option>
              <option value="chunks">片段数</option>
            </select>
          </div>
        </div>
      </div>

      <div className="scroll min-h-0 flex-1 px-5 py-3">
        {docs.length === 0 ? (
          <EmptyState
            tint="purple"
            icon={<FileText className="h-6 w-6" />}
            title="还没有文档"
            description="导入 PDF、Word、Excel 或纯文本后，切片会按当前分块参数写入索引。"
            actions={<Button variant="primary" tint="purple" icon={<Upload className="h-4 w-4" />} onClick={onImport}>上传文档</Button>}
            className="py-10"
          />
        ) : list.length === 0 ? (
          <EmptyState tint="purple" icon={<Search className="h-6 w-6" />} title="没有匹配的文档" description="调整筛选条件或清空搜索关键字。" actions={<Button onClick={() => { setQ(''); setFilter('all'); }}>清除筛选</Button>} className="py-10" />
        ) : (
          <ul className="space-y-2">
            {list.map((doc, index) => {
              const meta = docStatusMeta[doc.status];
              const open = expanded === doc.sourceId;
              const chunkState = chunks[doc.sourceId];
              return (
                <li key={doc.sourceId} className="group animate-rise rounded-xl border border-line bg-glass transition-colors hover:bg-glass-hover" style={{ animationDelay: `${Math.min(index, 8) * 35}ms` }}>
                  <div className="flex items-start gap-2.5 p-3">
                    <span className={cn('mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint', doc.status === 'failed' ? 'tint-red' : `tint-${docTint(doc.source)}`)}>
                      <FileText className="h-4 w-4" />
                    </span>
                    <button
                      type="button"
                      onClick={() => setExpanded(open ? null : doc.sourceId)}
                      aria-expanded={open}
                      className="min-w-0 flex-1 text-left"
                    >
                      <span className="flex items-center gap-2">
                        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg" title={doc.source}>{doc.source} · {doc.chunkCount} 块</span>
                        <Badge tone={meta.tone} dot live={meta.live}>{meta.label}</Badge>
                        <ChevronDown className={cn('h-3.5 w-3.5 flex-none text-fg-4 transition-transform duration-300', open && 'rotate-180')} />
                      </span>
                      <span className="num mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-fg-4">
                        <span>{doc.chunkCount} 个片段</span>
                        {doc.contentHash && <span className="font-mono" title={doc.contentHash}>#{doc.contentHash.slice(0, 8)}</span>}
                        <span>{doc.status === 'pending' ? '等待写入索引' : doc.updatedAt ? formatRelativeTime(doc.updatedAt) : '时间未知'}</span>
                      </span>
                    </button>
                    <IconButton label={`删除文档 ${doc.source}`} size="sm" className="opacity-60 transition-opacity group-hover:opacity-100 hover:opacity-100" onClick={() => onDeleteDocument(doc)}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </IconButton>
                  </div>
                  {open && (
                    <div className="animate-fade space-y-2 border-t border-line px-3 py-2.5 pl-[54px]">
                      {doc.status === 'failed' && doc.error ? (
                        <p className="flex items-start gap-1.5 text-[12px] leading-5 text-red"><AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-none" />{doc.error}</p>
                      ) : doc.status === 'pending' ? (
                        <p className="text-[12px] leading-5 text-fg-3">文档已进入队列，向量化完成后即可被检索。</p>
                      ) : doc.status === 'indexing' ? (
                        <p className="flex items-center gap-1.5 text-[12px] leading-5 text-purple"><Loader2 className="h-3.5 w-3.5 animate-spin-slow" />正在切分并写入向量索引，旧版本仍可检索。</p>
                      ) : (
                        <p className="text-[12px] leading-5 text-fg-2">文档已就绪，可展开片段核对召回内容。</p>
                      )}
                      <div className="flex flex-wrap items-center gap-2">
                        {doc.status === 'failed' && <Button size="sm" variant="tinted" tint="purple" onClick={onImport}>重试导入</Button>}
                        {doc.status === 'indexed' && doc.documentId && (
                          <Button size="sm" variant="ghost" loading={chunkState?.loading} onClick={() => void loadChunks(doc)}>查看片段</Button>
                        )}
                        {doc.status === 'indexed' && doc.documentId && chunkState && !chunkState.loading && (
                          <span className="num text-[11px] text-fg-4">{chunkState.error ? chunkState.error : `已读取 ${chunkState.items.length} 个片段`}</span>
                        )}
                      </div>
                      {doc.status === 'indexed' && chunkState && chunkState.items.length > 0 && (
                        <div className="scroll max-h-40 space-y-1.5 rounded-lg border border-line bg-[rgb(0_0_0/0.14)] p-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)]">
                          {chunkState.items.slice(0, 5).map((chunk, chunkIndex) => (
                            <p key={String(chunk.chunk_id ?? chunkIndex)} className="num text-[11px] leading-5 text-fg-3">
                              <span className="mr-2 text-fg-4">#{chunkIndex + 1}</span>
                              {String(chunk.text ?? '（该片段未包含正文）').slice(0, 240)}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="num flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-5 py-2.5 text-[11px] text-fg-4">
        <span>共 {docs.length} 份 · {kb.chunkCount} 个片段</span>
        <span>分块 {kb.config.chunk_size} / 重叠 {kb.config.chunk_overlap}</span>
        <span className="ml-auto">来源名称决定去重，重新导入时按重复策略生效</span>
      </div>
    </div>
  );
}

function RetrievalPane({ kb, docs, context, modelOptions, onSearch }: {
  kb: Kb;
  docs: Doc[];
  context: RagContext;
  modelOptions?: ModelOptions;
  onSearch?: () => void;
}) {
  const [query, setQuery] = useState('');
  const [rerank, setRerank] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [searchedFor, setSearchedFor] = useState('');
  const [resultRerank, setResultRerank] = useState('');

  const samples = ['退款多久能到账？', '发票怎么开', '退货运费谁承担'];

  const run = async () => {
    if (!query.trim() || loading) return;
    if (kb.status === 'error') {
      setResult(null);
      setError(kb.lastError || '知识库上次操作失败，请先重建索引');
      return;
    }
    setLoading(true);
    setError('');
    onSearch?.();
    setSearchedFor(query.trim());
    setResultRerank(rerank);
    try {
      const response = await ragApi.action(context, {
        action: 'search',
        query: query.trim(),
        config: {
          db_scope: kb.scope,
          global_db_ids: kb.scope === 'global' ? [kb.id] : [],
          session_db_ids: kb.scope === 'session' ? [kb.id] : [],
          rerank,
        },
      });
      if (response.ok === false) throw new Error(String(response.error || '检索失败'));
      setResult(response);
    } catch (reason) {
      setResult(null);
      setError(ragErrorMessage(reason));
    } finally {
      setLoading(false);
    }
  };

  const hits = Array.isArray(result?.results) ? result.results as Array<Record<string, unknown>> : [];
  const warnings = Array.isArray(result?.warnings) ? result.warnings.map(String) : [];
  const elapsed = typeof result?.elapsed_ms === 'number' ? result.elapsed_ms : typeof result?.elapsed === 'number' ? result.elapsed : null;
  const candidates = typeof result?.candidates === 'number' ? result.candidates : null;

  const scoreOf = (item: Record<string, unknown>) => {
    const value = resultRerank ? item.rerank_score ?? item.score ?? item.similarity : item.similarity ?? item.score;
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  };

  const copy = () => {
    if (!result) return;
    void navigator.clipboard?.writeText(JSON.stringify(result, null, 2));
    toast.success('检索结果已复制', 'JSON 格式');
  };

  return (
    <div className="flex min-h-0 flex-col">
      <div className="space-y-3 border-b border-line px-5 py-3">
        <Field label="检索问题" hint="只执行向量检索与重排，不会调用对话模型">
          <Textarea
            rows={2}
            value={query}
            placeholder="例如：退款申请通过后多久能到账？"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); void run(); }
            }}
          />
        </Field>
        <div className="flex flex-wrap items-end gap-2.5">
          <div className="min-w-[160px] flex-1">
            <Field label="重排配置">
              <select className="field" value={rerank} onChange={(event) => setRerank(event.target.value)}>
                <option value="">不使用重排</option>
                {rerankOptions(modelOptions).map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </Field>
          </div>
          <Button variant="primary" tint="purple" loading={loading} disabled={!query.trim() || Boolean(kb.status === 'error')} icon={<ScanSearch className="h-4 w-4" />} onClick={() => void run()}>
            测试检索
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-[11.5px] text-fg-4">
          <span>试试</span>
          {samples.map((sample) => (
            <button key={sample} type="button" onClick={() => setQuery(sample)} className="rounded-full border border-line px-2 py-0.5 text-[11.5px] text-fg-3 transition hover:border-[rgb(var(--rgb-purple)/0.4)] hover:bg-[rgb(var(--rgb-purple)/0.1)] hover:text-purple">
              {sample}
            </button>
          ))}
          <span className="num ml-auto hidden sm:inline">候选 {candidates ?? '—'} · {rerank ? '重排开启' : '仅向量'} · 文档 {docs.length}</span>
        </div>
        {kb.status === 'error' && (
          <p className="flex items-start gap-1.5 rounded-lg border border-[rgb(var(--rgb-red)/0.25)] bg-[rgb(var(--rgb-red)/0.08)] px-2.5 py-2 text-[12px] leading-5 text-red">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-none" />{kb.lastError || '知识库上次操作失败，请先重建索引'}
          </p>
        )}
      </div>

      <div className="scroll min-h-0 flex-1 px-5 py-3">
        {loading ? (
          <div className="space-y-2.5">
            {[0, 1, 2].map((item) => (
              <div key={item} className="rounded-xl border border-line bg-glass p-3.5">
                <div className="flex items-center gap-2"><div className="skeleton h-4 w-4 rounded-md" /><div className="skeleton h-3.5 w-40" /><div className="skeleton ml-auto h-3.5 w-10" /></div>
                <div className="skeleton mt-3 h-3 w-full" /><div className="skeleton mt-1.5 h-3 w-11/12" /><div className="skeleton mt-1.5 h-3 w-3/5" />
              </div>
            ))}
          </div>
        ) : error ? (
          <div className="flex flex-col items-center py-10 text-center">
            <div className="tint-red mb-3 grid h-12 w-12 place-items-center rounded-2xl border border-[rgb(var(--rgb-red)/0.25)] bg-[rgb(var(--rgb-red)/0.12)] text-red"><AlertTriangle className="h-6 w-6" /></div>
            <p className="text-sm font-medium text-fg">检索失败</p>
            <p className="mt-1 max-w-sm text-[12.5px] leading-5 text-fg-3">{error}</p>
          </div>
        ) : !result ? (
          <EmptyState
            tint="purple"
            icon={<ScanSearch className="h-6 w-6" />}
            title="测试召回效果"
            description="输入一个用户可能提出的问题，查看排序后的片段、相似度与重排分数。结果不会写入会话或日志。"
            className="py-8"
          />
        ) : hits.length === 0 ? (
          <EmptyState
            tint="purple"
            icon={<Search className="h-6 w-6" />}
            title="没有召回片段"
            description="知识库中还没有可检索的索引，或问题与现有内容差异过大。尝试更换问法或导入更多文档。"
            className="py-8"
          />
        ) : (
          <>
            <div className="mb-2.5 flex flex-wrap items-center gap-2">
              <span className="text-[13px] font-medium text-fg">找到 {hits.length} 个片段</span>
              <span className="num text-[11.5px] text-fg-4">{elapsed === null ? '耗时未知' : `耗时 ${elapsed}ms`}{candidates === null ? ' · 候选未知' : ` · 候选 ${candidates}`} · 问题「{searchedFor}」</span>
              <Badge tone={resultRerank ? 'purple' : 'neutral'}>{resultRerank ? '已重排' : '向量检索'}</Badge>
              <button type="button" onClick={copy} className="ml-auto flex h-7 items-center gap-1.5 rounded-lg border border-line px-2 text-[11.5px] text-fg-3 transition hover:text-fg">
                <Copy className="h-3.5 w-3.5" />复制结果
              </button>
            </div>

            {warnings.length > 0 && (
              <div className="tint-orange mb-2.5 space-y-1 rounded-xl border border-[rgb(var(--rgb-orange)/0.25)] bg-[rgb(var(--rgb-orange)/0.08)] px-3 py-2">
                {warnings.map((warning) => (
                  <p key={warning} className="flex items-start gap-1.5 text-[11.5px] leading-5 text-fg-2">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-tint" />{warning}
                  </p>
                ))}
              </div>
            )}

            <div className="space-y-2.5">
              {hits.map((item, index) => {
                const score = scoreOf(item);
                const source = String(item.source ?? (Array.isArray(item.sources) && item.sources[0] && typeof item.sources[0] === 'object' ? (item.sources[0] as Record<string, unknown>).source ?? '' : ''));
                const body = String(item.text ?? item.content ?? '');
                return (
                  <div key={`${source}-${index}`} className="animate-rise rounded-xl border border-line bg-glass p-3.5 transition-colors hover:border-[rgb(var(--rgb-purple)/0.28)]" style={{ animationDelay: `${index * 45}ms` }}>
                    <div className="flex items-start gap-2.5">
                      <span className="tint-purple grid h-7 w-7 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] font-mono text-[11px] font-semibold text-purple">#{index + 1}</span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="min-w-0 truncate text-[12.5px] font-medium text-fg" title={source}>{source || '未知来源'}</span>
                          <span className="chip !h-[18px] !px-1.5 !text-[10px]">{kb.name}</span>
                        </div>
                        <p className="mt-2 line-clamp-6 whitespace-pre-wrap break-words text-[12.5px] leading-[1.75] text-fg-2">{body || '（该片段未返回正文）'}</p>
                        <div className="num mt-2 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10.5px] text-fg-4">
                          <span>相似度 <span className="text-fg-2">{score === null ? '—' : score.toFixed(3)}</span></span>
                          <span className="ml-auto">{kb.scope === 'global' ? '全局库' : '会话库'}</span>
                        </div>
                      </div>
                      <div className="flex w-14 flex-none flex-col items-end">
                        <span className="num text-[17px] font-semibold leading-none text-purple">{score === null ? '—' : score.toFixed(3)}</span>
                        <span className="mt-1 text-[10px] text-fg-4">{resultRerank ? '重排分' : '相似度'}</span>
                      </div>
                    </div>
                    {score !== null && (
                      <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-glass-hover">
                        <div className="h-full rounded-full bg-[linear-gradient(90deg,rgb(var(--rgb-purple)/0.45),rgb(var(--rgb-purple)))] transition-[width] duration-500" style={{ width: `${Math.max(2, score * 100)}%` }} />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            {typeof result?.truncated === 'boolean' && result.truncated && <p className="mt-2 text-[11px] text-fg-4">部分片段因长度限制被截断，实际入库内容完整。</p>}
          </>
        )}

        {result && (
          <div className="mt-3">
            <div className="mb-1 text-[11px] text-fg-4">原始响应</div>
            <pre className="scroll max-h-56 overflow-auto rounded-lg border border-line bg-[rgb(0_0_0/0.2)] p-3 text-[11px] leading-5 text-fg-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)]">{JSON.stringify(result, null, 2)}</pre>
          </div>
        )}
      </div>
    </div>
  );
}

interface Props {
  kb: Kb;
  docs: Doc[];
  context: RagContext;
  capabilities: UploadCapabilities;
  modelOptions?: ModelOptions;
  onClose: () => void;
  onImport: () => void;
  onEdit: () => void;
  onRebuild: () => void;
  onDelete: () => void;
  onDeleteDocument: (doc: Doc) => void;
  /** 详情内的新操作开始时清除页级提示, 避免陈旧状态消息一直挂为 live region */
  onSearch?: () => void;
}

export function DetailDrawer({ kb, docs, context, capabilities: _capabilities, modelOptions, onClose, onImport, onEdit, onRebuild, onDelete, onDeleteDocument, onSearch }: Props) {
  const indexing = kb.status === 'indexing';
  const err = kb.status === 'error';

  const stats: { label: string; value: string; danger?: boolean; purple?: boolean }[] = [
    { label: '文档', value: kb.documentCount.toLocaleString() },
    { label: '片段', value: kb.chunkCount.toLocaleString() },
    { label: '索引状态', value: err ? '需要重建' : indexing ? (kb.progress === null ? `构建中 · ${jobPhaseLabel(kb.phase)}` : `构建中 ${Math.round(kb.progress)}%`) : kb.documentCount ? '可用' : '空库', danger: err, purple: indexing },
    { label: '最近索引', value: kb.lastIndexedAt ? formatRelativeTime(kb.lastIndexedAt) : '—' },
  ];

  return (
    <Drawer open onClose={onClose} label={`${kb.name} 详情`} width={1000}>
      <div className="tint-purple relative overflow-hidden rounded-t-[22px] px-5 pb-4 pt-5">
        <div aria-hidden className="pointer-events-none absolute -right-10 -top-20 h-56 w-56 rounded-full bg-[radial-gradient(circle,rgb(var(--rgb-purple)/0.32),transparent_65%)] blur-xl" />
        <div className="relative flex items-start gap-3">
          <div className="relative grid h-11 w-11 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">
            <Database className="h-5 w-5" />
            {indexing && <span aria-hidden className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-[var(--bg-base)] bg-tint animate-pulse" />}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-semibold text-fg">{kb.name}</h2>
              {kb.isDefault && <Badge tone="purple"><Star className="h-3 w-3 fill-current" />会话默认</Badge>}
              {err
                ? <Badge tone="red" dot>上次失败</Badge>
                : indexing
                  ? <Badge tone="purple" dot live>索引中{kb.progress === null ? '' : ` ${Math.round(kb.progress)}%`}</Badge>
                  : kb.documentCount
                    ? <Badge tone="green" dot>已就绪</Badge>
                    : <Badge tone="neutral" dot>空知识库</Badge>}
            </div>
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px] text-fg-3">
              <span>{scopeLabel(kb)}</span>
              <span aria-hidden className="text-fg-4">·</span>
              <span className="font-mono">v{kb.revision}</span>
              <span aria-hidden className="text-fg-4">·</span>
              <span className="font-mono">{kb.config.embed || '未配置 Embedding'}</span>
            </p>
          </div>
          <div className="flex flex-none items-center gap-1" onKeyDown={(event) => event.stopPropagation()}>
            <IconButton label="关闭详情" shortcut="Esc" size="sm" onClick={onClose}><X className="h-4 w-4" /></IconButton>
          </div>
        </div>

        <div className="relative mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {stats.map((stat) => (
            <div key={stat.label} className="rounded-xl border border-line bg-glass px-3 py-2">
              <div className="text-[10.5px] text-fg-4">{stat.label}</div>
              <div className={cn('num mt-0.5 truncate text-[13.5px] font-semibold', stat.danger ? 'text-red' : stat.purple ? 'text-purple' : 'text-fg')}>{stat.value}</div>
            </div>
          ))}
        </div>

        <div className="relative mt-3 flex flex-wrap items-center gap-2" onKeyDown={(event) => event.stopPropagation()}>
          <Button size="sm" variant="primary" tint="purple" icon={<Upload className="h-3.5 w-3.5" />} onClick={onImport}>上传文档</Button>
          <Button size="sm" variant="tinted" icon={<Settings2 className="h-3.5 w-3.5" />} onClick={onEdit}>编辑配置</Button>
          <Button size="sm" variant="ghost" icon={<RotateCw className="h-3.5 w-3.5" />} onClick={onRebuild} disabled={kb.documentCount === 0 || indexing}>重建索引</Button>
          <Button size="sm" variant="ghost" className="ml-auto text-red hover:text-red" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={onDelete}>删除</Button>
        </div>

        {err && (
          <div className="relative mt-3 flex items-start gap-2 rounded-xl border border-[rgb(var(--rgb-red)/0.26)] bg-[rgb(var(--rgb-red)/0.08)] px-3 py-2 text-[12px] leading-5 text-red">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-none" />{kb.lastError || '上次操作失败'}
          </div>
        )}
        {indexing && (
          <div className="relative mt-3 h-1.5 overflow-hidden rounded-full bg-glass-hover">
            {kb.progress === null
              ? <div className="h-full w-1/3 rounded-full bg-[linear-gradient(90deg,rgb(var(--rgb-purple)/0.2),rgb(var(--rgb-purple)),rgb(var(--rgb-purple)/0.2))] animate-pulse" />
              : <div className="h-full rounded-full bg-[linear-gradient(90deg,rgb(var(--rgb-purple)/0.55),rgb(var(--rgb-purple)))] transition-[width] duration-700" style={{ width: `${kb.progress}%` }} />}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-line px-5 py-2.5">
        <div className="flex items-center gap-2 text-[11.5px] text-fg-3">
          <StatusDot tone={err ? 'red' : indexing ? 'purple' : 'green'} live={indexing} />
          {err ? '配置与索引不一致' : indexing ? '正在写入新版本索引' : '索引可用'}
        </div>
        <div className="ml-auto hidden items-center gap-3 text-[11px] text-fg-4 sm:flex">
          <span className="num">分块 {kb.config.chunk_size} / {kb.config.chunk_overlap} · 批 {kb.config.batch_size}</span>
          <span>{kb.config.duplicate_policy === 'skip' ? '重复来源跳过' : '重复来源替换'}</span>
        </div>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)]">
        <DocumentsPane kb={kb} docs={docs} context={context} onDeleteDocument={onDeleteDocument} onImport={onImport} />
        <RetrievalPane kb={kb} docs={docs} context={context} modelOptions={modelOptions} onSearch={onSearch} />
      </div>

      <div className="flex items-center justify-between gap-2 border-t border-line px-5 py-2 text-[10.5px] text-fg-4">
        <span>知识库 ID <span className="font-mono">{kb.id}</span></span>
        <button
          type="button"
          onClick={() => { void navigator.clipboard?.writeText(kb.id); toast.success('已复制知识库 ID'); }}
          className="flex items-center gap-1 hover:text-fg"
        >
          <Copy className="h-3 w-3" />复制 ID
        </button>
      </div>
    </Drawer>
  );
}
