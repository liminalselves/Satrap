import { CHAT_API_URL } from '@/utils/constants';

// ==================== 类型 ====================

// 工具调用明细 (对齐 display_tool_calls)
export interface ToolCall {
  seq: number;
  name: string;
  arguments: string;          // JSON 字符串 (每值截断前 20 字符)
  success: boolean | null;    // null=进行中 / true=完成 / false=失败
  call_id: string;
  created_at: number;
}

// 附件信息
export interface Attachment {
  name: string;
  url: string;
  type: string;
}

// 消息段 (按时间顺序)
export interface MessageSegment {
  type: 'thinking' | 'tool' | 'content';
  content?: string;
  tool?: ToolCall;
}

// 对话轮次 (对齐 display_turns)
export interface ChatTurn {
  id: number;
  turn_index: number;
  user_input: string;
  thinking: string | null;
  answer: string;
  attachments: Attachment[] | null;
  segments: MessageSegment[] | null;
  created_at: number;
  tool_calls: ToolCall[];
}

// 会话列表项
export interface ConversationItem {
  conversation_id: string;
  turn_count: number;
  last_at: number;
  title: string;
}

// 模型配置项
export interface ModelConfigItem {
  name: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
  context_window?: number;
  history_ratio?: number;
  reasoning_body?: Record<string, unknown>;
  thinking_field_name?: string;
  thinking_fields?: string[];
}

// 能力项
export interface CapabilityItem {
  name: string;
  description: string;
  enabled: boolean;
}

export type CapabilityKind = 'tools' | 'skills' | 'mcp' | 'handlers' | 'commands';

// 插件信息
export interface ChatPlugin {
  name: string;
  version: string;
  description: string;
  dir: string;
  enabled: boolean;
  capabilities: Record<CapabilityKind, CapabilityItem[]>;
}

// 插件配置字段 (对齐后端 ConfigField)
export interface PluginConfigField {
  type: string;
  default: unknown;
  description: string;
  options?: string[];
}

// 插件配置响应
export interface PluginConfigResponse {
  ok: boolean;
  schema: Record<string, PluginConfigField>;
  config: Record<string, unknown>;
}

// 记忆记录
export interface MemoryRecord {
  id: string;
  scope: string;
  title: string;
  content: string;
  tags: string[];
  importance: number;
  created_at: string;
  updated_at: string;
}

// WS 推送事件
export type ChatEvent =
  | { type: 'subscribed'; conversation_id: string }
  | { type: 'turn_start'; user_input: string }
  | { type: 'thinking_delta'; delta: string }
  | { type: 'content_delta'; delta: string }
  | { type: 'tool_start'; name: string; arguments: unknown; call_id: string }
  | { type: 'tool_end'; name: string; call_id: string; success: boolean }
  | { type: 'turn_done'; answer: string }
  | { type: 'error'; error?: string; message?: string };

