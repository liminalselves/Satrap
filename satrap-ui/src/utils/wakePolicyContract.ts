// 策略字段契约: 前端唯一的契约来源, 由 scripts/sync_wake_policy_contract.py 从后端契约表生成
// 字段类型/范围/枚举/作用域/覆盖关闭值都从这里读取; label 与 placeholder 的语义提示保留在各编辑器手写
import contract from '@/generated/wake-policy-contract.json';

export type PolicyFieldKind =
  | 'int'
  | 'number'
  | 'bool'
  | 'enum'
  | 'text'
  | 'list'
  | 'notice_types'
  | 'words'
  | 'group_ids'
  | 'scope'
  | 'group_map'
  | 'time_rules';

// platform: 仅平台级; group: 平台与群覆盖; time: 平台, 时段规则与群覆盖
export type PolicyFieldScope = 'platform' | 'group' | 'time';

export interface PolicyField {
  key: string;
  kind: PolicyFieldKind;
  scope: PolicyFieldScope;
  hot_reload: boolean;
  display_in_preview: boolean;
  // 在覆盖里表示"显式关闭"的取值; 缺失表示该字段没有关闭态
  off_value?: unknown;
  default?: unknown;
  enum?: string[];
  // 含上界; 与 max_exclusive 互斥, 都没有表示只有下界
  min?: number;
  max?: number;
  // 排他上界: HTML max 表达不了, 必须由校验器检查
  max_exclusive?: number;
  integer?: boolean;
  max_length?: number;
  max_items?: number;
  nullable?: boolean;
}

export interface PolicyIssue {
  key: string;
  message: string;
}

// 生成的 JSON 只有字段名与取值, 类型收窄在加载时一次性完成
interface RawPolicyField {
  key: string;
  kind: string;
  scope: string;
  hot_reload: boolean;
  display_in_preview: boolean;
  off_value?: unknown;
  default?: unknown;
  enum?: string[];
  min?: number;
  max?: number;
  max_exclusive?: number;
  integer?: boolean;
  max_length?: number;
  max_items?: number;
  nullable?: boolean;
}

const KIND_NAMES: PolicyFieldKind[] = [
  'int', 'number', 'bool', 'enum', 'text', 'list',
  'notice_types', 'words', 'group_ids', 'scope', 'group_map', 'time_rules',
];
const SCOPE_NAMES: PolicyFieldScope[] = ['platform', 'group', 'time'];
const KINDS = new Set<string>(KIND_NAMES);
const SCOPES = new Set<string>(SCOPE_NAMES);

function isKind(value: string): value is PolicyFieldKind {
  return KINDS.has(value);
}

function isScope(value: string): value is PolicyFieldScope {
  return SCOPES.has(value);
}

function toPolicyField(raw: RawPolicyField): PolicyField {
  if (!isKind(raw.kind)) throw new Error(`策略字段契约包含未知类型: ${raw.key}/${raw.kind}`);
  if (!isScope(raw.scope)) throw new Error(`策略字段契约包含未知作用域: ${raw.key}/${raw.scope}`);
  return { ...raw, kind: raw.kind, scope: raw.scope };
}

const RAW_FIELDS: RawPolicyField[] = contract.fields;

export const POLICY_FIELDS: PolicyField[] = RAW_FIELDS.map(toPolicyField);
export const POLICY_FIELD_BY_KEY: ReadonlyMap<string, PolicyField> = new Map(
  POLICY_FIELDS.map((field) => [field.key, field]),
);

