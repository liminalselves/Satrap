import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PlatformArchiveRecord } from './types';
import { reminderApi, type GroupReminder } from './reminders';

const mocked = vi.hoisted(() => ({ coldGet: vi.fn(), coldPost: vi.fn(), livePost: vi.fn() }));
vi.mock('./control', () => ({ controlClient: { get: mocked.coldGet, post: mocked.coldPost } }));
vi.mock('./client', () => ({ apiClient: { post: mocked.livePost } }));
const record = { platform_id: 'future/platform', self_id: 'bot:/a', conversation_kind: 'group', chat_id: '群/中文' } as PlatformArchiveRecord;

describe('reminder management API', () => {
  beforeEach(() => { vi.clearAllMocks(); mocked.coldGet.mockResolvedValue({ data: { items: [] } }); mocked.coldPost.mockResolvedValue({ data: { ok: true } }); mocked.livePost.mockResolvedValue({ ok: true }); });
  it('cold listing keeps the full account and conversation scope', async () => {
    await reminderApi.list(record, { state: 'paused' });
    expect(mocked.coldGet).toHaveBeenCalledWith('/api/platforms/future%2Fplatform/group-chat/reminders', { params: {
      self_id: 'bot:/a', chat_id: '群/中文', conversation_kind: 'group', state: 'paused',
    } });
  });
  it('creation uses the running backend and preserves the retry intent', async () => {
    const values = { text: '提醒', after_seconds: 30, mention_user_ids: ['member'], idempotency_key: 'same-intent' };
    await reminderApi.create(record, values);
    const [path, body, timeout] = mocked.livePost.mock.calls[0];
    expect(path.startsWith('/api/platforms/future%2Fplatform/group-chat/reminders?')).toBe(true);
    const query = new URLSearchParams(path.split('?')[1]);
    expect(query.get('self_id')).toBe('bot:/a'); expect(query.get('chat_id')).toBe('群/中文');
    expect(body).toEqual(values); expect(timeout).toBe(30000);
  });
  it('cold cancellation and explicit live resume use the reviewed revision', async () => {
    const reminder = { reminder_id: 'id/中文', revision: 7 } as GroupReminder;
    await reminderApi.cancel(record, reminder);
    expect(mocked.coldPost.mock.calls[0][1]).toEqual({ expected_revision: 7 });
    await reminderApi.resume(record, reminder);
    expect(mocked.livePost.mock.calls[0][0]).toContain('/id%2F%E4%B8%AD%E6%96%87/resume?');
    expect(mocked.livePost.mock.calls[0][1]).toEqual({ expected_revision: 7 });
  });
});