// ==================== HTTP ====================

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(`${CHAT_API_URL}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = (await resp.json()) as T & { error?: string };
  if (!resp.ok) {
    throw new Error(data.error || `请求失败: ${resp.status}`);
  }
  return data;
}

export const chatApi = {
  health: () => request<{ ok: boolean; conversations: number }>('GET', '/api/chat/health'),

  listModels: () => request<{ models: string[] }>('GET', '/api/chat/models'),

  createConversation: (model: string = 'default', think = 'off', systemPrompt?: string) =>
    request<{ ok: boolean; conversation_id: string }>('POST', '/api/chat/conversations', {
      model, think, ...(systemPrompt ? { system_prompt: systemPrompt } : {}),
    }),

  listConversations: () => request<{ conversations: ConversationItem[] }>('GET', '/api/chat/conversations'),

  deleteConversation: (conversationId: string) =>
    request<{ ok: boolean }>('DELETE', `/api/chat/conversations/${encodeURIComponent(conversationId)}`),

  listTurns: (conversation: string) =>
    request<{ turns: ChatTurn[] }>('GET', `/api/chat/turns?conversation=${encodeURIComponent(conversation)}`),

  send: (conversation: string, text: string, think = 'off', attachments?: Attachment[]) =>
    request<{ ok: boolean; error?: string }>('POST', '/api/chat/send', { conversation, text, think, attachments }),

  // 模型配置管理
  listModelsDetail: () =>
    request<{ ok: boolean; models: Record<string, ModelConfigItem> }>('GET', '/api/chat/models/detail'),

  addModel: (config: ModelConfigItem) =>
    request<{ ok: boolean; error?: string }>('POST', '/api/chat/models', config),

  updateModel: (name: string, config: Partial<ModelConfigItem>) =>
    request<{ ok: boolean; error?: string }>('PUT', `/api/chat/models/${encodeURIComponent(name)}`, config),

  deleteModel: (name: string) =>
    request<{ ok: boolean; error?: string }>('DELETE', `/api/chat/models/${encodeURIComponent(name)}`),

  // 文件上传
  uploadFile: (conversation: string, fileName: string, fileDataBase64: string) =>
    request<{ ok: boolean; file_name: string; file_url: string; file_type: string; error?: string }>(
      'POST', '/api/chat/upload',
      { conversation, file_name: fileName, file_data: fileDataBase64 },
    ),

  // Retry / Fork
  retry: (conversation: string, think?: string) =>
    request<{ ok: boolean; error?: string }>('POST', '/api/chat/retry', { conversation, think }),

  fork: (conversation: string, turnIndex: number) =>
    request<{ ok: boolean; conversation_id: string; copied_turns: number; error?: string }>(
      'POST', '/api/chat/fork',
      { conversation, turn_index: turnIndex },
    ),

  // 取消生成
  cancel: (conversation: string) =>
    request<{ ok: boolean; error?: string }>('POST', '/api/chat/cancel', { conversation }),

  listPlugins: () => request<{ plugins: ChatPlugin[] }>('GET', '/api/chat/plugins'),

  setPluginEnabled: (name: string, enabled: boolean) =>
    request<{ ok: boolean; error?: string }>(
      'POST',
      `/api/chat/plugins/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`
    ),

  setPluginCapability: (name: string, kind: CapabilityKind, cap: string, enabled: boolean) =>
    request<{ ok: boolean; error?: string }>(
      'POST',
      `/api/chat/plugins/${encodeURIComponent(name)}/capability`,
      { kind, cap, enabled }
    ),

  // 插件配置
  getPluginConfig: (name: string) =>
    request<PluginConfigResponse>('GET', `/api/chat/plugins/${encodeURIComponent(name)}/config`),

  savePluginConfig: (name: string, config: Record<string, unknown>) =>
    request<{ ok: boolean; error?: string }>(
      'PUT',
      `/api/chat/plugins/${encodeURIComponent(name)}/config`,
      { config }
    ),

  // 记忆管理
  listMemories: (scope = 'web_chat') =>
    request<{ ok: boolean; memories: MemoryRecord[] }>('GET', `/api/chat/memories?scope=${encodeURIComponent(scope)}`),

  addMemory: (title: string, content: string, tags = '', importance = 1, scope = 'web_chat') =>
    request<{ ok: boolean; memory: MemoryRecord }>('POST', '/api/chat/memories', {
      title, content, tags, importance, scope,
    }),

  updateMemory: (id: string, fields: Partial<Pick<MemoryRecord, 'title' | 'content' | 'tags' | 'importance'>>, scope = 'web_chat') =>
    request<{ ok: boolean; memory: MemoryRecord }>('PUT', `/api/chat/memories/${encodeURIComponent(id)}`, {
      ...fields, scope,
    }),

  deleteMemory: (id: string, scope = 'web_chat') =>
    request<{ ok: boolean }>('DELETE', `/api/chat/memories/${encodeURIComponent(id)}?scope=${encodeURIComponent(scope)}`),
};

// ==================== WebSocket ====================

export type ChatEventHandler = (event: ChatEvent) => void;

// 订阅会话实时事件; 返回取消订阅函数
export function subscribeChat(conversationId: string, onEvent: ChatEventHandler): () => void {
  const wsUrl = `${CHAT_API_URL.replace(/^http/, 'ws')}/ws/chat?conversation=${encodeURIComponent(conversationId)}`;
  const ws = new WebSocket(wsUrl);

  ws.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data) as ChatEvent);
    } catch (err) {
      console.error('[ChatWS] 解析消息失败:', err);
    }
  };
  ws.onerror = (err) => {
    console.error('[ChatWS] 连接错误:', err);
  };
  ws.onclose = (e) => {
    console.log('[ChatWS] 连接关闭:', e.code, e.reason);
  };

  return () => {
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      ws.close(1000, 'unsubscribe');
    }
  };
}
