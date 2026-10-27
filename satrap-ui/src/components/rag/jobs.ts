import { ApiError } from '@/api/client';
import { ragJobsApi, type Page, type RagJob, type RagJobDocument, type RagScope } from '@/api/contracts';

export { newOperationId } from './types';

export const sleep = (ms: number) => new Promise<void>((resolve) => { window.setTimeout(resolve, ms); });

export function isJobPayload(value: unknown): value is { job: RagJob } {
  if (!value || typeof value !== 'object') return false;
  const job = (value as { job?: unknown }).job;
  return Boolean(job && typeof job === 'object' && typeof (job as { job_id?: unknown }).job_id === 'string');
}

export function isRagPage<T>(value: unknown): value is Page<T> {
  if (!value || typeof value !== 'object') return false;
  return Array.isArray((value as { items?: unknown }).items);
}

/** 老部署没有 jobs 路由时回退同步接口, 队列满/修订冲突等业务错误不回退 */
export function jobsUnavailable(error: unknown): boolean {
  if (error instanceof ApiError) {
    if (error.status === 404 || error.status === 405 || error.status === 501) return true;
    if (error.code === 'not_found' || error.code === 'rag_jobs_unavailable') return true;
  }
  return false;
}

function isClientRejection(error: unknown): boolean {
  return error instanceof ApiError && Boolean(error.status && error.status >= 400 && error.status < 500 && error.status !== 404);
}

/** 上传回执丢失后按 operation_id 精确找回原任务 */
export async function findJobByOperation(scope: RagScope, operationId: string, via?: 'control'): Promise<RagJob | null> {
  try {
    const page = await ragJobsApi.jobs(scope, { operation_id: operationId, limit: 1 }, via);
    if (!isRagPage<RagJob>(page)) return null;
    return page.items.find((job) => job.operation_id === operationId) ?? null;
  } catch {
    return null;
  }
}

/** 上传/提交失败时先确认是否已登记, 避免重复入库 */
export async function recoverJob(scope: RagScope, operationId: string, error: unknown, via?: 'control'): Promise<RagJob | null> {
  if (jobsUnavailable(error)) return null;
  if (isClientRejection(error)) return null;
  return findJobByOperation(scope, operationId, via);
}

export async function waitForJob(
  scope: RagScope,
  initial: RagJob,
  via: 'control' | undefined,
  onUpdate: (job: RagJob) => void,
): Promise<RagJob> {
  let current = initial;
  onUpdate(current);
  for (let attempt = 0; attempt < 900; attempt += 1) {
    if (current.status === 'succeeded' || current.status === 'failed' || current.status === 'interrupted') return current;
    await sleep(attempt < 5 ? 350 : 900);
    const response = await ragJobsApi.job(scope, current.job_id, via);
    if (!isJobPayload(response)) throw new Error('任务进度响应无效, 请刷新后确认结果');
    current = response.job;
    onUpdate(current);
  }
  throw new Error('任务仍在后台执行, 请稍后刷新知识库确认结果');
}

export async function readDocuments(scope: RagScope, via?: 'control'): Promise<RagJobDocument[] | null> {
  try {
    const page = await ragJobsApi.documents(scope, { limit: 100 }, via);
    if (!isRagPage<RagJobDocument>(page)) return null;
    return page.items;
  } catch (error) {
    if (jobsUnavailable(error)) return null;
    throw error;
  }
}

export async function readLatestJob(scope: RagScope, via?: 'control'): Promise<RagJob | null> {
  try {
    const page = await ragJobsApi.jobs(scope, { limit: 20 }, via);
    if (!isRagPage<RagJob>(page)) return null;
    const active = page.items.find((job) => job.status === 'queued' || job.status === 'running');
    return active ?? page.items[0] ?? null;
  } catch {
    return null;
  }
}
