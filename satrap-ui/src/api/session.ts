import { apiClient } from './client';
import { controlApi } from './control';
import type { RuntimeSession } from './types';

interface DeleteRuntimeSessionsResult {
  ok: boolean;
  deleted_count: number;
  deleted_ids: string[];
  deleted_refs?: Array<{ platform_id: string; session_id: string }>;
}

export const sessionApi = {
  // 列出会话类配置
  list: () => controlApi.listSessionClasses(),

  // 获取单个会话类配置
  get: (name: string) => controlApi.getSessionClass(name),

  // 注册会话类
  register: (data: {
    name: string;
    class_path: string;
    is_async?: boolean;
    enabled?: boolean;
    description?: string;
    context_key?: string;
    model_key?: string;
    params?: Record<string, unknown>;
  }) => controlApi.createSessionClass(data),

  // 启用会话类
  enable: (name: string) =>
    controlApi.setSessionClassEnabled(name, true),

  // 禁用会话类
  disable: (name: string) =>
    controlApi.setSessionClassEnabled(name, false),

  // 更新会话类参数
  updateParams: (name: string, params: Record<string, unknown>) =>
    controlApi.updateSessionClass(name, { params }),

  // 更新会话类配置
  update: (name: string, data: {
    name?: string;
    class_path?: string;
    is_async?: boolean;
    enabled?: boolean;
    params?: Record<string, unknown>;
    description?: string;
    context_key?: string;
    model_key?: string;
  }) => controlApi.updateSessionClass(name, data),

  // 注销会话类
  unregister: (name: string) =>
    controlApi.deleteSessionClass(name),

  // 扫描会话类
  discover: (path?: string) => controlApi.discoverSessionClasses(path),

  // 创建配置中的扫描目录
  createScanDirectory: (path?: string) => controlApi.createSessionScanDirectory(path),

  // 按后端状态列出热管理或冷管理会话实例
  listRuntime: (backendRunning = true) => backendRunning
    ? apiClient.get<{ sessions: RuntimeSession[] }>('/api/sessions')
    : controlApi.listSessionInstances(),

  // 创建运行时会话配置
  createRuntime: (data: {
    class_name?: string;
    session_provider?: string;
    session_type?: string;
    session_id?: string;
    platform_id?: string;
    adapter_id?: string;
    llm_name?: string;
    params?: Record<string, unknown>;
    activate?: boolean;
  }, backendRunning = true) => backendRunning
    ? apiClient.post<{ ok: boolean; session: RuntimeSession }>('/api/sessions', data)
    : controlApi.createSessionInstance(data),

  // 删除单个会话实例及关联引用
  deleteRuntime: (platformId: string, sessionId: string, backendRunning = true) => backendRunning
    ? apiClient.delete<DeleteRuntimeSessionsResult>(
      `/api/sessions/${encodeURIComponent(sessionId)}?platform_id=${encodeURIComponent(platformId)}`,
    )
    : controlApi.deleteSessionInstance(platformId, sessionId),

  // 按选择或消息数批量删除会话实例
  bulkDeleteRuntime: (data: {
    mode: 'empty' | 'single' | 'selected';
    session_refs?: Array<{ platform_id: string; session_id: string }>;
  }, backendRunning = true) => backendRunning
    ? apiClient.post<DeleteRuntimeSessionsResult>('/api/sessions/bulk-delete', data)
    : controlApi.bulkDeleteSessionInstances(data),

  // 卸载并按冷配置重新激活会话, 不创建新的持久化实例
  restartRuntime: (platformId: string, sessionId: string) => apiClient.post<{
    ok: boolean;
    runtime: RuntimeSession['runtime'];
  }>(
    `/api/sessions/${encodeURIComponent(sessionId)}/restart?platform_id=${encodeURIComponent(platformId)}`,
  ),
};
