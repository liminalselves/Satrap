import { apiClient } from './client';
import { controlApi } from './control';

export interface StorageAuditItem {
  item_id: string;
  category:
    | 'orphan_session_directory'
    | 'orphan_database_rows'
    | 'broken_user_reference'
    | 'detached_platform'
    | 'trash_entry'
    | 'invalid_manifest'
    | 'unsafe_entry'
    | string;
  platform_id: string;
  session_id: string;
  path: string;
  size_bytes: number;
  modified_at: number;
  reason: string;
  recommended_action: string;
  auto_safe: boolean;
  details: {
    archive_id?: string;
    tables?: string[];
    manifest?: Record<string, unknown>;
  };
}

export interface StorageAuditResult {
  items: StorageAuditItem[];
  summary: {
    count: number;
    size_bytes: number;
  };
}

export const storageApi = {
  audit: (backendRunning = true) => backendRunning
    ? apiClient.get<StorageAuditResult>('/api/storage/audit')
    : controlApi.auditStorage(),

  cleanup: (itemIds: string[], backendRunning = true) => backendRunning
    ? apiClient.post<{
    ok: boolean;
    results: Array<{ item_id: string; ok: boolean; error?: string }>;
  }>('/api/storage/cleanup', { item_ids: itemIds })
    : controlApi.cleanupStorage(itemIds),

  restore: (platformId: string, archiveId: string, backendRunning = true) => backendRunning
    ? apiClient.post<{
    ok: boolean;
    platform_id: string;
    session_id: string;
    archive_id: string;
  }>('/api/storage/trash/restore', {
    platform_id: platformId,
    archive_id: archiveId,
  })
    : controlApi.restoreStorageArchive(platformId, archiveId),

  purge: (platformId: string, archiveId: string, backendRunning = true) => backendRunning
    ? apiClient.post<{ ok: boolean }>(
    '/api/storage/trash/purge',
    {
      platform_id: platformId,
      archive_id: archiveId,
    },
    )
    : controlApi.purgeStorageArchive(platformId, archiveId),

  purgeBatch: (data: {
    archive_refs?: Array<{ platform_id: string; archive_id: string }>;
    older_than_days?: number;
    platform_id?: string;
  }, backendRunning = true) => backendRunning
    ? apiClient.post<{
      ok: boolean;
      results: Array<{ platform_id: string; archive_id: string; ok: boolean; error?: string }>;
    }>('/api/storage/trash/purge-batch', data)
    : controlApi.purgeStorageArchives(data),
};
