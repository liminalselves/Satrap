import { apiClient } from './client';

export interface GroupInvitation {
  request_id: string;
  group_id: string;
  user_id: string;
  comment: string;
  revision: number;
  archived: boolean;
  can_handle: boolean;
  execution_state: string;
  decision: 'accepted' | 'rejected' | null;
  handling_reason: string;
  received_at: number;
}

export interface InvitationAction {
  action_id: string;
  group_id: string;
  state: string;
  params: { approve: boolean; sub_type: 'invite'; request_id?: string };
  result: { reason?: string; message?: string } | null;
}

interface Invitations { items: GroupInvitation[]; has_more: boolean; next_cursor: string | null; unavailable_count: number }
interface Actions { items: InvitationAction[]; total: number; page: number; page_size: number }
const path = (adapterId: string) => `/api/platforms/${encodeURIComponent(adapterId)}/account-requests/group-invites`;

export const accountRequestsApi = {
  list: (adapterId: string, account: string, view: string, cursor?: string, signal?: AbortSignal) =>
    apiClient.get<Invitations>(path(adapterId), { account, view, ...(cursor ? { cursor } : {}) }, signal),
  actions: (adapterId: string, account: string, page: number, signal?: AbortSignal) =>
    apiClient.get<Actions>(`${path(adapterId)}/actions`, { account, page, state: 'all' }, signal),
  handle: (adapterId: string, account: string, invitation: GroupInvitation, approve: boolean, actionId: string) =>
    apiClient.post<InvitationAction>(path(adapterId), { expected_self_id: account, action_id: actionId,
      request_id: invitation.request_id, expected_revision: invitation.revision, approve }),
  decide: (adapterId: string, account: string, actionId: string, approve: boolean) =>
    apiClient.post<InvitationAction>(`${path(adapterId)}/actions/${encodeURIComponent(actionId)}/decision`, { expected_self_id: account, approve }),
};
