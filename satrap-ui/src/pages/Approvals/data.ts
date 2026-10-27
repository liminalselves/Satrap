/**
 * 审批中心展示层数据与文案。
 *
 * 字段来自 Control 的统一审批目录 (`approvalsApi`), 列表项为 `UnifiedAction`.
 * 这里只做展示映射: 状态 / 来源 / 风险 / 相对时间, 不根据操作文案猜测风险,
 * 也不把 `applied` 当成平台执行成功 (平台结果以 `action.state` 为准).
 */
import type {
  ActionKey, ActionState, ApprovalPlatform, ApprovalSource, DecisionOutput, SourceCoverage, UnifiedAction,
} from '@/api/contracts';
import type { Tone } from '@/components/kit';

export type { ActionKey, ActionState, ApprovalPlatform, ApprovalSource, DecisionOutput, SourceCoverage, UnifiedAction };

export type StateFilter = ActionState | 'all';
export type SourceFilter = ApprovalSource | 'all';

export interface FilterScope {
  platform: string;
  account: string;
  source: SourceFilter;
  state: StateFilter;
  target: string;
  action: string;
  q: string;
  sort: 'created' | 'expires';
  order: 'asc' | 'desc';
}

export interface FacetCounts {
  states: Partial<Record<ActionState | 'all', number | null>>;
  sources: Partial<Record<ApprovalSource, number | null>>;
  platforms: Record<string, number | null>;
  pendingTotal: number | null;
  todayDecided: number | null;
  latencyMs: number | null;
}

export const ALL_STATES: ActionState[] = ['pending', 'ready', 'executing', 'succeeded', 'failed', 'unknown', 'rejected', 'expired'];
export const ALL_SOURCES: ApprovalSource[] = ['group', 'invitation', 'friend', 'memory'];

export const sourceLabels: Record<ApprovalSource, string> = {
  group: '群管理', invitation: '入群邀请', friend: '好友管理', memory: '记忆提案',
};

export const stateLabels: Record<ActionState, string> = {
  pending: '待审批', ready: '准备执行', executing: '执行中', succeeded: '已生效',
  failed: '执行失败', unknown: '结果未知', rejected: '已拒绝', expired: '已过期',
};

export const stateTone: Record<ActionState, Tone> = {
  pending: 'orange', ready: 'accent', executing: 'accent', succeeded: 'green',
  failed: 'red', unknown: 'orange', rejected: 'neutral', expired: 'neutral',
};

export const sourceTint: Record<ApprovalSource, 'pink' | 'purple' | 'teal' | 'green'> = {
  group: 'pink', invitation: 'purple', friend: 'teal', memory: 'green',
};

export const riskTone: Record<string, Tone> = { low: 'neutral', medium: 'orange', high: 'red', unknown: 'neutral' };
export const riskLabel: Record<string, string> = { low: '低风险', medium: '需复核', high: '高风险', unknown: '风险未知' };

export const decisionLabels: Record<DecisionOutput['result'], string> = {
  applied: '已写入', conflict: '修订冲突', unavailable: '当前不可用', failed: '提交失败', unknown: '结果未知',
};
export const decisionTone: Record<DecisionOutput['result'], Tone> = {
  applied: 'green', conflict: 'orange', unavailable: 'orange', failed: 'red', unknown: 'orange',
};

export const serviceLabels: Record<SourceCoverage['service'], string> = {
  control: '控制服务', backend: '后端服务', chat: 'Chat 服务',
};

const groupActionLabels: Record<string, string> = {
  recall_message: '撤回消息',
  kick_group_member: '移出成员',
  ban_group_member: '禁言成员',
  set_group_whole_ban: '全员禁言',
  ban_anonymous: '禁言匿名成员',
  set_group_admin: '设置管理员',
  set_group_anonymous: '匿名聊天',
  set_group_card: '修改群昵称',
  set_group_name: '修改群名',
  set_group_special_title: '设置头衔',
  leave_group: '退出或解散群',
  handle_group_request: '处理加群请求',
  handle_request: '处理好友申请',
  delete_friend: '删除好友',
  send_request: '发送好友申请',
};

