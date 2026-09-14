import type { ConversationItem } from '@/api/chat';

export interface Conversation<Message> {
  id: string;   // 后端 conversation_id
  title: string;
  messages: Message[];
  updatedAt: number;
  // 是否已从后端加载历史
  loaded?: boolean;
  // 所属项目 id (无项目会话为 null/缺省, 归入"最近")
  projectId?: string | null;
  // 是否为尚未发送首条消息的后端预加载会话
  preloaded?: boolean;
  // 前端触发本次预加载时的设置键
  preloadKey?: string;
}

/** 将服务端元数据映射为会话状态, 可保留已有消息和加载状态 */
export function restoreConversation<Message = never>(
  item: ConversationItem,
  previous?: Conversation<Message>,
): Conversation<Message> {
  return {
    id: item.conversation_id,
    title: item.title,
    messages: previous?.messages ?? [],
    updatedAt: item.last_at,
    loaded: previous?.loaded ?? false,
    projectId: item.project_id ?? null,
  };
}
