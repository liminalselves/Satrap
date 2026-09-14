import type { LLMConfig } from './types';
import { getChatApiUrl } from '@/utils/constants';
import type { SessionPluginSettings } from './pluginSettings';
import type { ModelOptions } from '@/components/common/PluginConfigFields';
import type { RagResult } from './rag';

// ==================== 类型 ====================

// 工具调用明细 (对齐 display_tool_calls)
export interface ToolCall {
  seq: number;
  name: string;
  arguments: string;   // JSON 字符串 (每值截断前 20 字符)
  success: boolean | null;   // null=进行中 / true=完成 / false=失败
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

export interface ChatTurnVariant {
  variant_index: number;
  thinking: string | null;
  answer: string;
  segments: MessageSegment[] | null;
  created_at: number;
  tool_calls: ToolCall[];
  context_stats?: ContextTurnStats | null;
}

export interface ContextRequestStats {
  model?: string | null;
  strategy: 'sliding' | 'mid_truncate' | 'summarize';
  compressed: boolean;
  original_turns: number;
  prepared_turns: number;
  original_estimated_input_tokens: number;
  estimated_input_tokens: number;
  effective_input_tokens: number;
  preflight_token_source: 'tokenizer' | 'experience' | 'api_calibrated';
  history_budget: number;
  trigger_tokens: number;
  floor_tokens: number;
  api_input_tokens?: number | null;
  api_output_tokens?: number | null;
  api_total_tokens?: number | null;
  api_cached_tokens?: number | null;
}

export interface ContextTurnStats {
  request_count: number;
  last_request: ContextRequestStats;
  total_api_input_tokens?: number | null;
  total_api_output_tokens?: number | null;
  total_api_tokens?: number | null;
  total_api_cached_tokens?: number | null;
}

// 对话轮次 (对齐 display_turns)
export interface ChatTurn {
  generating?: boolean;
  interrupted?: boolean;
  id: number;
  turn_index: number;
  user_input: string;
  thinking: string | null;
  answer: string;
  attachments: Attachment[] | null;
  segments: MessageSegment[] | null;
  created_at: number;
  tool_calls: ToolCall[];
  active_variant: number;
  variant_count: number;
  variants: ChatTurnVariant[];
  context_stats?: ContextTurnStats | null;
}

// 会话列表项
export interface ConversationItem {
  conversation_id: string;
  turn_count: number;
  last_at: number;
  title: string;
  project_id?: string | null;   // 所属项目 (无项目会话为 null/缺省)
  model?: string;
  think?: string;
  created_at?: number;
}

export interface ChatHistoryItem extends ConversationItem {
  model: string;
  think: string;
  created_at: number;
  active: boolean;
  generating: boolean;
  waiting_user: boolean;
}

export interface ChatHistoryQuery {
  search?: string;
  project_id?: string;
  model?: string;
  turn_count?: 'all' | 'empty' | 'single';
  older_than_days?: number;
  page?: number;
  page_size?: number;
}

export interface ChatHistoryResult {
  items: ChatHistoryItem[];
  total: number;
  page: number;
  page_size: number;
  storage_size_bytes: number | null;
  storage_size_updated_at?: number | null;
  mode: 'hot' | 'cold';
}

export interface ChatHistoryArchive {
  archive_id: string;
  platform_id: string;
  session_id: string;
  deleted_at: number;
  size_bytes: number;
  has_files: boolean;
  tables: string[];
  title: string;
  model: string;
  think: string;
  project_id?: string | null;
  created_at: number;
  turn_count: number;
}

export interface ChatHistoryTrashResult {
  items: ChatHistoryArchive[];
  total: number;
  storage_size_bytes: number;
  mode: 'hot' | 'cold';
}

export interface ChatHistoryDeleteRequest {
  mode: 'selected' | 'empty' | 'single' | 'filtered';
  conversation_ids?: string[];
  filters?: ChatHistoryQuery;
  force?: boolean;
}

export interface ChatHistoryDeleteResult {
  ok: boolean;
  deleted_count: number;
  results: Array<{
    ok: boolean;
    conversation_id: string;
    archive_id?: string;
    error?: string;
  }>;
}

// 项目 (绑定的工作区文件夹)
export interface ProjectItem {
  project_id: string;
  name: string;
  root_path: string;
  created_at: number;
}

// 目录浏览项 (子目录; Windows 根视图为盘符)
export interface DirEntry {
  name: string;
  path: string;
}

// 模型配置项
export type ModelConfigItem = LLMConfig;

// 预加载及首次发送使用的会话构建设置
export interface ChatPreloadSettings {
  model: string;
  think: string;
  temperature: number;
  systemPrompt: string;
  projectId: string | null;
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
  availability?: { allowed: boolean; reason_code: string; message: string; warnings: string[] };
  compatibility?: { satrap?: string };
  applicability?: { session_types?: string[]; platforms?: string[] | "*" };
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
export type ChatEvent = (
  | { type: 'snapshot'; conversation_id: string; seq: number; stream_id: string; state: 'idle' | 'preparing' | 'running' | 'cancelling'; active_turn_id: number | null; turns: ChatTurn[]; pending_user_inputs: { request_id: string; question: string; options: string[] }[] }
  | { type: 'resync_required' }
  | { type: 'subscribed'; conversation_id: string }
  | { type: 'turn_start'; user_input: string; turn_id: number; turn_index: number; variant_index: number; retry?: boolean }
  | { type: 'thinking_delta'; delta: string }
  | { type: 'content_delta'; delta: string }
  | { type: 'tool_start'; name: string; arguments: unknown; call_id: string }
  | { type: 'tool_end'; name: string; call_id: string; success: boolean }
  | { type: 'ask_user'; conversation_id: string; request_id: string; question: string; options?: string[] }
  | { type: 'ask_user_end'; conversation_id: string; request_id: string; status: 'answered' | 'timeout' | 'cancelled' }
  | { type: 'turn_done'; answer: string; turn_id: number; turn_index: number; variant_index: number; variant_count: number; context_stats?: ContextTurnStats | null }
  | { type: 'variant_selected'; turn_index: number; variant_index: number }
  | { type: 'error'; error?: string; message?: string; turn_id?: number; turn_index?: number; variant_index?: number; variant_count?: number; context_stats?: ContextTurnStats | null }
) & { seq?: number; stream_id?: string };

// ==================== HTTP 请求 ====================

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const send = () => fetch(`${getChatApiUrl()}${path}`, {
    method,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let resp = await send();
  if (resp.status === 401) {
    const { establishApiSession } = await import('@/api/auth');
    await establishApiSession(getChatApiUrl());
    resp = await send();
  }
  const data = (await resp.json()) as T & { error?: string };
  if (!resp.ok) {
    throw new Error(data.error || `请求失败: ${resp.status}`);
  }
  return data;
}

function historyQueryString(query: ChatHistoryQuery = {}): string {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value));
    }
  });
  const encoded = params.toString();
  return encoded ? `?${encoded}` : '';
}