export function actionKey(item: Pick<UnifiedAction, 'platform_id' | 'self_id' | 'source' | 'action_id'>): string {
  return JSON.stringify([item.platform_id, item.self_id, item.source, item.action_id]);
}

export function keyId(key: ActionKey): string {
  return JSON.stringify([key.platform_id, key.self_id, key.source, key.action_id]);
}

const asString = (value: unknown): string => (typeof value === 'string' ? value : value == null ? '' : String(value));
const asBool = (value: unknown): boolean => value === true;

export function groupLabel(item: UnifiedAction): string {
  if (!item.group_id) return '好友关系';
  return item.group_name || `群 ${item.group_id}`;
}

export function actionDescription(item: UnifiedAction): string {
  const identity = item.target.user_id || asString(item.params.user_id) || asString(item.params.request_id)
    || item.group_id || asString(item.params.name) || '目标';
  const target = item.target.nickname ? `${item.target.nickname} (${identity})` : identity;
  const approve = asBool(item.params.approve);

  if (item.source === 'memory') {
    const name = item.group_name || item.group_id || '会话';
    return `记忆提案 · ${name}${item.action_type ? ` · ${groupActionLabels[item.action_type] || item.action_type}` : ''}`;
  }
  if (item.source === 'invitation') return `${approve ? '接受' : '拒绝'}群 ${item.group_id} 的入群邀请`;
  switch (item.action_type) {
    case 'handle_request': return `${approve ? '同意' : '拒绝'} ${target} 的好友申请`;
    case 'delete_friend': return `删除好友 ${target}`;
    case 'send_request': return `向 ${target} 发送好友申请`;
    case 'handle_group_request': return `${approve ? '同意' : '拒绝'}成员入群申请 ${target}`;
    case 'set_group_whole_ban': return asBool(item.params.enable) ? '开启全员禁言' : '解除全员禁言';
    case 'leave_group': return asBool(item.params.dismiss) ? '解散群' : '退出群';
    case 'set_group_admin': return `${asBool(item.params.enable) ? '设置' : '取消'}成员 ${target} 的管理员权限`;
    case 'ban_group_member': {
      const seconds = Number(item.params.duration || 0);
      const duration = seconds % 3600 === 0 ? `${seconds / 3600} 小时` : seconds % 60 === 0 ? `${seconds / 60} 分钟` : `${seconds} 秒`;
      return seconds ? `禁言成员 ${target} ${duration}` : `解除成员 ${target} 的禁言`;
    }
    case 'ban_anonymous': return `禁言匿名成员 ${target}`;
    case 'kick_group_member': return `移出群成员 ${target}`;
    case 'recall_message': return `撤回群 ${item.group_id} 中的消息 ${asString(item.params.message_id)}`;
    case 'set_group_name': return `将群名修改为「${asString(item.params.name)}」`;
    case 'set_group_card': return `修改成员 ${target} 的群昵称为「${asString(item.params.card)}」`;
    case 'set_group_special_title': return `设置成员 ${target} 的头衔为「${asString(item.params.title)}」`;
    case 'set_group_anonymous': return asBool(item.params.enable) ? '开启匿名聊天' : '关闭匿名聊天';
    default:
      return `${groupActionLabels[item.action_type] || item.action_type}${item.params.user_id ? ' · ' + target : ''}`;
  }
}

export function actorLabel(item: UnifiedAction): string {
  const kind = item.actor_kind === 'model' ? '模型发起' : item.actor_kind === 'panel' ? '面板发起'
    : item.actor_kind === 'workflow' ? '工作流发起' : item.actor_kind === 'system' ? '系统' : '提交方';
  return item.actor_id ? `${kind} · ${item.actor_id}` : kind;
}

export function requesterLabel(item: UnifiedAction): string {
  if (!item.requester) return '—';
  const name = item.requester.name ? `${item.requester.name} (${item.requester.id})` : item.requester.id;
  return item.requester.kind ? `${name} · ${item.requester.kind}` : name;
}

