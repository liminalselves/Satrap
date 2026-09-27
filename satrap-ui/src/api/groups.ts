import { isAxiosError } from 'axios';
import { apiClient, toApiError } from './client';
import { controlClient } from './control';

export interface GroupAccount {
  self_id: string;
  mode: 'selected' | 'all';
  revision: number;
  last_bound_at: number;
}

export interface GroupAccountsResult {
  items: GroupAccount[];
  current_account: string;
  waiting_for_account: boolean;
  offline_snapshot?: boolean;
}

export interface GroupRow {
  group_id: string;
  group_name: string | null;
  member_count: number | null;
  max_member_count: number | null;
  membership: 'joined' | 'left' | 'unknown' | 'config_only';
  confirmed_at: number | null;
  response_enabled: boolean;
  response_source: 'group' | 'account' | 'platform';
}

export interface GroupSyncStatus {
  status: 'never' | 'running' | 'complete' | 'partial' | 'failed';
  sync_id: string | null;
  complete: boolean;
  truncated: boolean;
  reason: string | null;
  started_at: number | null;
  completed_at: number | null;
  last_complete_at: number | null;
  connection_generation: number;
}

export interface GroupListResult {
  items: GroupRow[];
  total: number;
  page: number;
  page_size: number;
  counts: { joined: number; response_enabled: number; configured: number };
  account: string;
  current_account: string;
  account_generation: number | null;
  sync: GroupSyncStatus;
  offline_snapshot?: boolean;
}

export interface GroupSettings {
  self_id: string;
  mode: 'selected' | 'all';
  approval_defaults: Record<string, 'approval_required' | 'auto_execute'>;
  approval_actions: Array<{ action_type: string; risk: 'high' | 'normal' }>;
  approval_inheriting_counts: Record<string, number>;
  revision: number;
  migrated_at: number;
  last_bound_at: number;
  legacy_adopted: boolean;
  current: boolean;
  offline_snapshot?: boolean;
  apply_status?: 'applied' | 'pending' | 'failed';
  apply_error?: string;
}

export interface GroupListQuery {
  account: string;
  q?: string;
  membership?: 'joined' | 'left' | 'unknown' | 'config_only' | 'all';
  response?: 'enabled' | 'disabled' | 'all';
  page?: number;
  page_size?: 25 | 50 | 100;
}

export type GroupPolicyValue = { mode: 'inherit' } | { mode: 'value'; value: unknown };

export interface GroupConfigResult {
  account: string;
  current_account: string;
  group: Pick<GroupRow, 'group_id' | 'group_name' | 'member_count' | 'max_member_count' | 'membership' | 'confirmed_at'>;
  explicit: { policy?: Record<string, GroupPolicyValue>; session?: Record<string, GroupPolicyValue>;
    events?: Record<string, GroupPolicyValue>; approval?: Record<string, GroupPolicyValue> };
  revision: number;
  saved_revision: number;
  active_revision: number | null;
  apply_status: 'applied' | 'pending' | 'failed';
  apply_error?: string;
  route_generation: number;
  base_revision: string;
  effective: { policy: Record<string, unknown>; session: Record<string, unknown>;
    approval: Record<string, 'approval_required' | 'auto_execute'>; events: Record<string, boolean> };
  sources: { policy: Record<string, { source: string; source_index: number | null; source_label: string } | null>;
    session: Record<string, string>; approval: Record<string, string>; events: Record<string, string> };
  capabilities: { policy_fields: string[]; session_fields: string[]; approval_actions: string[]; event_kinds: string[];
    binding_available?: boolean | null; model_reference_available?: boolean | null };
  offline_snapshot?: boolean;
}

const path = (adapterId: string) => `/platforms/${encodeURIComponent(adapterId)}/groups`;

async function controlGet<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  try {
    const response = await controlClient.get<T>(url, { params });
    return response.data;
  } catch (error) {
    if (isAxiosError(error)) throw toApiError(error);
    throw error;
  }
}

async function controlPatch<T>(url: string, data: unknown): Promise<T> {
  try {
    const response = await controlClient.patch<T>(url, data);
    return response.data;
  } catch (error) {
    if (isAxiosError(error)) throw toApiError(error);
    throw error;
  }
}

