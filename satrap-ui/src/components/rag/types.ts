import type { KnowledgeBase, RagDocument } from '@/api/rag';
import { ragPhaseLabels, type RagJob, type RagJobDocument } from '@/api/contracts';
import type { ModelOptions } from '@/components/common/PluginConfigFields';

export type KBScope = 'global' | 'session';
export type KBStatus = 'ready' | 'indexing' | 'error';
export type DocStatus = 'indexed' | 'indexing' | 'pending' | 'failed';
export type DuplicatePolicy = 'skip' | 'replace';

export interface KBConfig {
  embed: string;
  chunk_size: number;
  chunk_overlap: number;
  batch_size: number;
  duplicate_policy: DuplicatePolicy;
  [field: string]: unknown;
}

/** 同步知识库接口 + 任务进度后的页面模型 */
export interface Kb {
  id: string;
  name: string;
  scope: KBScope;
  platform: string;
  sessionId: string;
  config: KBConfig;
  revision: number;
  isDefault: boolean;
  status: KBStatus;
  lastError: string;
  documentCount: number;
  chunkCount: number;
  lastIndexedAt: number | null;
  createdAt: number | null;
  /** 后台任务进度; 总量未知时保持 null, 不伪报 0 */
  progress: number | null;
  phase: string | null;
  activeJobId: string | null;
}

export interface Doc {
  /** 任务目录里的稳定 document_id; 同步接口回退时为空 */
  documentId: string;
  sourceId: string;
  source: string;
  chunkCount: number;
  contentHash: string;
  status: DocStatus;
  updatedAt: number | null;
  lastIndexedAt: number | null;
  sizeBytes: number | null;
  error: string;
}

export interface ImportSummaryItem {
  source: string;
  status: 'indexed' | 'skipped' | 'failed';
  chunks: number;
  error?: string;
  replaced?: boolean;
}

export interface ImportSummary {
  indexed: number;
  skipped: number;
  failed: number;
  chunks: number;
  items: ImportSummaryItem[];
}

export interface KBDraft {
  name: string;
  scope: KBScope;
  config: KBConfig;
  isDefault: boolean;
}

export interface ConfigContext {
  platformId: string;
  sessionId?: string;
  label: string;
}

export const emptyCapabilities = {
  extensions: [] as string[],
  max_file_bytes: 32 * 1024 * 1024,
  max_text_chars: 1000000,
  missing_parsers: {} as Record<string, string>,
  text_encoding: 'UTF-8',
};

export const defaultConfig: KBConfig = {
  embed: '',
  chunk_size: 800,
  chunk_overlap: 120,
  batch_size: 32,
  duplicate_policy: 'skip',
};

const numberOr = (value: unknown, fallback: number) => {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

export function toConfig(raw: Record<string, unknown> | undefined): KBConfig {
  return {
    ...raw,
    embed: typeof raw?.embed === 'string' ? raw.embed : '',
    chunk_size: numberOr(raw?.chunk_size, defaultConfig.chunk_size),
    chunk_overlap: numberOr(raw?.chunk_overlap, defaultConfig.chunk_overlap),
    batch_size: numberOr(raw?.batch_size, defaultConfig.batch_size),
    duplicate_policy: raw?.duplicate_policy === 'replace' ? 'replace' : 'skip',
  };
}

function jobStatus(job: RagJob | undefined): KBStatus {
  if (!job) return 'ready';
  if (job.status === 'queued' || job.status === 'running') return 'indexing';
  if (job.status === 'failed' || job.status === 'interrupted') return 'error';
  return 'ready';
}

export function toKb(item: KnowledgeBase, job?: RagJob): Kb {
  const derived = jobStatus(job);
  const baseError = typeof item.last_error === 'string' ? item.last_error : '';
  const jobError = job?.status === 'failed' && job.error?.message ? job.error.message : '';
  return {
    id: item.id,
    name: item.name,
    scope: item.scope,
    // 同步接口不返回平台; 会话库以 session_id 定位, 页面范围已知
    platform: '',
    sessionId: item.session_id || '',
    config: toConfig(item.config),
    revision: item.revision,
    isDefault: Boolean(item.is_default),
    status: derived === 'error' || item.status === 'error' ? 'error' : derived,
    lastError: jobError || baseError,
    documentCount: numberOr(item.document_count, 0),
    chunkCount: numberOr(item.chunk_count, 0),
    lastIndexedAt: job?.finished_at ?? null,
    createdAt: null,
    progress: derived === 'indexing' ? job?.progress?.percent ?? null : null,
    phase: derived === 'indexing' ? job?.phase ?? null : null,
    activeJobId: derived === 'indexing' ? job?.job_id ?? null : null,
  };
}

const docStatus: Record<RagJobDocument['status'], DocStatus> = {
  queued: 'pending',
  indexing: 'indexing',
  ready: 'indexed',
  failed: 'failed',
};

export function toDoc(item: RagJobDocument): Doc {
  return {
    documentId: item.document_id,
    sourceId: item.source_id || item.document_id,
    source: item.name || item.source_id || item.document_id,
    chunkCount: numberOr(item.chunk_count, 0),
    contentHash: '',
    status: docStatus[item.status] ?? 'pending',
    updatedAt: item.updated_at ?? null,
    lastIndexedAt: item.last_indexed_at,
    sizeBytes: item.size_bytes,
    error: item.error?.message || '',
  };
}

export function toLegacyDoc(item: RagDocument): Doc {
  return {
    documentId: '',
    sourceId: item.source_id,
    source: item.source,
    chunkCount: numberOr(item.chunk_count, 0),
    contentHash: item.content_hash || '',
    status: 'indexed',
    updatedAt: null,
    lastIndexedAt: null,
    sizeBytes: null,
    error: '',
  };
}

export function scopeLabel(kb: Kb): string {
  if (kb.scope === 'global') return '全局库 · 所有会话可见';
  const owner = [kb.platform, kb.sessionId].filter(Boolean).join(' / ') || '未知会话';
  return `会话库 · ${owner}`;
}

/** 后台任务阶段的中文文案; 未知阶段原样展示, 不伪造成完成 */
export function jobPhaseLabel(phase: string | null | undefined): string {
  if (!phase) return '排队中';
  return (ragPhaseLabels as Record<string, string>)[phase] ?? phase;
}

export function docTint(source: string): string {
  const ext = source.slice(source.lastIndexOf('.')).toLowerCase();
  if (ext === '.pdf') return 'red';
  if (ext === '.docx') return 'accent';
  if (ext === '.xlsx' || ext === '.csv') return 'green';
  if (ext === '.md' || ext === '.txt') return 'teal';
  if (ext === '.json' || ext === '.py') return 'orange';
  return 'purple';
}

export function estimateChunks(bytes: number, chunkSize: number): number {
  return Math.max(1, Math.round(bytes / Math.max(1, chunkSize || 900)));
}

export function embedOptions(modelOptions: ModelOptions | undefined, current = '') {
  const options = (modelOptions?.embed || []).map((option) => ({ value: option.value, label: option.label, description: (option as { model?: string }).model }));
  if (current && !options.some((option) => option.value === current)) options.unshift({ value: current, label: current, description: undefined });
  return options;
}

export function rerankOptions(modelOptions: ModelOptions | undefined) {
  return (modelOptions?.rerank || []).map((option) => ({ value: option.value, label: option.label, description: (option as { model?: string }).model }));
}

export function newOperationId(): string {
  const raw = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return raw.replace(/[^A-Za-z0-9_-]/g, '').slice(0, 128);
}