// 校验提示里的字段名: 只用于拼接文案, 与表单 label 无关
export const POLICY_FIELD_LABELS: Record<string, string> = {
  message_text_limit: '每条消息文本上限',
  input_text_limit: '单条消息输入文本预算',
  input_media_limit: '单条消息输入媒体上限',
  enable_private: '私聊开关',
  enable_group: '群聊开关',
  group_whitelist: '群白名单',
  context_scope: '上下文范围',
  asr_model: '语音转写配置',
  voice_transcribe: '语音转写来源',
  attachment_extract: '提取附件正文',
  media_insecure_tls: '跳过 TLS 校验',
  media_plaintext_http: '允许明文 HTTP',
  media_trusted_hosts: '媒体主机列表',
  notice_types: '通知订阅类型',
  wake_mode: '自动参与模式',
  wake_message_threshold: '自动参与消息阈值',
  wake_cooldown: '自动参与冷却秒数',
  wake_max_wait: '最长等待秒数',
  wake_score_threshold: '必要性评分阈值',
  wake_question_weight: '问题权重',
  wake_address_weight: '指向性权重',
  wake_backlog_weight: '积压权重',
  wake_reply_penalty: '已回复惩罚',
  wake_talk_value: '发言频率偏好',
  wake_words: '唤醒词',
  wake_aliases: '机器人别名',
  reply_with_quote: '回复引用原消息',
  reply_with_mention: '回复 @发送者',
  quote_lookup: '回源引用原文',
  forward_lookup: '回源合并转发',
  wake_on_quote_self: '引用机器人时唤醒',
  wake_group_overrides: '群级唤醒覆盖',
  wake_time_rules: '时段自动参与规则',
};

export function policyField(key: string): PolicyField | undefined {
  return POLICY_FIELD_BY_KEY.get(key);
}

export function policyFieldLabel(key: string): string {
  return POLICY_FIELD_LABELS[key] ?? key;
}

export function overrideFieldKeys(context: 'group' | 'time'): string[] {
  return POLICY_FIELDS
    .filter((field) => field.scope === context || (context === 'group' && field.scope !== 'platform'))
    .map((field) => field.key);
}

export function previewFieldKeys(): string[] {
  return POLICY_FIELDS.filter((field) => field.display_in_preview).map((field) => field.key);
}

// 范围文本: 含上界 (1–32) / 排他上界 (0 ≤ x < 120) / 只有下界 (≥ 0)
export function rangeText(field: PolicyField): string | null {
  const min = field.min;
  if (min === undefined) return null;
  if (field.max !== undefined) return `${min}–${field.max}`;
  if (field.max_exclusive !== undefined) return `${min} ≤ x < ${field.max_exclusive}`;
  return `≥ ${min}`;
}

// 平台表单 label 里的范围文本: 语义前缀仍由表单手写, 范围与控件约束来自契约
export function formRangeText(key: string): string {
  const field = policyField(key);
  return (field ? rangeText(field) : null) ?? '';
}

export function formRangeSuffix(key: string): string {
  const range = formRangeText(key);
  return range ? `（${range}）` : '';
}

export function formNumericLimits(key: string): { min?: number; max?: number; step: number | 'any' } {
  const field = policyField(key);
  if (!field) return { step: 'any' };
  return {
    min: field.min,
    // 排他上界不能用 max 表达, 保留给校验器检查
    max: field.max,
    step: field.kind === 'int' || field.integer === true ? 1 : 'any',
  };
}

function numericError(field: PolicyField, label: string, value: unknown): string | null {
  const key = field.key;
  if (typeof value !== 'number' || !Number.isFinite(value)) return `${label}必须为数字（${key}）`;
  if ((field.kind === 'int' || field.integer === true) && !Number.isInteger(value)) {
    return `${label}必须为整数（${key}）`;
  }
  const min = field.min;
  const max = field.max;
  const maxExclusive = field.max_exclusive;
  const belowMin = min !== undefined && value < min;
  const aboveMax = max !== undefined && value > max;
  // 排他上界: 不能以 119 或任意 epsilon 代替, 由这里保证取值为 < 上界
  const atOrAboveExclusive = maxExclusive !== undefined && value >= maxExclusive;
  if (!belowMin && !aboveMax && !atOrAboveExclusive) return null;
  if (maxExclusive !== undefined) {
    return min === undefined
      ? `${label}必须小于 ${maxExclusive}（${key}）`
      : `${label}必须大于等于 ${min} 且小于 ${maxExclusive}（${key}）`;
  }
  if (min !== undefined && max !== undefined) return `${label}必须在 ${min} 到 ${max} 之间（${key}）`;
  if (min !== undefined) return `${label}必须大于等于 ${min}（${key}）`;
  return `${label}必须小于等于 ${String(max)}（${key}）`;
}

