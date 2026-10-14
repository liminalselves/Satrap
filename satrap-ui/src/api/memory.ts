import { controlClient } from './control';
import type { PlatformArchiveRecord } from './types';

export type MemoryKind = 'group_rule' | 'member_preference';
export interface ScopedMemory {
  memory_id: string; kind: MemoryKind; owner_user_id: string; key: string; title: string; content: string;
  revision: number; state: string; origin: string; source_message_ids: string[]; source_status: string; updated_at: string;
}
export interface MemoryProposal {
  proposal_id: string; operation: string; memory_id: string | null; base_revision: number;
  actor_id: string; state: string; created_at: number; expires_at: number;
  proposed: Partial<Pick<ScopedMemory, 'title' | 'content' | 'key' | 'source_message_ids'>>;
}
export interface MemoryPage { ok: boolean; items: ScopedMemory[]; has_more: boolean; next_cursor: string | null }
export interface MemoryWrite {
  kind?: MemoryKind; owner_user_id?: string; key?: string; title?: string; content?: string;
  source_message_ids?: string[]; expected_revision?: number; idempotency_key: string;
}
const root = (record: PlatformArchiveRecord) => `/api/platforms/${encodeURIComponent(record.platform_id)}/memory`;
const scope = (record: PlatformArchiveRecord) => ({ self_id: record.self_id, chat_id: record.chat_id, conversation_kind: record.conversation_kind });
export const memoryApi = {
  list: async (record: PlatformArchiveRecord, options: { kind?: MemoryKind; user_id?: string; keyword?: string; cursor?: string } = {}): Promise<MemoryPage> =>
    (await controlClient.get(`${root(record)}/memories`, { params: { ...scope(record), ...options } })).data,
  get: async (record: PlatformArchiveRecord, id: string): Promise<{ memory: ScopedMemory }> =>
    (await controlClient.get(`${root(record)}/memories/${encodeURIComponent(id)}`, { params: scope(record) })).data,
  create: async (record: PlatformArchiveRecord, values: MemoryWrite): Promise<{ status: string }> =>
    (await controlClient.post(`${root(record)}/memories`, values, { params: scope(record) })).data,
  update: async (record: PlatformArchiveRecord, id: string, values: MemoryWrite): Promise<{ status: string }> =>
    (await controlClient.patch(`${root(record)}/memories/${encodeURIComponent(id)}`, values, { params: scope(record) })).data,
  remove: async (record: PlatformArchiveRecord, memory: ScopedMemory, key: string): Promise<{ status: string }> =>
    (await controlClient.delete(`${root(record)}/memories/${encodeURIComponent(memory.memory_id)}`, { params: scope(record), data: { expected_revision: memory.revision, idempotency_key: key } })).data,
  proposals: async (record: PlatformArchiveRecord): Promise<{ items: MemoryProposal[] }> =>
    (await controlClient.get(`${root(record)}/proposals`, { params: scope(record) })).data,
  decide: async (record: PlatformArchiveRecord, proposal: MemoryProposal, approve: boolean): Promise<{ status: string }> =>
    (await controlClient.post(`${root(record)}/proposals/${encodeURIComponent(proposal.proposal_id)}/decision`, { approve, expected_revision: proposal.base_revision }, { params: scope(record) })).data,
};