/** 共用面板密码不提供真实个人身份: kind=panel 且 id=null 时只显示“面板”。 */
export function decidedByLabel(item: Pick<UnifiedAction, 'decided_by'>): string {
  const decided = item.decided_by;
  if (!decided) return '—';
  if (decided.kind === 'panel') return decided.id ? `面板 · ${decided.id}` : '面板';
  if (decided.kind === 'platform_user') return decided.id ? `平台用户 · ${decided.id}` : '平台用户';
  return '系统';
}

export function decisionLabel(item: Pick<UnifiedAction, 'decision'>): string {
  return item.decision === 'approved' ? '已批准' : item.decision === 'rejected' ? '已驳回' : '—';
}

export function isExpired(item: UnifiedAction, now: number): boolean {
  return (item.state === 'pending' || item.state === 'ready') && item.expires_at !== null && item.expires_at <= now;
}

export function isActionable(item: UnifiedAction, now: number): boolean {
  return (item.state === 'pending' || item.state === 'ready') && item.can_decide && !isExpired(item, now);
}

export function avatarInfo(item: UnifiedAction): { label: string; name: string; sub: string } {
  if (item.requester?.name) {
    return { label: firstChar(item.requester.name), name: item.requester.name, sub: item.requester.id };
  }
  if (item.target.nickname) {
    return { label: firstChar(item.target.nickname), name: item.target.nickname, sub: item.target.user_id ?? '' };
  }
  if (item.group_name) {
    return { label: firstChar(item.group_name), name: item.group_name, sub: item.group_id };
  }
  const source = sourceLabels[item.source];
  return { label: source.slice(0, 1), name: source, sub: item.action_id };
}

function firstChar(value: string): string {
  return Array.from(value.trim())[0] ?? '?';
}

export function businessLink(item: UnifiedAction): string {
  const platform = encodeURIComponent(item.platform_id);
  const account = encodeURIComponent(item.self_id);
  if (item.source === 'friend') return `/platforms/${platform}/friends?account=${account}`;
  if (item.source === 'invitation') return `/platforms/${platform}/groups?account=${account}&invites=1`;
  if (item.source === 'memory') {
    const query = new URLSearchParams({ view: 'archive', record_platform: item.platform_id });
    if (item.group_id) query.set('conversation', item.group_id);
    return `/conversations?${query}`;
  }
  return `/platforms/${platform}/groups/${encodeURIComponent(item.group_id)}/actions?account=${account}`;
}

export function businessLabel(item: UnifiedAction): string {
  if (item.source === 'friend') return '好友管理';
  if (item.source === 'invitation') return '入群邀请管理';
  if (item.source === 'memory') return '对话记忆';
  return '原群动作记录';
}

export function requestComment(item: UnifiedAction): string {
  const comment = item.request?.params?.comment ?? item.params.comment;
  return typeof comment === 'string' ? comment : '';
}

export function relativeTime(seconds: number, now: number = Date.now() / 1000): string {
  const diff = Math.max(0, now - seconds);
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 172800) return '昨天';
  return `${Math.floor(diff / 86400)} 天前`;
}

export function remainingLabel(seconds: number, now: number = Date.now() / 1000): string {
  const diff = seconds - now;
  if (diff <= 0) return '已到期';
  if (diff < 60) return `${Math.ceil(diff)} 秒后到期`;
  if (diff < 3600) return `${Math.ceil(diff / 60)} 分钟后到期`;
  if (diff < 86400) return `${Math.ceil(diff / 3600)} 小时后到期`;
  return `${Math.ceil(diff / 86400)} 天后到期`;
}

const pad = (value: number) => String(value).padStart(2, '0');

export function formatDateTime(seconds: number): string {
  const date = new Date(seconds * 1000);
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function formatHms(ms: number): string {
  const date = new Date(ms);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 平均决策延迟: null 表示没有可统计的人工决策, 不能当成 0。 */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '未知';
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
}

export function coverageTone(source: SourceCoverage): Tone {
  return source.state === 'available' ? 'green' : source.state === 'stale' ? 'orange' : source.state === 'auth_required' ? 'red' : 'red';
}

export function domainOf(link: string): string {
  try { return new URL(link, 'http://local').pathname; } catch { return link; }
}
