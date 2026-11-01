import type { ConversationModel } from '@/api/chat';

export type BindingProblemKind = 'temporary' | 'unavailable';

export interface ModelBindingProblem {
  kind: BindingProblemKind;
  message: string;
}

/** 将 getModel 结果区分为暂时无法绑定与已确认模型不可用 */
export function modelBindingProblem(result: ConversationModel): ModelBindingProblem | null {
  if (!result.ok) {
    return { kind: 'temporary', message: result.error || '读取会话模型失败' };
  }
  if (result.available === false) {
    return { kind: 'unavailable', message: `模型 ${result.model} 不可用, 请切换模型或检查模型配置` };
  }
  return null;
}

/** 读取会话模型请求本身失败时保留可重试的暂时错误 */
export function bindingFailureProblem(error: unknown): ModelBindingProblem {
  return {
    kind: 'temporary',
    message: `读取会话模型失败: ${error instanceof Error ? error.message : String(error)}`,
  };
}

/**
 * 权威来源恢复或确认不可用时更新绑定问题
 *
 * model_changed 带回可用性, snapshot 只带回绑定模型;
 * snapshot 未携带模型时不能清除当前问题
 */
export function bindingProblemFromAuthoritative(
  model: string | null | undefined,
  available: boolean | undefined,
  current: ModelBindingProblem | null,
): ModelBindingProblem | null {
  if (!model) return current;
  if (available === false) {
    return { kind: 'unavailable', message: `模型 ${model} 不可用, 请切换模型或检查模型配置` };
  }
  return null;
}

export interface ModelReadinessInput {
  isDraft: boolean;
  hasDefaults: boolean;
  model: string;
  modelsLoaded: boolean;
  models: readonly string[];
  bindingPending: boolean;
  bindingProblem: ModelBindingProblem | null;
}

export interface ModelReadiness {
  ready: boolean;
  reason: 'defaults' | 'binding' | 'busy' | 'unavailable' | null;
  problem: ModelBindingProblem | null;
}

/**
 * 计算当前会话能否发送
 *
 * 草稿依赖整体默认模型; 已有会话只依赖自身绑定, 不因整体默读取失败而阻塞;
 * 模型目录尚未读取时不猜测不可用, 读取成功且缺少当前模型才算确认不可用
 */
export function resolveModelReadiness(input: ModelReadinessInput): ModelReadiness {
  if (input.isDraft && !input.hasDefaults) {
    return {
      ready: false,
      reason: 'defaults',
      problem: { kind: 'temporary', message: '整体默认模型尚未读取完成' },
    };
  }
  if (input.bindingProblem) {
    return {
      ready: false,
      reason: input.bindingProblem.kind === 'unavailable' ? 'unavailable' : 'binding',
      problem: input.bindingProblem,
    };
  }
  if (input.bindingPending) {
    return { ready: false, reason: 'busy', problem: null };
  }
  if (input.modelsLoaded && !input.models.includes(input.model)) {
    return {
      ready: false,
      reason: 'unavailable',
      problem: { kind: 'unavailable', message: `模型 ${input.model} 不可用, 请切换模型或检查模型配置` },
    };
  }
  return { ready: true, reason: null, problem: null };
}

export interface AttachmentModelCapabilities {
  supports_visual_input?: boolean;
  supports_native_video_input?: boolean;
}

/**
 * 判断附件是否被模型已确认缺失的能力阻止
 *
 * 能力未知时不阻止, 由后端在发送时校验; 只有明确声明不支持才拦下对应媒体
 */
export function attachmentCapabilityBlocked(
  type: string | undefined,
  capabilities: AttachmentModelCapabilities | undefined,
): boolean {
  const kind = type ?? '';
  if (kind.startsWith('image/')) return capabilities?.supports_visual_input === false;
  if (kind.startsWith('video/')) {
    return capabilities?.supports_visual_input === false
      && capabilities?.supports_native_video_input === false;
  }
  return false;
}
