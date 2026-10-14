import { controlClient } from './control';
import { apiClient } from './client';
import type { PlatformArchiveRecord } from './types';

export type ReminderState = 'scheduled' | 'waiting_delivery' | 'paused' | 'sending' | 'sent' | 'partial' | 'failed' | 'unknown' | 'missed' | 'cancelled';
export interface GroupReminder {
  reminder_id: string;
  revision: number;
  state: ReminderState;
  creator_user_id: string;
  creator_kind: 'model' | 'operator';
  text: string;
  mention_user_ids: string[];
  due_at: string;
  due_at_utc: string;
  source_message_id: string;
  source_status?: 'available' | 'unavailable' | 'operator';
  reason: string;
  created_at: number;
  paused_at: number | null;
  settled_at: number | null;
  delivery: { status: string; message_ids: string[]; reason: string; failed_index: number | null } | null;
}
export interface ReminderPage { items: GroupReminder[]; has_more: boolean; next_cursor: string | null }
export interface ReminderCreate { text: string; due_at?: string; after_seconds?: number; mention_user_ids: string[]; idempotency_key: string }
export interface ReminderResult { ok: boolean; status?: string; reminder: GroupReminder }
const root = (record: PlatformArchiveRecord) => `/api/platforms/${encodeURIComponent(record.platform_id)}/group-chat/reminders`;
const scope = (record: PlatformArchiveRecord) => ({ self_id: record.self_id, conversation_kind: record.conversation_kind, chat_id: record.chat_id });
const livePath = (record: PlatformArchiveRecord, suffix = '') => `${root(record)}${suffix}?${new URLSearchParams(scope(record))}`;

export const reminderApi = {
  list: async (record: PlatformArchiveRecord, options: { state?: ReminderState; cursor?: string } = {}): Promise<ReminderPage> =>
    (await controlClient.get(root(record), { params: { ...scope(record), ...options } })).data,
  get: async (record: PlatformArchiveRecord, id: string): Promise<ReminderResult> =>
    (await controlClient.get(`${root(record)}/${encodeURIComponent(id)}`, { params: scope(record) })).data,
  create: (record: PlatformArchiveRecord, values: ReminderCreate): Promise<ReminderResult> =>
    apiClient.post(livePath(record), values, 30000),
  cancel: async (record: PlatformArchiveRecord, reminder: GroupReminder): Promise<ReminderResult> =>
    (await controlClient.post(`${root(record)}/${encodeURIComponent(reminder.reminder_id)}/cancel`, { expected_revision: reminder.revision }, { params: scope(record) })).data,
  resume: (record: PlatformArchiveRecord, reminder: GroupReminder): Promise<ReminderResult> =>
    apiClient.post(livePath(record, `/${encodeURIComponent(reminder.reminder_id)}/resume`), { expected_revision: reminder.revision }, 30000),
};
