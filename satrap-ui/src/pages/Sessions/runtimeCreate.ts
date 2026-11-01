import type { AgentEntry } from './shared';

export interface CreateSessionInput {
  session_id: string;
  adapter_id: string;
  llm_name: string;
  params: Record<string, unknown>;
  // 多平台绑定时在同一个创建表单里选择目标
  platform_id?: string;
}

export interface RuntimeCreateOptions {
  platformId?: string;
  running: boolean;
}

export interface RuntimeCreatePayload {
  session_provider: 'edictum' | 'session_class';
  session_type: string;
  session_id?: string;
  platform_id?: string;
  adapter_id?: string;
  llm_name?: string;
  params: Record<string, unknown>;
  activate?: boolean;
}

/**
 * 构造创建运行时会话的请求体
 *
 * Edictum 与会话类都透传表单中的 session_id, llm_name 与 params;
 * Edictum 的目标平台可来自多平台表单, 未选择时回退到调用的默认平台
 */
export function buildCreateRuntimePayload(
  agent: AgentEntry,
  data: CreateSessionInput,
  options: RuntimeCreateOptions,
): RuntimeCreatePayload {
  const sessionId = data.session_id.trim() || undefined;
  const llmName = data.llm_name.trim() || undefined;
  if (agent.provider === 'edictum') {
    const platformId = data.platform_id || options.platformId;
    return {
      session_provider: 'edictum',
      session_type: agent.name,
      ...(sessionId ? { session_id: sessionId } : {}),
      ...(platformId ? { platform_id: platformId, adapter_id: platformId } : {}),
      ...(llmName ? { llm_name: llmName } : {}),
      params: data.params,
      activate: options.running,
    };
  }
  return {
    session_provider: 'session_class',
    session_type: agent.name,
    ...(sessionId ? { session_id: sessionId } : {}),
    ...(data.adapter_id.trim() ? { adapter_id: data.adapter_id.trim() } : {}),
    ...(llmName ? { llm_name: llmName } : {}),
    params: data.params,
  };
}

/** 插件管理目标只能来自 Edictum 配置, 同名会话类不影响解析 */
export function resolveEdictumPluginTarget(
  name: string | null,
  edictumAgents: readonly AgentEntry[],
): AgentEntry | null {
  if (!name) return null;
  return edictumAgents.find((agent) => agent.provider === 'edictum' && agent.name === name) ?? null;
}
