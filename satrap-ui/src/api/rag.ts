import axios from 'axios';
import { errorText } from '@/utils/errorText';
import { ensureApiSession, establishApiSession } from './auth';
import { getChatApiUrl, getControlApiUrl } from '@/utils/constants';
import { chatApi } from './chat';
import { controlApi } from './control';
import type { ModelOptions } from '@/components/common/PluginConfigFields';

export interface RagContext { platformId: string; sessionId?: string; via?: 'control' }
export interface KnowledgeBase {
  id: string; name: string; scope: 'global' | 'session'; session_id: string;
  config: Record<string, unknown>; revision: number; is_default: number;
  status: string; last_error: string; document_count: number; chunk_count: number;
}
export interface RagDocument { source_id: string; source: string; chunk_count: number; content_hash: string }
export interface UploadCapabilities {
  extensions: string[];
  max_file_bytes: number;
  max_text_chars: number;
  missing_parsers: Record<string, string>;
  text_encoding: string;
}
export interface RagResult {
  ok: boolean;
  knowledge_bases: KnowledgeBase[];
  documents: RagDocument[];
  model_options: ModelOptions;
  upload: UploadCapabilities;
}

export function ragErrorMessage(reason: unknown): string {
  return errorText(reason, String(reason || '操作失败，请重试'), {
    httpFallback: (status) => `请求失败（HTTP ${status}），后端未返回具体原因`,
    timeoutMessage: '等待后端处理超时，任务可能仍在执行。请刷新知识库确认结果后再重试',
    networkMessage: '无法读取后端响应，请检查后端是否运行及网络连接、跨域配置。任务结果未知，请刷新知识库确认',
  });
}

export const ragApi = {
  upload: async (context: RagContext, kbId: string, file: File, source: string, onProgress: (percent: number) => void): Promise<Record<string, unknown>> => {
    const chat = context.platformId === 'chat' && context.via !== 'control';
    const base = chat ? getChatApiUrl() : getControlApiUrl();
    await ensureApiSession(base);
    const path = chat ? '/api/chat/rag/upload' : '/config/rag/upload';
    const params = new URLSearchParams({ platform_id: context.platformId, session_id: context.sessionId || '', kb_id: kbId, file_name: file.name, source });
    const send = () => axios.post<Record<string, unknown>>(base + path + '?' + params, file, {
      withCredentials: true, timeout: 300000,
      headers: { 'Content-Type': 'application/octet-stream' },
      onUploadProgress: (event) => onProgress(Math.min(100, Math.round(event.loaded * 100 / (event.total || file.size || 1)))),
    });
    try { return (await send()).data; }
    catch (error) {
      if (axios.isAxiosError(error) && error.response?.status === 401) {
        await establishApiSession(base);
        return (await send()).data;
      }
      throw error;
    }
  },
  list: (context: RagContext, kbId = ''): Promise<RagResult> => context.platformId === 'chat' && context.via !== 'control'
    ? chatApi.ragList(context.sessionId || '', kbId)
    : controlApi.ragList(context.platformId, context.sessionId || '', kbId),
  action: (context: RagContext, payload: Record<string, unknown>): Promise<Record<string, unknown>> => context.platformId === 'chat' && context.via !== 'control'
    ? chatApi.ragAction(context.sessionId || '', payload)
    : controlApi.ragAction(context.platformId, context.sessionId || '', payload),
};
