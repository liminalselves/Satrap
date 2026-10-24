import { apiClient } from './client';
import { friendApi } from './friends';
import { groupApi } from './groups';
import { accountRequestsApi } from './accountRequests';
import { groupActionLabels, groupActionStateLabels } from '@/pages/Groups/groupActionLabels';

export type ActionSource = 'group' | 'invitation' | 'friend';
export interface PlatformAction {
  platform_id: string; self_id: string; source: ActionSource; action_id: string; group_id: string;
  action_type: string; params: Record<string, unknown>; actor_kind: string; actor_id: string;
  state: string; created_at: number; expires_at: number | null;
  result: { reason?: string; message?: string; verification?: string } | null;
  target: { user_id?: string; nickname?: string; remark?: string }; can_decide: boolean; unavailable_reason: string;
}
export interface ActionCatalog {
  items: PlatformAction[]; total: number; pending_count: number; complete: boolean;
  platforms: { platform_id: string; current_account: string; online: boolean; accounts: string[]; pending_count: number | null }[];
  warnings: string[]; has_more: boolean; next_cursor: string | null;
}
export interface ActionFilters { platform?: string; account?: string; source?: string; state?: string; target?: string; action?: string; cursor?: string }
export const actionSourceLabels: Record<ActionSource, string> = { group: '群管理', invitation: '入群邀请', friend: '好友管理' };
export const actionStateLabels: Record<string, string> = { ...groupActionStateLabels, pending: '待人工批准', ready: '准备执行' };

export function approvalLink(filters: ActionFilters = {}): string {
  const query = new URLSearchParams(Object.entries({ ...filters, ...(filters.action && !filters.state ? { state: 'all' } : {}) }).filter(([, value]) => !!value) as [string, string][]);
  return '/platforms/approvals' + (query.size ? '?' + query : '');
}

export function actionKey(item: PlatformAction): string {
  return JSON.stringify([item.platform_id, item.self_id, item.source, item.action_id]);
}

export function actionDescription(item: PlatformAction): string {
  const identity = item.target.user_id || String(item.params.user_id || item.params.request_id || item.group_id || '目标');
  const target = item.target.nickname ? `${item.target.nickname} (${identity})` : identity;
  if (item.source === 'invitation') return `${item.params.approve ? '接受' : '拒绝'}群 ${item.group_id} 的入群邀请`;
  if (item.action_type === 'handle_request') return `${item.params.approve ? '同意' : '拒绝'} ${target} 的好友申请`;
  if (item.action_type === 'delete_friend') return `删除好友 ${target}`;
  if (item.action_type === 'send_request') return `向 ${target} 发送好友申请`;
  if (item.action_type === 'handle_group_request') return `${item.params.approve ? '同意' : '拒绝'}成员入群申请 ${target}`;
  if (item.action_type === 'set_group_whole_ban') return item.params.enable ? '开启全员禁言' : '解除全员禁言';
  if (item.action_type === 'leave_group') return item.params.dismiss ? '解散群' : '退出群';
  if (item.action_type === 'set_group_admin') return `${item.params.enable ? '设置' : '取消'}成员 ${target} 的管理员权限`;
  if (item.action_type === 'ban_group_member') {
    const seconds = Number(item.params.duration || 0);
    const duration = seconds % 3600 === 0 ? `${seconds / 3600} 小时` : seconds % 60 === 0 ? `${seconds / 60} 分钟` : `${seconds} 秒`;
    return seconds ? `禁言成员 ${target} ${duration}` : `解除成员 ${target} 的禁言`;
  }
  return `${groupActionLabels[item.action_type] || item.action_type}${item.params.user_id ? ' · ' + target : ''}`;
}

export const platformActionsApi = {
  list: (filters: ActionFilters, signal?: AbortSignal): Promise<ActionCatalog> => apiClient.get('/api/platform-actions', {
    platform_id: filters.platform, account: filters.account || 'current', source: filters.source || 'all', state: filters.state || (filters.action ? 'all' : 'pending'),
    target: filters.target, action_id: filters.action, cursor: filters.cursor, limit: 50,
  }, signal),
  summary: (signal?: AbortSignal): Promise<ActionCatalog> => apiClient.get('/api/platform-actions/summary', undefined, signal),
  decide: (item: PlatformAction, approve: boolean) => {
    switch (item.source) {
      case 'group': return groupApi.decideAction(item.platform_id, item.group_id, item.action_id, item.self_id, approve);
      case 'invitation': return accountRequestsApi.decide(item.platform_id, item.self_id, item.action_id, approve);
      case 'friend': return friendApi.decide(item.platform_id, item.self_id, item.action_id, approve);
    }
  },
};
