import type { DebugEvent, DebugRequest, DiagnosticLevel } from '@/api/debugLogging';

export type DiagnosticTone = 'normal' | 'warning' | 'error' | 'neutral' | 'unknown';
// 外框颜色是诊断语义的唯一载体, 四类在深浅主题下都必须可区分
export const diagnosticBorders: Record<DiagnosticTone, string> = {
  normal: 'border-2 !border-[rgb(var(--rgb-accent)/0.55)]',
  warning: 'border-2 !border-[rgb(var(--rgb-orange)/0.7)]',
  error: 'border-2 !border-[rgb(var(--rgb-red)/0.7)]',
  neutral: 'border-2 !border-[var(--glass-border-strong)]',
  unknown: 'border-2 !border-[var(--glass-border-strong)]',
};

/** 诊断类别对应的染色, 用于图标, 侧条与提示胶囊 */
export const diagnosticTints: Record<DiagnosticTone, string> = {
  normal: 'tint-accent',
  warning: 'tint-orange',
  error: 'tint-red',
  neutral: '',
  unknown: '',
};
const neutralStatuses = new Set(['cancelled', 'skipped', 'pending', 'ready', 'waiting', 'needs_attention', 'ask', 'deny', 'rejected', 'expired']);
const categories: Record<string, string> = {
  request: '请求', queue: '排队', projection: '输入投影', projection_input: '模型输入准备', context: '上下文绑定',
  context_preparation: '上下文处理', context_reset: '上下文重置', context_fork: '创建分支', context_variant: '切换回复版本',
  session: '会话执行', session_result: '会话返回', model: '模型调用', tool: '工具调用', tool_result: '工具返回',
  approval: '执行审批', platform_approval: '平台审批', delivery: '平台发送', reply: '生成回复', chat_delivery: 'Chat 交付',
  chat: 'Chat 请求', execution_run: '工作流', preprocessor: '前处理', request_error: '请求异常',
};
const states: Record<string, string> = {
  completed: '已完成', success: '成功', ok: '成功', returned: '已返回', accepted: '已接受', failed: '失败', timeout: '超时',
  cancelled: '已取消', skipped: '未执行', pending: '待审批', ready: '待执行', waiting: '等待中', needs_attention: '待处理',
  ask: '等待确认', deny: '已拒绝', rejected: '已拒绝', expired: '已过期', queued: '已排队', saved: '已保存', deleted: '已删除',
  already_exists: '已存在', approved: '已批准', unknown: '结果未确认', partial: '部分完成', interrupted: '未确认中断',
  sent: '已发送', published: '已发布', uploaded: '已上传',
};

/**
 * 从明确分类选择颜色, 中性状态不覆盖已确认的问题级别
 *
 * 参数:
 * - level: 后端确认的问题级别, 旧接口可能没有此字段
 * - status: 执行结果或等待状态
 * - incomplete: 是否存在观测缺口, 默认 false
 *
 * 返回:
 * - 独立于执行结果的展示颜色类别
 */
export function diagnosticTone(level: DiagnosticLevel | undefined, status: string, incomplete = false): DiagnosticTone {
  if (level === 'error') return 'error';
  if (level === 'warning' || incomplete) return 'warning';
  if (neutralStatuses.has(status)) return 'neutral';
  return level === 'info' ? 'normal' : 'unknown';
}

/**
 * 读取有界的标题字段, 不解析工具或模型正文
 *
 * 参数:
 * - value: 待展示的摘要值
 *
 * 返回:
 * - 最多 120 字符的文本, 非字符串返回空字符串
 */
function text(value: unknown): string {
  return typeof value === 'string' ? value.slice(0, 120) : '';
}

/**
 * 保留业务终态, 另行提示链路中发生过的问题
 *
 * 参数:
 * - request: 请求摘要及最高问题级别
 *
 * 返回:
 * - 问题提示, 普通请求返回空字符串
 */
export function requestHint(request: DebugRequest): string {
  if (request.diagnostic_level === 'error') return '含系统错误';
  if (request.incomplete) return '记录不完整';
  if (request.diagnostic_level === 'warning') return '含调用问题';
  return !request.diagnostic_level || request.diagnostic_level === 'unknown' ? '旧记录未分类' : '';
}

/**
 * 返回事件标题和可访问的颜色说明, 缺少分类时不猜测历史错误原因
 *
 * 参数:
 * - event: 不包含正文的事件摘要
 *
 * 返回:
 * - 标题, 外框类别和辅助文字标签
 */
export function eventPresentation(event: DebugEvent): { title: string; tone: DiagnosticTone; label: string } {
  const raw = event.metadata.diagnostic_level;
  const level = raw === 'info' || raw === 'warning' || raw === 'error' || raw === 'unknown' ? raw : undefined;
  const status = text(event.metadata.result_status) || event.status;
  const tone = diagnosticTone(level, status);
  const kind = text(event.metadata.error_kind), code = text(event.metadata.error_code);
  const problem = kind === 'model_input' ? code === 'not_found' ? '工具不存在' : '参数或调用错误'
    : kind === 'business_rejection' ? code === 'revision_conflict' ? '版本冲突' : '调用被拒绝'
      : kind === 'observation_gap' ? '记录不完整' : kind === 'uncertain_result' ? '结果待确认'
        : tone === 'error' ? '系统错误' : tone === 'warning' ? '未分类失败' : '';
  const stage = problem ? (status === 'partial' ? `部分完成 · ${problem}` : problem)
    : states[status] || (event.phase === 'start' ? '开始' : event.phase === 'end' ? '结束' : '更新');
  const name = text(event.metadata.name) || text(event.metadata.model);
  return { title: [categories[event.category] || event.category, name, stage].filter(Boolean).join(' · '), tone,
    label: problem || (tone === 'unknown' ? '旧记录未分类' : tone === 'neutral' ? states[status] || status : '正常') };
}

/**
 * 将浏览增量转换为可读摘要, 完整快照仍由独立入口读取
 *
 * 参数:
 * - value: 已授权的模型输入增量响应
 *
 * 返回:
 * - 默认正文及变化范围说明, 旧格式直接显示原始资料
 */
export function inputDeltaPresentation(value: unknown): { text: string; summary?: string } {
  if (!value || typeof value !== 'object' || !('view' in value) || value.view !== 'delta'
    || !('messages' in value) || !Array.isArray(value.messages)) return { text: JSON.stringify(value, null, 2) };
  const data = value as Record<string, unknown>;
  const count = (key: string) => typeof data[key] === 'number' ? data[key] : 0;
  const summary = data.basis === 'previous_model_input'
    ? `相对本次请求的上一次模型输入: 新增或变更 ${value.messages.length} 条, 移除 ${count('removed_messages')} 条, 隐藏未变消息 ${Number(count('hidden_prefix_messages')) + Number(count('hidden_suffix_messages'))} 条`
    : data.basis === 'initial_input' ? `首次输入没有用户消息分界, 展示当前 ${value.messages.length} 条`
      : `首次输入: 从最近一条用户消息开始展示 ${value.messages.length} 条, 更早历史 ${count('hidden_prefix_messages')} 条已隐藏`;
  return { summary: summary + (data.baseline_unavailable ? '; 对比基线不可用, 已改为展示最近输入' : ''),
    text: JSON.stringify({ messages: value.messages, changed_parameters: data.changed_parameters, removed_parameters: data.removed_parameters }, null, 2) };
}
