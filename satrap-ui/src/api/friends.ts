import { apiClient } from './client';

export interface Friend { user_id: string; nickname: string; remark: string; matched_by?: string[]; exact?: boolean }
export interface FriendRequest {
  request_id: string; user_id: string; comment: string; received_at: number; expires_at: number;
  revision: number; archived: boolean; archived_at: number | null; platform_state: string; execution_state: string;
  last_checked_at: number | null; decision: string | null; can_handle: boolean; requires_confirmation: boolean;
  handling_reason: string; verification?: string; platform_query_supported?: boolean;
}
export interface FriendPage<T> { items: T[]; has_more: boolean; next_cursor: string | null; coverage?: { complete: boolean }; unavailable_count?: number }
export interface FriendPolicy { protected_friend_ids: string[]; manager_ids: string[] }
export interface RequestPolicy { credential_days: number; history_days: number }
export interface FriendInfo extends FriendPolicy { current_account: string; capabilities: Record<string, { state: string; reason: string }> }
export interface FriendAction {
  action_id: string; self_id: string; action_type: 'delete_friend' | 'handle_request' | 'send_request';
  actor_kind: 'panel' | 'model'; state: string; params: { user_id?: string; request_id?: string; approve?: boolean; remark?: string; message?: string; expected_revision?: number };
  actor_id: string;
  created_at: number; expires_at: number; result: { code?: string; message?: string; status?: string; verification?: string } | null;
  target: Friend | null;
}
const root = (adapter: string) => `/api/platforms/${encodeURIComponent(adapter)}/friends`;
export const friendApi = {
  info: (adapter: string): Promise<FriendInfo> => apiClient.get(`${root(adapter)}/info`),
  list: (adapter: string, account: string, q: string, cursor?: string): Promise<FriendPage<Friend>> =>
    apiClient.get(root(adapter), { account, q, limit: 20, cursor }),
  requests: (adapter: string, account: string, cursor?: string, view: 'active' | 'archived' = 'active'): Promise<FriendPage<FriendRequest>> =>
    apiClient.get(`${root(adapter)}/requests`, { account, limit: 20, cursor, view }),
  recheckRequest: (adapter: string, account: string, id: string): Promise<FriendRequest> =>
    apiClient.post(`${root(adapter)}/requests/${encodeURIComponent(id)}/recheck`, { expected_self_id: account }),
  deleteRequest: (adapter: string, account: string, id: string, revision: number): Promise<{ status: string }> =>
    apiClient.post(`${root(adapter)}/requests/${encodeURIComponent(id)}/delete`, { expected_self_id: account, expected_revision: revision }),
  requestPolicy: (adapter: string, account: string): Promise<RequestPolicy> =>
    apiClient.get(`${root(adapter)}/request-policy`, { account }),
  saveRequestPolicy: (adapter: string, account: string, policy: RequestPolicy): Promise<RequestPolicy> =>
    apiClient.patch(`${root(adapter)}/request-policy`, { expected_self_id: account, ...policy }),
  actions: (adapter: string, account: string, page: number): Promise<{ items: FriendAction[]; has_more: boolean }> =>
    apiClient.get(`${root(adapter)}/actions`, { account, page, limit: 20 }),
  action: (adapter: string, account: string, id: string): Promise<FriendAction> =>
    apiClient.get(`${root(adapter)}/actions/${encodeURIComponent(id)}`, { account }),
  submit: (adapter: string, account: string, id: string, action: FriendAction['action_type'], params: FriendAction['params']): Promise<FriendAction> =>
    apiClient.post(`${root(adapter)}/actions`, { expected_self_id: account, action_id: id, action_type: action, params }, 40000),
  decide: (adapter: string, account: string, id: string, approve: boolean): Promise<FriendAction> =>
    apiClient.post(`${root(adapter)}/actions/${encodeURIComponent(id)}/decision`, { expected_self_id: account, approve }, 40000),
  policy: (adapter: string, account: string, ids: string[]): Promise<FriendPolicy> =>
    apiClient.patch(`${root(adapter)}/policy`, { expected_self_id: account, protected_friend_ids: ids }),
};
