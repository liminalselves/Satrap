import { apiClient } from './client';

export interface Friend { user_id: string; nickname: string; remark: string; matched_by?: string[]; exact?: boolean }
export interface FriendRequest { request_id: string; user_id: string; comment: string; received_at: number; expires_at: number }
export interface FriendPage<T> { items: T[]; has_more: boolean; next_cursor: string | null; coverage?: { complete: boolean }; unavailable_count?: number }
export interface FriendPolicy { protected_friend_ids: string[]; manager_ids: string[] }
export interface FriendInfo extends FriendPolicy { current_account: string; capabilities: Record<string, { state: string; reason: string }> }
export interface FriendAction {
  action_id: string; self_id: string; action_type: 'delete_friend' | 'handle_request';
  actor_kind: 'panel' | 'model'; state: string; params: { user_id?: string; request_id?: string; approve?: boolean; remark?: string };
  actor_id: string;
  created_at: number; expires_at: number; result: { code?: string; message?: string; verification?: string } | null;
  target: Friend | null;
}
const root = (adapter: string) => `/api/platforms/${encodeURIComponent(adapter)}/friends`;
export const friendApi = {
  info: (adapter: string): Promise<FriendInfo> => apiClient.get(`${root(adapter)}/info`),
  list: (adapter: string, account: string, q: string, cursor?: string): Promise<FriendPage<Friend>> =>
    apiClient.get(root(adapter), { account, q, limit: 20, cursor }),
  requests: (adapter: string, account: string, cursor?: string): Promise<FriendPage<FriendRequest>> =>
    apiClient.get(`${root(adapter)}/requests`, { account, limit: 20, cursor }),
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
