import type { ConversationItem } from '@/api/chat';

export interface Conversation<Message> {
  id: string;   // 后端 conversation_id
  title: string;
  model?: string;   // 服务端已确认的会话模型, 不从其它会话或整体默认补猜
  messages: Message[];
  updatedAt: number;
  // 是否已从后端加载历史
  loaded?: boolean;
  hasOlder?: boolean;
  olderBefore?: number | null;
  // 所属项目 id (无项目会话为 null/缺省, 归入"最近")
  projectId?: string | null;
  // 是否为尚未发送首条消息的后端预加载会话
  preloaded?: boolean;
  // 前端触发本次预加载时的设置键
  preloadKey?: string;
  locallyOpened?: boolean;   // 本次主动创建或打开的会话, 不随目录分页丢失
}

/**
 * 判断刷新是否覆盖原已加载末项, 与服务端 last_at DESC 和 UTF-8 ID ASC 排序保持一致
 * 参数:
 * - last: 当前页末项, 空页时未提供
 * - boundary: 刷新前已加载的末项, 初次加载时为 null
 * 返回:
 * - 是否已到达或越过原边界, 边界记录被删除时仍可根据排序位置停止
 */
export function conversationBoundaryReached(last: ConversationItem | undefined, boundary: ConversationItem | null): boolean {
  if (!boundary) return true;
  if (!last) return false;
  if (last.last_at !== boundary.last_at) return last.last_at < boundary.last_at;
  const encoder = new TextEncoder();
  const left = encoder.encode(last.conversation_id), right = encoder.encode(boundary.conversation_id);
  for (let index = 0; index < Math.min(left.length, right.length); index++) {
    if (left[index] !== right[index]) return left[index] > right[index];
  }
  return left.length >= right.length;
}

/** 服务端目录负责搜索历史, 本次主动打开的会话按当前筛选补入侧栏 */
export function sidebarConversationVisible<Message>(conversation: Conversation<Message>, listed: Set<string>, activeId: string, search: string, project: string): boolean {
  if (project && conversation.projectId !== project) return false;
  if (listed.has(conversation.id)) return true;
  if (conversation.id === activeId) return true;
  const matches = !search.trim() || conversation.title.toLowerCase().includes(search.trim().toLowerCase());
  return matches && (conversation.id === '__draft__' || !!conversation.preloaded || !!conversation.locallyOpened);
}

/** 将服务端元数据映射为会话状态, 可保留已有消息和加载状态 */
export function restoreConversation<Message = never>(
  item: ConversationItem,
  previous?: Conversation<Message>,
): Conversation<Message> {
  return {
    ...previous,
    id: item.conversation_id,
    title: item.title,
    model: previous?.model ?? item.model,
    // 目录读取可能早于模型切换, 不能覆盖已经回填的绑定; 激活后由模型接口与 WS 更新
    messages: previous?.messages ?? [],
    updatedAt: item.last_at,
    loaded: previous?.loaded ?? false,
    projectId: item.project_id ?? null,
  };
}
