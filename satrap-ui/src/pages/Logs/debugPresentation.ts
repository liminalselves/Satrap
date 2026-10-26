import type { DebugEvent, DebugRequest, DiagnosticLevel } from '@/api/debugLogging';

export type DiagnosticTone = 'normal' | 'warning' | 'error' | 'neutral' | 'unknown';
export const diagnosticBorders: Record<DiagnosticTone, string> = {
  normal: 'border-2 border-blue-500/70 dark:border-blue-400',
  warning: 'border-2 border-amber-500/80 dark:border-amber-400',
  error: 'border-2 border-red-500/80 dark:border-red-400',
  neutral: 'border-2 border-slate-400/60 dark:border-slate-500',
  unknown: 'border-2 border-slate-400/60 dark:border-slate-500',
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