function textListError(field: PolicyField, label: string, value: unknown): string | null {
  const key = field.key;
  const maxItems = field.max_items === undefined ? '' : `最多 ${field.max_items} 项的`;
  if (!Array.isArray(value)) return `${label}必须是${maxItems}非空文本列表（${key}）`;
  if (field.max_items !== undefined && value.length > field.max_items) {
    return `${label}必须是${maxItems}非空文本列表（${key}）`;
  }
  const invalid = value.some((item) => typeof item !== 'string' || !item.trim()
    || (field.max_length !== undefined && item.length > field.max_length));
  return invalid ? `${label}必须是${maxItems}非空文本列表（${key}）` : null;
}

/**
 * 严格校验单个字段取值
 *
 * 只接受规范 JSON 取值 (数字/布尔/字符串/数组), 不做 Number()/Boolean() 之类的宽松转换;
 * 表单里的数字字符串与空白先经 normalizePlatformSettings 归一化, 再调用本函数
 */
export function validatePolicyValue(
  field: PolicyField,
  value: unknown,
  label = policyFieldLabel(field.key),
): string | null {
  if (value === null) {
    return field.nullable ? null : `${label}不能为空（${field.key}）`;
  }
  switch (field.kind) {
    case 'int':
    case 'number':
      return numericError(field, label, value);
    case 'bool':
      return typeof value === 'boolean' ? null : `${label}必须为布尔值（${field.key}）`;
    case 'enum':
      return typeof value === 'string' && (field.enum ?? []).includes(value)
        ? null
        : `${label}必须为 ${(field.enum ?? []).join(' 或 ')}（${field.key}）`;
    case 'text':
      return typeof value === 'string' && (field.max_length === undefined || value.length <= field.max_length)
        ? null
        : `${label}必须是不超过 ${String(field.max_length)} 字符的文本（${field.key}）`;
    case 'list':
    case 'words':
      return textListError(field, label, value);
    case 'notice_types': {
      const maxItems = field.max_items ?? 64;
      if (!Array.isArray(value) || value.length > maxItems) {
        return `${label}必须是最多 ${maxItems} 项的 notice/request 列表（${field.key}）`;
      }
      const invalid = value.some((item) => typeof item !== 'string' || !/^(notice|request)(\.[a-z_]+)?$/.test(item));
      return invalid ? `${label}必须是最多 ${maxItems} 项的 notice/request 列表（${field.key}）` : null;
    }
    default:
      // 群白名单, 上下文范围与覆盖结构由后端专用校验器和读取点负责, 前端不复制其归一化
      return null;
  }
}

/**
 * 按上下文校验一组字段取值
 *
 * 参数:
 * - context: platform 校验平台设置 (未知扩展字段透传), group/time 校验一条覆盖 (未知字段拒绝)
 * - settings: 已归一化的设置或覆盖对象
 * - labelOf: 覆盖编辑器等场景可覆盖字段名文案
 */
export function validatePolicySettings(
  context: 'platform' | 'group' | 'time',
  settings: Record<string, unknown>,
  labelOf: (key: string) => string = policyFieldLabel,
): PolicyIssue[] {
  const issues: PolicyIssue[] = [];
  if (context === 'platform') {
    for (const field of POLICY_FIELDS) {
      if (!(field.key in settings)) continue;
      const message = validatePolicyValue(field, settings[field.key], labelOf(field.key));
      if (message) issues.push({ key: field.key, message });
    }
    return issues;
  }
  const allowed = new Set(overrideFieldKeys(context));
  const contextLabel = context === 'group' ? '群覆盖' : '时段规则';
  for (const [key, value] of Object.entries(settings)) {
    const field = POLICY_FIELD_BY_KEY.get(key);
    if (!field || !allowed.has(key)) {
      issues.push({ key, message: `字段 ${key} 不能出现在${contextLabel}中` });
      continue;
    }
    // 缺失表示继承, 显式 null 与默认值注入都按取值校验处理
    const message = validatePolicyValue(field, value, labelOf(key));
    if (message) issues.push({ key, message });
  }
  return issues;
}

export function policyIssuesMessage(issues: PolicyIssue[]): string | null {
  return issues.length ? issues.map((issue) => issue.message).join('; ') : null;
}
