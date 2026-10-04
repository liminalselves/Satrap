import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PlatformArchiveRecord } from './types';
import { memoryApi, type ScopedMemory, type MemoryProposal } from './memory';

const mocked = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock('./control', () => ({ controlClient: mocked }));
const record = { platform_id: 'future/platform', self_id: 'bot:/a', conversation_kind: 'group', chat_id: '群/中文' } as PlatformArchiveRecord;

describe('memory management API', () => {
  beforeEach(() => { vi.clearAllMocks(); Object.values(mocked).forEach((method) => method.mockResolvedValue({ data: { ok: true, status: 'saved', items: [] } })); });
  it('keeps the complete account and conversation scope in queries', async () => {
    await memoryApi.list(record, { kind: 'member_preference', user_id: 'member', keyword: '称呼' });
    expect(mocked.get).toHaveBeenCalledWith('/api/platforms/future%2Fplatform/memory/memories', { params: {
      self_id: 'bot:/a', conversation_kind: 'group', chat_id: '群/中文', kind: 'member_preference', user_id: 'member', keyword: '称呼',
    } });
  });
  it('sends the displayed revision and stable intent when deleting', async () => {
    const memory = { memory_id: 'id/中文', revision: 3 } as ScopedMemory;
    await memoryApi.remove(record, memory, 'same-intent');
    expect(mocked.delete).toHaveBeenCalledWith('/api/platforms/future%2Fplatform/memory/memories/id%2F%E4%B8%AD%E6%96%87', {
      params: { self_id: 'bot:/a', conversation_kind: 'group', chat_id: '群/中文' }, data: { expected_revision: 3, idempotency_key: 'same-intent' },
    });
  });
  it('approval uses the reviewed proposal base revision, including creation revision zero', async () => {
    const proposal = { proposal_id: 'proposal', base_revision: 0 } as MemoryProposal;
    await memoryApi.decide(record, proposal, true);
    expect(mocked.post).toHaveBeenCalledWith('/api/platforms/future%2Fplatform/memory/proposals/proposal/decision',
      { approve: true, expected_revision: 0 }, { params: { self_id: 'bot:/a', conversation_kind: 'group', chat_id: '群/中文' } });
  });
});
