import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RagJob, RagJobDocument, RagScope } from '@/api/contracts';
import { readDocuments, sleep, waitForJob } from './jobs';

const api = vi.hoisted(() => ({
  documents: vi.fn(), jobs: vi.fn(), job: vi.fn(), submit: vi.fn(), upload: vi.fn(), chunks: vi.fn(),
}));
vi.mock('@/api/contracts', () => ({ ragJobsApi: api }));

const scope: RagScope = { platform_id: 'chat', session_id: 's', kb_id: 'k' };
const document = (id: string): RagJobDocument => ({
  document_id: id, source_id: id, name: id, size_bytes: 1, created_at: 1, updated_at: 1,
  last_indexed_at: 1, status: 'ready', error: null, job_id: null, chunk_count: 1,
});
const runningJob: RagJob = {
  job_id: 'j', operation_id: 'o', owner_service: 'chat', owner_runtime_id: 'r', platform_id: 'chat',
  session_id: 's', kb_id: 'k', action: 'ingest', status: 'running', phase: 'embedding',
  progress: { unit: 'chunks', done: 0, total: 1, percent: 0 }, created_at: 1, started_at: 1,
  finished_at: null, error: null, result: null,
};

beforeEach(() => vi.clearAllMocks());

describe('RAG 任务读取', () => {
  it('消费全部文档分页并保留顺序', async () => {
    api.documents
      .mockResolvedValueOnce({ items: [document('a')], has_more: true, next_cursor: 'next' })
      .mockResolvedValueOnce({ items: [document('b')], has_more: false, next_cursor: null });
    await expect(readDocuments(scope, 'control')).resolves.toMatchObject([{ document_id: 'a' }, { document_id: 'b' }]);
    expect(api.documents).toHaveBeenNthCalledWith(1, scope, { limit: 100, cursor: undefined }, 'control', undefined);
    expect(api.documents).toHaveBeenNthCalledWith(2, scope, { limit: 100, cursor: 'next' }, 'control', undefined);
  });

  it('重复分页游标明确失败而不返回截断目录', async () => {
    api.documents.mockResolvedValue({ items: [], has_more: true, next_cursor: 'same' });
    await expect(readDocuments(scope)).rejects.toThrow('分页游标重复');
    expect(api.documents).toHaveBeenCalledTimes(2);
  });

  it('中止等待后不再轮询或回调', async () => {
    const controller = new AbortController();
    controller.abort();
    const update = vi.fn();
    await expect(waitForJob(scope, runningJob, undefined, update, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    expect(update).not.toHaveBeenCalled();
    expect(api.job).not.toHaveBeenCalled();
  });

  it('sleep 可被中止且清理定时器', async () => {
    const controller = new AbortController();
    const pending = sleep(60_000, controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });
});
