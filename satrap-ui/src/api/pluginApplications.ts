import { apiClient } from './client';
import { ApplicationUnconfirmedError, RuntimeApplication } from './runtimeApplication';
import { getControlApiUrl } from '@/utils/constants';
import type { PluginRuntimeResult } from './types';

export interface PluginApplicationResult {
  ok: boolean;
  runtime: PluginRuntimeResult[];
}

export interface PluginApplicationScope {
  plugin_name?: string;
  kind?: 'chat' | 'edictum';
  location_id?: string;
}

export interface PluginSaveOptions {
  applyRuntime?: boolean;
  baseUrl?: string;
  signal?: AbortSignal;
}

/**
 * 创建绑定原控制服务的插件应用任务, 保存接口返回 runtime 时无需启动此任务
 * 参数:
 * - scope: 插件及可选使用位置, 缺省位置时协调该插件的全部使用位置
 * 返回:
 * - 可提交一次并重复查询的任务, 不保存配置正文
 */
export function createPluginApplication(scope: PluginApplicationScope) {
  return new RuntimeApplication<PluginApplicationResult>('/config/plugins/reconcile', { ...scope },
    (value): value is PluginApplicationResult => Boolean(value && typeof value === 'object'
      && typeof (value as PluginApplicationResult).ok === 'boolean' && Array.isArray((value as PluginApplicationResult).runtime)
      && (value as PluginApplicationResult).runtime.every((row) => row && typeof row.target === 'string'
        && ['applied', 'next_activation', 'error'].includes(row.status) && Array.isArray(row.sessions))),
    [scope.plugin_name, scope.kind === 'chat' ? 'Chat' : scope.location_id].filter(Boolean).join(' / '),
    { currentService: getControlApiUrl, queryPrefix: '/config/plugin-operations/' });
}

/**
 * 仅保存插件配置, 在原控制服务执行原 revision 事务
 * 参数:
 * - path: 原全局参数或使用位置保存路径
 * - body: 原保存正文, 保留配置及 expected_revision
 * - options: 原服务地址及页面生命周期, 地址缺省取当前控制服务
 * 返回:
 * - 原保存结果; 旧服务可能附带 runtime, 换服务或页面离开则不确认迟到结果
 */
export async function savePluginConfigOnly<T>(path: string, body: Record<string, unknown>, options: PluginSaveOptions): Promise<T> {
  const baseUrl = (options.baseUrl || getControlApiUrl()).replace(/\/$/, '');
  const check = () => {
    if (options.signal?.aborted) throw new ApplicationUnconfirmedError('状态查询已停止, 生效状态未确认', 'query_stopped');
    if (getControlApiUrl().replace(/\/$/, '') !== baseUrl) throw new ApplicationUnconfirmedError('控制服务地址已变化, 原服务的保存状态未确认');
  };
  check();
  try {
    const result = await apiClient.forService(baseUrl, getControlApiUrl).put<T>(path, { ...body, apply_runtime: false }, options.signal);
    check();
    return result;
  } catch (error) {
    check();
    throw error;
  }
}