export const chatApi = {
  refreshHistoryStorage: () => request<{ storage_size_bytes: number | null; storage_size_updated_at: number | null }>('POST', '/api/chat/history/storage'),
  ragList: (sessionId: string, kbId: string) => request<RagResult>('GET', `/api/chat/rag?${new URLSearchParams({ session_id: sessionId, kb_id: kbId })}`),
  ragAction: (sessionId: string, payload: Record<string, unknown>) => request<Record<string, unknown>>('POST', `/api/chat/rag?${new URLSearchParams({ session_id: sessionId })}`, payload),
  pluginModelOptions: () => request<{ options: ModelOptions }>('GET', '/api/chat/plugin-model-options'),
  getSessionPluginConfig: (conversationId: string, plugin: string) => request<SessionPluginSettings>('GET', `/api/chat/session-plugin-config?${new URLSearchParams({ conversation_id: conversationId, plugin })}`),
  saveSessionPluginConfig: (conversationId: string, plugin: string, overrides: Record<string, unknown>, revision: number) => request<SessionPluginSettings>('PUT', `/api/chat/session-plugin-config?${new URLSearchParams({ conversation_id: conversationId, plugin })}`, { overrides, expected_revision: revision }),
  health: () => request<{ ok: boolean; conversations: number; preloaded: number }>('GET', '/api/chat/health'),

  listModels: () => request<{ models: string[] }>('GET', '/api/chat/models'),

  createConversation: (model: string = 'default', think = 'off', systemPrompt?: string, projectId?: string) =>
    request<{ ok: boolean; conversation_id: string }>('POST', '/api/chat/conversations', {
      model, think,
      ...(systemPrompt ? { system_prompt: systemPrompt } : {}),
      ...(projectId ? { project_id: projectId } : {}),
    }),

  preloadConversation: (settings: ChatPreloadSettings) =>
    request<{ ok: boolean; conversation_id: string }>('POST', '/api/chat/conversations/preload', {
      model: settings.model,
      think: settings.think,
      temperature: settings.temperature,
      system_prompt: settings.systemPrompt,
      project_id: settings.projectId,
    }),

  // 会话改绑项目 (null = 移出项目)
  setConversationProject: (conversationId: string, projectId: string | null) =>
    request<{ ok: boolean; error?: string }>(
      'POST', `/api/chat/conversations/${encodeURIComponent(conversationId)}/project`,
      { project_id: projectId },
    ),

  // 项目管理
  listProjects: () => request<{ projects: ProjectItem[] }>('GET', '/api/projects'),

  createProject: (name: string, rootPath: string) =>
    request<{ ok: boolean; project?: ProjectItem; error?: string }>(
      'POST', '/api/projects', { name, root_path: rootPath },
    ),

  deleteProject: (projectId: string) =>
    request<{ ok: boolean; error?: string }>('DELETE', `/api/projects/${encodeURIComponent(projectId)}`),

  // 浏览服务器目录 (新建项目选择工作区; path 为空 = 根视图/盘符视图)
  browseDirs: (path = '') =>
    request<{ ok: boolean; path: string; parent: string | null; dirs: DirEntry[] }>(
      'GET', `/api/fs/browse?path=${encodeURIComponent(path)}`,
    ),

  listConversations: () => request<{ conversations: ConversationItem[] }>('GET', '/api/chat/conversations'),

  queryHistory: (query: ChatHistoryQuery = {}) =>
    request<ChatHistoryResult>('GET', `/api/chat/history${historyQueryString(query)}`),

  deleteHistory: (data: ChatHistoryDeleteRequest) =>
    request<ChatHistoryDeleteResult>('POST', '/api/chat/history/delete', data),

  listHistoryTrash: () =>
    request<ChatHistoryTrashResult>('GET', '/api/chat/history/trash'),

  restoreHistory: (archiveId: string) =>
    request<{ ok: boolean; session_id: string; archive_id: string }>(
      'POST', '/api/chat/history/trash/restore', { archive_id: archiveId },
    ),

  purgeHistory: (archiveId: string) =>
    request<{ ok: boolean; archive_id: string }>(
      'POST', '/api/chat/history/trash/purge', { archive_id: archiveId },
    ),

  deleteConversation: (conversationId: string) =>
    request<{ ok: boolean }>('DELETE', `/api/chat/conversations/${encodeURIComponent(conversationId)}`),

  listTurns: (conversation: string) =>
    request<{ turns: ChatTurn[] }>('GET', `/api/chat/turns?conversation=${encodeURIComponent(conversation)}`),

  send: (
    conversation: string,
    text: string,
    think = 'off',
    attachments?: Attachment[],
    preloadSettings?: ChatPreloadSettings,
  ) =>
    request<{
      ok: boolean;
      conversation_id: string;
      turn_id: number;
      turn_index: number;
      variant_index: number;
      error?: string;
    }>('POST', '/api/chat/send', {
      conversation,
      text,
      think,
      attachments,
      ...(preloadSettings ? {
        model: preloadSettings.model,
        temperature: preloadSettings.temperature,
        system_prompt: preloadSettings.systemPrompt,
        project_id: preloadSettings.projectId,
      } : {}),
    }),

  answerAskUser: (conversation: string, requestId: string, answer: string) =>
    request<{ ok: boolean; error?: string }>('POST', '/api/chat/ask-user/answer', {
      conversation,
      request_id: requestId,
      answer,
    }),

  // 模型配置管理
  listModelsDetail: () =>
    request<{ ok: boolean; models: Record<string, ModelConfigItem> }>('GET', '/api/chat/models/detail'),

  addModel: (config: ModelConfigItem) =>
    request<{ ok: boolean; error?: string; refreshed_conversations?: number; deferred_conversations?: number }>('POST', '/api/chat/models', config),

  updateModel: (name: string, config: Partial<ModelConfigItem>) =>
    request<{ ok: boolean; error?: string; refreshed_conversations?: number; deferred_conversations?: number }>('PUT', `/api/chat/models/${encodeURIComponent(name)}`, config),

  deleteModel: (name: string) =>
    request<{ ok: boolean; error?: string }>('DELETE', `/api/chat/models/${encodeURIComponent(name)}`),

  // 文件上传
  uploadFile: (conversation: string, fileName: string, fileDataBase64: string) =>
    request<{ ok: boolean; file_name: string; file_url: string; file_type: string; error?: string }>(
      'POST', '/api/chat/upload',
      { conversation, file_name: fileName, file_data: fileDataBase64 },
    ),

  // Retry / Fork
  listRuns: (conversation: string, cursor?: string, unfinished = false) =>
    request<{ ok: boolean; runs: AgentRun[]; next_cursor: string | null }>("GET", `/api/chat/runs?conversation=${encodeURIComponent(conversation)}&limit=20&unfinished=${unfinished}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
  manageRun: (conversation: string, runId: string, action: string, stepId?: string) =>
    request<{ ok: boolean; error?: string }>("POST", "/api/chat/runs/action", { conversation, run_id: runId, action, step_id: stepId }),
  retry: (conversation: string, think?: string) =>
    request<{ ok: boolean; turn_id: number; turn_index: number; variant_index: number; error?: string }>(
      'POST', '/api/chat/retry', { conversation, think },
    ),

  selectVariant: (conversation: string, turnIndex: number, variantIndex: number) =>
    request<{ ok: boolean; turn: ChatTurn; error?: string }>(
      'POST', '/api/chat/turns/variant',
      { conversation, turn_index: turnIndex, variant_index: variantIndex },
    ),

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
    request<{ ok: boolean; applied?: boolean; failed?: number; error?: string }>(
      'POST',
      `/api/chat/plugins/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`
    ),

  setPluginCapability: (name: string, kind: CapabilityKind, cap: string, enabled: boolean) =>
    request<{ ok: boolean; applied?: boolean; failed?: number; error?: string }>(
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
  listMemories: (scope: string) =>
    request<{ ok: boolean; memories: MemoryRecord[] }>('GET', `/api/chat/memories?scope=${encodeURIComponent(scope)}`),

  addMemory: (title: string, content: string, tags: string, importance: number, scope: string) =>
    request<{ ok: boolean; memory: MemoryRecord }>('POST', '/api/chat/memories', {
      title, content, tags, importance, scope,
    }),

  updateMemory: (id: string, fields: Partial<Pick<MemoryRecord, 'title' | 'content' | 'tags' | 'importance'>>, scope: string) =>
    request<{ ok: boolean; memory: MemoryRecord }>('PUT', `/api/chat/memories/${encodeURIComponent(id)}`, {
      ...fields, scope,
    }),

  deleteMemory: (id: string, scope: string) =>
    request<{ ok: boolean }>('DELETE', `/api/chat/memories/${encodeURIComponent(id)}?scope=${encodeURIComponent(scope)}`),
};

// ==================== WebSocket ====================

export type ChatEventHandler = (event: ChatEvent) => void;

// 订阅会话实时事件; 返回取消订阅函数
export function subscribeChat(conversationId: string, onEvent: ChatEventHandler): () => void {
  const wsUrl = `${getChatApiUrl().replace(/^http/, 'ws')}/ws/chat?conversation=${encodeURIComponent(conversationId)}`;
  let ws: WebSocket;
  let stopped = false;
  let retry = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const connect = () => {
    if (stopped) return;
    ws = new WebSocket(wsUrl);
    const current = ws;
    let sequence: number | undefined;
    let streamId: string | undefined;
    ws.onmessage = (e) => {
      if (stopped || current !== ws) return;
      try {
        const event = JSON.parse(e.data) as ChatEvent;
        if (event.type === 'snapshot') {
          sequence = event.seq;
          streamId = event.stream_id;
          retry = 0;
        } else if (event.type === 'resync_required') {
          current.close(1000, 'resync');
          return;
        } else if (event.seq !== undefined) {
          if (sequence === undefined || event.stream_id !== streamId || event.seq > sequence + 1) {
            current.close(1000, 'sequence gap');
            return;
          }
          if (event.seq <= sequence) return;
          sequence = event.seq;
        }
        onEvent(event);
      } catch (err) {
        console.error('[ChatWS] 解析消息失败:', err);
        current.close(1000, 'invalid event');
      }
    };
    ws.onclose = () => {
      if (!stopped && current === ws) {
        timer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
      }
    };
  };
  connect();
  return () => {
    stopped = true;
    clearTimeout(timer);
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      ws.close(1000, 'unsubscribe');
    }
  };
}

export interface AgentRun {
  id: string; status: string; created_at: number; updated_at: number; error?: string;
  steps: { step_key: string; kind: string; status: string; recovery_policy: string }[];
}