export const groupApi = {
  accounts: (adapterId: string, running: boolean): Promise<GroupAccountsResult> => (
    running
      ? apiClient.get<GroupAccountsResult>(`/api${path(adapterId)}/accounts`)
      : controlGet<GroupAccountsResult>(`${path(adapterId)}/accounts`)
  ),
  list: (adapterId: string, query: GroupListQuery, running: boolean): Promise<GroupListResult> => (
    running
      ? apiClient.get<GroupListResult>(`/api${path(adapterId)}`, query as unknown as Record<string, unknown>)
      : controlGet<GroupListResult>(path(adapterId), query as unknown as Record<string, unknown>)
  ),
  settings: (adapterId: string, account: string, running: boolean): Promise<GroupSettings> => (
    running
      ? apiClient.get<GroupSettings>(`/api${path(adapterId)}/settings`, { account })
      : controlGet<GroupSettings>(`${path(adapterId)}/settings`, { account })
  ),
  saveSettings: (
    adapterId: string, data: { expected_self_id: string; expected_revision: number; mode: 'selected' | 'all';
      approval_defaults: GroupSettings['approval_defaults'] }, running: boolean,
  ): Promise<GroupSettings> => (
    running
      ? apiClient.patch<GroupSettings>(`/api${path(adapterId)}/settings`, data)
      : controlPatch<GroupSettings>(`${path(adapterId)}/settings`, data)
  ),
  sync: (adapterId: string, account: string): Promise<{ sync_id: string; status: string; reused: boolean }> => (
    apiClient.post(`/api${path(adapterId)}/sync`, { expected_self_id: account })
  ),
  syncStatus: (adapterId: string, account: string, syncId: string): Promise<GroupSyncStatus> => (
    apiClient.get(`/api${path(adapterId)}/sync/${encodeURIComponent(syncId)}`, { account })
  ),
  config: (adapterId: string, groupId: string, account: string, running: boolean): Promise<GroupConfigResult> => {
    const url = `${path(adapterId)}/${encodeURIComponent(groupId)}/config`;
    return running
      ? apiClient.get<GroupConfigResult>(`/api${url}`, { account })
      : controlGet<GroupConfigResult>(url, { account });
  },
  saveConfig: (adapterId: string, groupId: string, data: {
    expected_self_id: string; expected_revision: number; base_revision: string;
    section: 'policy' | 'session' | 'approval' | 'events'; values: Record<string, GroupPolicyValue>;
  }, running: boolean): Promise<GroupConfigResult> => {
    const url = `${path(adapterId)}/${encodeURIComponent(groupId)}/config`;
    return running
      ? apiClient.patch<GroupConfigResult>(`/api${url}`, data)
      : controlPatch<GroupConfigResult>(url, data);
  },
  applyConfig: (adapterId: string, groupId: string, account: string, revision: number): Promise<GroupConfigResult> => (
    apiClient.post(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/config/apply`, {
      expected_self_id: account, saved_revision: revision,
    })
  ),
  dryRunPolicy: (adapterId: string, groupId: string, data: {
    expected_self_id: string; expected_revision: number; base_revision: string;
    values: Record<string, GroupPolicyValue>; scenario: { probe: { text: string; at_self: boolean } };
  }): Promise<{ response_enabled: boolean; explicit: { triggered: boolean | null; reason: string };
    automatic: { decision: { triggered: boolean | null; reason: string } }; draft: boolean }> => (
    apiClient.post(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/dry-run`, data)
  ),
  bindingOptions: (adapterId: string, account: string): Promise<{ account: string; models: string[];
    plugins: Array<{ name: string; description: string; config_schema: Record<string, {
      type: string; description: string; session_overridable: boolean; options?: unknown[];
      minimum?: number; maximum?: number; integer?: boolean;
    }> }>;
    items: Array<{
    provider: 'session_class' | 'edictum'; config_name: string; enabled: boolean;
    available: boolean; description: string; session_fields?: string[];
  }> }> => apiClient.get(`/api${path(adapterId)}/bindings`, { account }),
  actionTypes: (adapterId: string, groupId: string, account: string): Promise<{ items: Array<{
    action_type: string; schema: Record<string, string>; risk: 'high' | 'normal';
    approval_mode: 'approval_required' | 'auto_execute'; approval_source: string;
    available: boolean; capability: string; membership: string;
  }> }> => apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/action-types`, { account }),
  members: (adapterId: string, groupId: string, account: string, q: string, page = 1): Promise<{
    items: GroupMember[]; total_loaded: number; page: number; page_size: number;
    truncated: boolean; complete: boolean;
  }> => apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/members`, { account, q, page }),
  info: (adapterId: string, groupId: string, account: string): Promise<Record<string, unknown>> => (
    apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/info`, { account })
  ),
  events: (adapterId: string, groupId: string, account: string, limit: 50 | 100): Promise<{
    items: GroupEvent[]; volatile: boolean; capacity: number; truncated: boolean;
  }> => apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/events`, { account, limit }),
  diagnostics: (adapterId: string, groupId: string, account: string, stage = '', requestId = ''): Promise<{
    records: GroupDiagnostic[]; available: boolean; capacity?: number;
  }> => apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/diagnostics`,
    { account, stage, request_id: requestId, limit: 50 }),
  diagnostic: (adapterId: string, groupId: string, account: string, requestId: string): Promise<{
    records: Array<Record<string, unknown>>; truncated: boolean;
  }> => apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/diagnostics/${encodeURIComponent(requestId)}`,
    { account }),
  send: (adapterId: string, groupId: string, data: {
    expected_self_id: string; action_id: string; message: string;
  }): Promise<GroupAction> => apiClient.post(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/send`, data),
  wake: (adapterId: string, groupId: string, data: {
    expected_self_id: string; request_id: string; prompt: string;
  }): Promise<{ status: string; reason?: string; request_id: string }> => (
    apiClient.post(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/wake`, data)
  ),
  wakeStatus: (adapterId: string, groupId: string, account: string, requestId: string): Promise<{
    status: string; reason?: string; detail?: string; request_id: string;
  }> => apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/wake/${encodeURIComponent(requestId)}`,
    { account }),
  actions: (adapterId: string, groupId: string, account: string, state: string, page = 1): Promise<{
    items: GroupAction[]; total: number; page: number; page_size: number;
  }> => apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/actions`, { account, state, page }),
  action: (adapterId: string, groupId: string, account: string, actionId: string): Promise<GroupAction> => (
    apiClient.get(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/actions/${encodeURIComponent(actionId)}`, { account })
  ),
  submitAction: (adapterId: string, groupId: string, data: {
    expected_self_id: string; action_id: string; action_type: string; params: Record<string, unknown>;
  }): Promise<GroupAction> => apiClient.post(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/actions`, data),
  decideAction: (adapterId: string, groupId: string, actionId: string, account: string, approve: boolean): Promise<GroupAction> => (
    apiClient.post(`/api${path(adapterId)}/${encodeURIComponent(groupId)}/actions/${encodeURIComponent(actionId)}/decision`,
      { expected_self_id: account, approve })
  ),
};

export interface GroupAction {
  action_id: string; self_id: string; group_id: string; action_type: string;
  params: Record<string, unknown>; actor_kind: 'panel' | 'model';
  state: 'pending' | 'expired' | 'rejected' | 'executing' | 'succeeded' | 'failed' | 'unknown';
  created_at: number; expires_at: number | null; decision_at: number | null; executed_at: number | null;
  result: { reason?: string } | null;
}

export interface GroupMember {
  user_id: string | number; nickname: string | null; card: string | null; role: string | null;
  title: string | null; join_time: number | null; last_sent_time: number | null;
}

export interface GroupEvent {
  id: number; kind: string; sub_type: string; user_id: string; operator_id: string;
  target_id: string; message_id: string; duration: number; time: number;
}

export interface GroupDiagnostic {
  request_id: string; recorded_at: string; session_id: string; self_id: string;
  stages: string[]; statuses: Record<string, string>; reason_codes: string[];
  send_status: string | null;
}
