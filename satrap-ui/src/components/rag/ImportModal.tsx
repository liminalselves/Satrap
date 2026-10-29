import { useEffect, useRef, useState, type DragEvent } from 'react';
import { AlertCircle, CheckCircle2, FileText, Loader2, Plus, Sparkles, UploadCloud, X } from 'lucide-react';
import { cn } from '@/utils/cn';
import { ApiError } from '@/api/client';
import { ragApi, ragErrorMessage, type RagContext, type UploadCapabilities } from '@/api/rag';
import { ragJobsApi, ragPhaseLabels, type RagJob } from '@/api/contracts';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { Field, Input, Textarea } from '@/components/kit/Field';
import { Modal } from '@/components/kit/Modal';
import { formatBytes } from '@/utils/format';
import { isJobPayload, jobsUnavailable, newOperationId, recoverJob, waitForJob } from './jobs';
import { docTint, estimateChunks, type ImportSummary, type ImportSummaryItem as SummaryItem, type Kb } from './types';

type QueueStatus = 'pending' | 'invalid' | 'uploading' | 'indexed' | 'skipped' | 'failed';

interface QueueItem {
  id: number;
  file: File;
  name: string;
  size: number;
  source: string;
  status: QueueStatus;
  progress: number;
  phase: string;
  chunks: number;
  error: string;
}

interface Props {
  context: RagContext;
  target: Kb;
  capabilities: UploadCapabilities;
  existingSources?: string[];
  onClose: () => void;
  onComplete: (summary: ImportSummary) => void;
}

const statusTone: Record<QueueStatus, 'neutral' | 'purple' | 'green' | 'orange' | 'red'> = {
  pending: 'neutral', invalid: 'red', uploading: 'purple', indexed: 'green', skipped: 'orange', failed: 'red',
};

function statusText(item: QueueItem): string {
  if (item.status === 'pending') return '待导入';
  if (item.status === 'invalid') return '文件不可导入';
  if (item.status === 'uploading') {
    if (item.progress < 100) return `上传中 ${item.progress}%`;
    return item.phase ? `${item.phase}…` : '解析文档并构建索引…';
  }
  if (item.status === 'indexed') return `已导入 · ${item.chunks} 块`;
  if (item.status === 'skipped') return '已跳过重复来源';
  return '导入失败';
}

function summarize(items: QueueItem[]): ImportSummary {
  let indexed = 0, skipped = 0, failed = 0, chunks = 0;
  const results: SummaryItem[] = [];
  for (const item of items) {
    if (item.status === 'indexed') { indexed += 1; chunks += item.chunks; results.push({ source: item.source, status: 'indexed', chunks: item.chunks }); }
    else if (item.status === 'skipped') { skipped += 1; results.push({ source: item.source, status: 'skipped', chunks: 0 }); }
    else if (item.status === 'failed') { failed += 1; results.push({ source: item.source, status: 'failed', chunks: 0, error: item.error }); }
  }
  return { indexed, skipped, failed, chunks, items: results };
}

function jobFailure(job: RagJob): string {
  if (job.status === 'interrupted') return '任务被服务重启中断，可重新导入';
  return job.error?.message || '导入失败，请检查文件后重试';
}

