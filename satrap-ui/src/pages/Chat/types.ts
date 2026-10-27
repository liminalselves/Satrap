import type {
  Attachment,
  ContextTurnStats,
  OutputFile,
  ResponseMetadata,
  ToolCall,
} from '@/api/chat';
import type { Conversation as ConversationState } from './conversations';
import type { LocalMessageSegment } from './streaming';

// 消息角色
export type MessageRole = 'user' | 'assistant';

export interface LocalResponseVariant {
  responseMetadata?: ResponseMetadata | null;
  outputFiles?: OutputFile[];
  variantIndex: number;
  content: string;
  thinking?: string;
  toolCalls: ToolCall[];
  segments?: LocalMessageSegment[];
  contextStats?: ContextTurnStats | null;
}

// 单条消息
export interface ChatMessage {
  responseMetadata?: ResponseMetadata | null;
  outputFiles?: OutputFile[];
  id: string;
  role: MessageRole;
  content: string;
  timestamp: number;
  // 思考流内容 (聚合, 兼容旧数据)
  thinking?: string;
  // 工具调用明细 (聚合, 兼容旧数据)
  toolCalls?: ToolCall[];
  // 附件列表
  attachments?: Attachment[];
  // 流式输出中标记
  streaming?: boolean;
  // 对应后端 turn_index (用于 fork)
  turnIndex?: number;
  // 同一用户输入对应的多个助手回复
  variants?: LocalResponseVariant[];
  // 当前激活的回复版本
  activeVariant?: number;
  // 已持久化的回复版本总数
  variantCount?: number;
  variantIndices?: number[];
  // 分段内容 (按时间顺序, 用于流式渲染)
  segments?: LocalMessageSegment[];
  // 本回复版本对应的上下文准备和 API token 统计
  contextStats?: ContextTurnStats | null;
}

// 一个会话
export type Conversation = ConversationState<ChatMessage>;