export function ImportModal({ context, target, capabilities, existingSources = [], onClose, onComplete }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const nextId = useRef(0);
  const running = useRef(false);
  const itemsRef = useRef<QueueItem[]>([]);
  const [mode, setMode] = useState<'file' | 'text'>('file');
  const [items, setItems] = useState<QueueItem[]>([]);
  const [source, setSource] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState('');
  const [done, setDone] = useState<ImportSummary | null>(null);
  const [dragging, setDragging] = useState(false);

  useEffect(() => { itemsRef.current = items; }, [items]);
  useEffect(() => () => { running.current = false; }, []);

  const patch = (id: number, value: Partial<QueueItem>) => {
    itemsRef.current = itemsRef.current.map((item) => item.id === id ? { ...item, ...value } : item);
    setItems(itemsRef.current);
  };

  const maxBytes = capabilities.max_file_bytes || 32 * 1024 * 1024;
  const maxChars = capabilities.max_text_chars || 1000000;
  const extensions = capabilities.extensions || [];
  const missingParsers = capabilities.missing_parsers || {};
  const scope = { platform_id: context.platformId, session_id: context.sessionId || target.sessionId || '', kb_id: target.id };

  const choose = (files: FileList | null) => {
    if (running.current || !files?.length) return;
    const selected = Array.from(files).map((file): QueueItem => {
      const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
      let invalid = '';
      if (extensions.length && !extensions.includes(ext)) invalid = '不支持此文件类型';
      else if (missingParsers[ext]) invalid = `缺少解析依赖 ${missingParsers[ext]}，请在后端安装后重试`;
      else if (!file.size) invalid = '不能上传空文件';
      else if (file.size > maxBytes) invalid = `文件超过 ${maxBytes / 1024 / 1024} MiB`;
      return {
        id: nextId.current++, file, name: file.name, size: file.size, source: file.name,
        status: invalid ? 'invalid' : 'pending', progress: 0, phase: '', chunks: estimateChunks(file.size, target.config.chunk_size), error: invalid,
      };
    });
    itemsRef.current = [...itemsRef.current, ...selected];
    setItems(itemsRef.current);
    setDone(null);
    if (inputRef.current) inputRef.current.value = '';
  };

  const uploadViaJobs = async (file: File, documentSource: string, onJob: (job: RagJob) => void): Promise<{ status: 'indexed' | 'skipped'; chunks: number } | null> => {
    const operationId = newOperationId();
    let job: RagJob | null = null;
    try {
      const response = await ragJobsApi.upload(scope, file, { operation_id: operationId, expected_revision: target.revision, source: documentSource }, context.via);
      if (!isJobPayload(response)) return null;
      job = response.job;
    } catch (reason) {
      if (reason instanceof ApiError && reason.status && reason.status >= 400 && reason.status < 500 && reason.status !== 404) throw reason;
      job = await recoverJob(scope, operationId, reason, context.via);
      if (!job) {
        if (jobsUnavailable(reason) || (reason instanceof ApiError && reason.status === 404)) return null;
        throw reason;
      }
    }
    const finished = await waitForJob(scope, job, context.via, onJob);
    if (finished.status !== 'succeeded') throw new Error(jobFailure(finished));
    const result = (finished.result || {}) as Record<string, unknown>;
    const chunks = Number(result.indexed_chunks ?? result.chunks ?? 0);
    const status = result.status === 'skipped' ? 'skipped' : 'indexed';
    return { status, chunks };
  };

  const runQueue = async (retry = false) => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    setDone(null);
    setError('');
    try {
      const queue = itemsRef.current.filter((item) => item.status === (retry ? 'failed' : 'pending'));
      for (const item of queue) {
        const trimmed = item.source.trim();
        if (!trimmed) {
          patch(item.id, { status: 'failed', error: '来源名称不能为空' });
          continue;
        }
        patch(item.id, { status: 'uploading', progress: 0, phase: '', error: '' });
        try {
          const viaJobs = await uploadViaJobs(item.file, trimmed, (job) => {
            patch(item.id, { progress: job.progress?.percent ?? 100, phase: ragPhaseLabels[job.phase] || '' });
          });
          const result = (viaJobs ?? await ragApi.upload(context, target.id, item.file, trimmed, (percent) => patch(item.id, { progress: percent }))) as { ok?: unknown; status?: unknown; chunks?: unknown; error?: unknown };
          if (result.ok === false) throw new Error(String(result.error || '导入失败'));
          const status = result.status === 'skipped' ? 'skipped' : 'indexed';
          patch(item.id, { status, progress: 100, phase: '', chunks: Number(result.chunks || 0) });
        } catch (reason) {
          patch(item.id, { status: 'failed', phase: '', error: ragErrorMessage(reason) });
        }
      }
      const summary = summarize(itemsRef.current);
      setDone(summary);
      onComplete(summary);
    } finally {
      running.current = false;
      setBusy(false);
    }
  };

  const runText = async () => {
    if (running.current || !text.trim() || !source.trim() || text.length > maxChars) return;
    running.current = true;
    setBusy(true);
    setError('');
    setProgress(0);
    try {
      // 正文按原始文件上传: JSON 任务请求体上限 1 MiB, 中文长文本会在字符上限之前超出
      const file = new File([text], 'pasted-text.txt', { type: 'text/plain' });
      const viaJobs = await uploadViaJobs(file, source.trim(), (job) => {
        setProgress(job.progress?.percent ?? 100);
      });
      const result = (viaJobs ?? await ragApi.upload(context, target.id, file, source.trim(), setProgress)) as { ok?: unknown; status?: unknown; chunks?: unknown; error?: unknown };
      if (result.ok === false) throw new Error(String(result.error || '导入失败'));
      const chunks = Number(result.chunks || 0);
      const summary: ImportSummary = {
        indexed: result.status === 'skipped' ? 0 : 1,
        skipped: result.status === 'skipped' ? 1 : 0,
        failed: 0,
        chunks,
        items: [{ source: source.trim(), status: result.status === 'skipped' ? 'skipped' : 'indexed', chunks }],
      };
      setDone(summary);
      onComplete(summary);
      onClose();
    } catch (reason) {
      setError(ragErrorMessage(reason));
    } finally {
      running.current = false;
      setBusy(false);
    }
  };

  const pending = items.filter((item) => item.status === 'pending').length;
  const failed = items.filter((item) => item.status === 'failed').length;
  const invalid = items.filter((item) => item.status === 'invalid').length;
  const overLimit = text.length > maxChars;
  const canSubmitText = Boolean(text.trim() && source.trim() && !overLimit);

  const drop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    choose(event.dataTransfer.files);
  };

  const primary = () => {
    if (mode === 'text') { void runText(); return; }
    if (!pending) { onClose(); return; }
    void runQueue();
  };

  return (
    <Modal
      open
      onClose={() => { if (!busy) onClose(); }}
      title="导入文档"
      description={`导入到「${target.name}」· ${target.scope === 'global' ? '全局库' : `会话库 ${target.sessionId || ''}`}`}
      icon={<UploadCloud className="h-[18px] w-[18px]" />}
      size="lg"
      className="tint-purple"
      footer={<>
        <Button variant="ghost" disabled={busy} onClick={onClose}>关闭</Button>
        {mode === 'file' && failed > 0 && (
          <Button variant="tinted" disabled={busy} onClick={() => void runQueue(true)}>重试失败文件</Button>
        )}
        <Button
          variant="primary"
          loading={busy}
          disabled={mode === 'file' ? pending === 0 && !done : !canSubmitText}
          onClick={primary}
        >
          导入到此库
        </Button>
      </>}
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-glass px-3.5 py-2.5">
          <Badge tone="purple">{target.scope === 'global' ? '全局库' : '会话库'}</Badge>
          <span className="chip max-w-full">
            <Sparkles className="h-3 w-3 flex-none" aria-hidden />
            <span className="truncate font-mono text-[11px]">{target.config.embed || '未配置 Embedding'}</span>
          </span>
          <span className="text-[11.5px] text-fg-3">分块 {target.config.chunk_size} / 重叠 {target.config.chunk_overlap}</span>
          <span className="ml-auto text-[11.5px] text-fg-4">
            重复来源：{target.config.duplicate_policy === 'replace' ? '替换已有文档' : '跳过导入'}
          </span>
        </div>

        <div className="flex flex-wrap gap-2" role="group" aria-label="导入方式">
          <Button disabled={busy} variant={mode === 'file' ? 'primary' : 'ghost'} onClick={() => { setMode('file'); setError(''); }}>上传文件</Button>
          <Button disabled={busy} variant={mode === 'text' ? 'primary' : 'ghost'} onClick={() => { setMode('text'); setError(''); }}>粘贴文本</Button>
        </div>

        {mode === 'file' ? (
          <>
            <div
              role="group"
              aria-label="文件拖放区域"
              onDragOver={(event) => { event.preventDefault(); if (!busy) setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={drop}
              className={cn(
                'rounded-xl border border-dashed transition-all duration-300',
                items.length ? 'p-3' : 'p-6',
                dragging
                  ? 'border-[rgb(var(--rgb-purple)/0.65)] bg-[rgb(var(--rgb-purple)/0.1)] shadow-[0_0_0_4px_rgb(var(--rgb-purple)/0.12)]'
                  : 'border-[var(--glass-border-strong)] bg-glass',
                busy && 'pointer-events-none opacity-70',
              )}
            >
              {items.length === 0 ? (
                <div className="flex flex-col items-center py-4 text-center">
                  <div className="relative mb-3">
                    <span aria-hidden className="absolute inset-0 -m-4 rounded-full bg-[radial-gradient(circle,rgb(var(--rgb-purple)/0.3),transparent_70%)] blur-md" />
                    <div className="relative grid h-12 w-12 place-items-center rounded-2xl border border-[rgb(var(--rgb-purple)/0.25)] bg-[rgb(var(--rgb-purple)/0.12)] text-purple">
                      <UploadCloud className="h-6 w-6" />
                    </div>
                  </div>
                  <p className="text-sm font-medium text-fg">拖入文件，或点击选择</p>
                  <p className="mt-1 text-[12px] text-fg-3">支持一次导入多个文件，单个失败不影响其余文件</p>
                  <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
                    <Button size="sm" variant="primary" icon={<Plus className="h-3.5 w-3.5" />} onClick={() => inputRef.current?.click()}>选择文件</Button>
                  </div>
                </div>
              ) : (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="flex items-center gap-1.5 text-[12.5px] text-fg-2"><UploadCloud className="h-4 w-4 text-purple" />继续拖入文件</span>
                  <Button size="sm" variant="tinted" onClick={() => inputRef.current?.click()}>选择文件</Button>
                  <span className="num ml-auto text-[11px] text-fg-4">{items.length} 个文件</span>
                </div>
              )}
              <input
                ref={inputRef}
                type="file"
                multiple
                className="sr-only"
                aria-label="导入文件"
                accept={extensions.join(',')}
                disabled={busy}
                onChange={(event) => choose(event.target.files)}
              />
            </div>

            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-fg-3">
              <span>{extensions.join(' / ')}</span>
              <span>单个文件最大 {maxBytes / 1024 / 1024} MiB</span>
              <span>文本编码 {capabilities.text_encoding || 'UTF-8'}；PDF 需包含文字，暂不支持扫描件 OCR</span>
            </div>

            {items.length > 0 && (
              <ul className="scroll max-h-[46vh] space-y-2.5 pr-1">
                {items.map((item) => (
                  <li key={item.id} className={cn('rounded-xl border border-line bg-glass p-3', item.status === 'invalid' && 'border-[rgb(var(--rgb-red)/0.24)]')}>
                    <div className="flex items-start gap-2.5">
                      <span className={cn(
                        'mt-0.5 grid h-8 w-8 flex-none place-items-center rounded-lg',
                        item.status === 'invalid' || item.status === 'failed' ? 'tint-red bg-[rgb(var(--tint)/0.14)] text-tint' : `tint-${docTint(item.name)} bg-[rgb(var(--tint)/0.14)] text-tint`,
                      )}>
                        <FileText className="h-4 w-4" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg" title={item.name}>{item.name}</span>
                          <span className="num flex-none text-[11px] text-fg-4">{formatBytes(item.size)}</span>
                          <button
                            type="button"
                            aria-label={`移除 ${item.name}`}
                            disabled={busy || item.status === 'uploading'}
                            onClick={() => {
                              itemsRef.current = itemsRef.current.filter((entry) => entry.id !== item.id);
                              setItems(itemsRef.current);
                            }}
                            className="grid h-6 w-6 flex-none place-items-center rounded-md text-fg-4 transition hover:bg-glass-hover hover:text-fg disabled:opacity-30"
                          >
                            <X className="h-3.5 w-3.5" />
                          </button>
                        </div>

                        <Input
                          className="mt-2 !h-8 !text-[12px]"
                          aria-label={`来源：${item.name}`}
                          maxLength={1000}
                          value={item.source}
                          disabled={busy || (item.status !== 'pending' && item.status !== 'failed')}
                          placeholder="来源名称（默认使用文件名）"
                          onChange={(event) => patch(item.id, { source: event.target.value })}
                        />

                        {item.status === 'uploading' && (
                          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-glass-hover">
                            <div className="h-full rounded-full bg-[linear-gradient(90deg,rgb(var(--rgb-purple)/0.55),rgb(var(--rgb-purple)))] transition-[width] duration-200" style={{ width: `${Math.max(2, item.progress)}%` }} />
                          </div>
                        )}

                        <div className="mt-1.5 flex flex-wrap items-center gap-2">
                          <Badge tone={statusTone[item.status]} dot live={item.status === 'uploading'}>{statusText(item)}</Badge>
                          <span className="min-w-0 flex-1 truncate text-[11px] text-fg-4">
                            {item.status === 'pending' ? `预计 ${item.chunks} 个片段${existingSources.includes(item.source.trim()) ? ' · 与现有来源同名，按重复策略处理' : ''}` : ''}
                          </span>
                        </div>
                        {(item.status === 'failed' || item.status === 'invalid') && item.error && (
                          <p role="alert" className="mt-1 flex items-start gap-1.5 text-[11.5px] leading-5 text-red">
                            <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-none" aria-hidden />{item.error}
                          </p>
                        )}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}

            {items.length > 0 && (
              <p role="status" className="num flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-line bg-glass px-3 py-2 text-[11.5px] text-fg-3">
                共 {items.length} 个 · 成功 {items.filter((item) => item.status === 'indexed').length} · 跳过 {items.filter((item) => item.status === 'skipped').length} · 失败 {failed} · 不可导入 {invalid} · 待导入 {pending}
              </p>
            )}
          </>
        ) : (
          <div className="space-y-4">
            <Field label="文本内容" hint={`粘贴需要入库的文本，上限 ${maxChars.toLocaleString()} 字符`}>
              <Textarea
                aria-label="导入文本"
                rows={8}
                value={text}
                disabled={busy}
                placeholder="粘贴需要入库的文本…"
                onChange={(event) => setText(event.target.value)}
              />
            </Field>
            <Field label="文档来源" required hint="同名来源按当前重复策略处理">
              <Input
                aria-label="文档来源"
                maxLength={1000}
                value={source}
                disabled={busy}
                placeholder="例如：售后话术补充说明.md"
                onChange={(event) => setSource(event.target.value)}
              />
            </Field>
            <div className="flex items-center gap-3">
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-glass-hover">
                <div className={cn('h-full rounded-full transition-[width] duration-300', overLimit ? 'tint-red bg-tint' : 'tint-purple bg-tint')} style={{ width: `${Math.min(100, (text.length / maxChars) * 100)}%` }} />
              </div>
              <span className={cn('num text-[11px]', overLimit ? 'text-red' : 'text-fg-4')}>{text.length.toLocaleString()} / {maxChars.toLocaleString()}</span>
            </div>
            {overLimit && <p role="alert" className="text-[12px] text-red">文本超过字符上限，请拆分后导入。</p>}
          </div>
        )}

        <p className="text-[11.5px] leading-5 text-fg-4">
          同名来源将{target.config.duplicate_policy === 'replace' ? '替换已有文档的索引' : '跳过导入'}。
          导入会实时叠加到现有索引；索引更新期间旧版本仍可用于检索。
        </p>

        {error && <p role="alert" className="flex items-center gap-1.5 text-[13px] text-red"><AlertCircle className="h-4 w-4" />{error}</p>}

        {busy && mode === 'text' && (
          <div className="flex items-center gap-2 rounded-xl border border-line bg-glass px-3 py-2 text-[12px] text-fg-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin-slow text-purple" />
            {progress < 100 ? `上传中 ${progress}%` : '正在解析文档并构建索引，请稍候…'}
          </div>
        )}

        {done && (
          <div className={cn(
            'flex items-start gap-2 rounded-xl border px-3 py-2.5 text-[12.5px] leading-5',
            done.failed > 0 ? 'border-[rgb(var(--rgb-orange)/0.28)] bg-[rgb(var(--rgb-orange)/0.08)] tint-orange' : 'border-[rgb(var(--rgb-green)/0.28)] bg-[rgb(var(--rgb-green)/0.08)] tint-green',
          )}>
            <CheckCircle2 className="mt-0.5 h-4 w-4 flex-none text-tint" aria-hidden />
            <span className="text-fg-2">
              本次导入：成功 <span className="num font-medium text-fg">{done.indexed}</span>，
              跳过 <span className="num font-medium text-fg">{done.skipped}</span>，
              失败 <span className="num font-medium text-fg">{done.failed}</span>
              {done.chunks > 0 && <>，新增 <span className="num font-medium text-fg">{done.chunks}</span> 个片段</>}。
            </span>
          </div>
        )}
      </div>
    </Modal>
  );
}
