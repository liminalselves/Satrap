// 群/时段唤醒覆盖的行编辑模型: 继承 / 显式关闭 / 显式值 三态与 JSON 互转
// 与后端 satrap/core/config/wake_overrides.py 的 GROUP_KEYS / AUTOMATIC_KEYS 对齐

export type OverrideState = 'inherit' | 'off' | 'value';

export interface OverrideFieldDef {
  key: string;
  label: string;
  kind: 'select' | 'number' | 'bool' | 'lines';
  options?: { value: string; label: string }[];
  // 显式关闭时写入的值; undefined 表示该字段不支持"关闭"态
  offValue?: unknown;
  offLabel?: string;
  placeholder?: string;
  min?: number;
  max?: number;
  // 仅群覆盖可用 (时段规则只允许自动参与参数)
  groupOnly?: boolean;
}

export const OVERRIDE_FIELDS: OverrideFieldDef[] = [
  {
    key: 'wake_mode', label: '自动参与模式', kind: 'select',
    options: [
      { value: 'frequency', label: '按消息数量触发' },
      { value: 'necessity', label: '按必要性评分' },
    ],
    offValue: 'explicit', offLabel: '关闭自动参与',
  },
  { key: 'wake_talk_value', label: '发言频率偏好 (0–1)', kind: 'number', offValue: 0, offLabel: '不自动参与', min: 0, max: 1, placeholder: '0.05–1' },
  { key: 'wake_message_threshold', label: '消息条数阈值', kind: 'number', min: 1, max: 32, placeholder: '1–32' },
  { key: 'wake_cooldown', label: '冷却秒数', kind: 'number', min: 0, max: 86400 },
  { key: 'wake_score_threshold', label: '必要性评分阈值', kind: 'number', min: 0, max: 1, placeholder: '0–1' },
  { key: 'wake_max_wait', label: '最长等待秒数', kind: 'number', min: 0, max: 119, placeholder: '0 关闭' },
  { key: 'wake_question_weight', label: '问题权重', kind: 'number', min: 0, max: 1 },
  { key: 'wake_address_weight', label: '指向性权重', kind: 'number', min: 0, max: 1 },
  { key: 'wake_backlog_weight', label: '积压权重', kind: 'number', min: 0, max: 1 },
  { key: 'wake_reply_penalty', label: '已回复惩罚', kind: 'number', min: 0, max: 1 },
  { key: 'wake_words', label: '唤醒词', kind: 'lines', offValue: [], offLabel: '清空继承词表', groupOnly: true, placeholder: '每行一个' },
  { key: 'wake_aliases', label: '机器人别名', kind: 'lines', offValue: [], offLabel: '清空继承别名', groupOnly: true, placeholder: '每行一个' },
  { key: 'reply_with_quote', label: '回复引用原消息', kind: 'bool', offValue: false, groupOnly: true },
  { key: 'reply_with_mention', label: '回复 @发送者', kind: 'bool', offValue: false, groupOnly: true },
  { key: 'quote_lookup', label: '回源引用原文', kind: 'bool', offValue: false, groupOnly: true },
  { key: 'forward_lookup', label: '回源合并转发', kind: 'bool', offValue: false, groupOnly: true },
  { key: 'wake_on_quote_self', label: '引用机器人时唤醒', kind: 'bool', offValue: false, groupOnly: true },
];

export const GROUP_OVERRIDE_FIELDS = OVERRIDE_FIELDS;
export const TIME_RULE_FIELDS = OVERRIDE_FIELDS.filter((field) => !field.groupOnly);

export interface GroupOverrideRow {
  id: string;
  override: Record<string, unknown>;
}

export interface TimeRuleRow {
  start: string;
  end: string;
  settings: Record<string, unknown>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

// 兼容历史 JSON 文本形态, 解析失败回退为空
export function parseOverrideJson(value: unknown, fallback: unknown): unknown {
  if (typeof value !== 'string') return value;
  if (!value.trim()) return fallback;
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return fallback;
  }
}

// 数组 offValue (清空词表 []) 必须按内容比较: 解析后的数组与定义中的字面量不是同一引用
function isOffValue(value: unknown, offValue: unknown): boolean {
  if (value === offValue) return true;
  if (Array.isArray(value) && Array.isArray(offValue)) {
    return value.length === offValue.length && value.every((item, index) => item === offValue[index]);
  }
  return false;
}

export function fieldState(override: Record<string, unknown>, def: OverrideFieldDef): OverrideState {
  if (!(def.key in override)) return 'inherit';
  if (def.offValue !== undefined && isOffValue(override[def.key], def.offValue)) return 'off';
  return 'value';
}

export function fieldValue(override: Record<string, unknown>, def: OverrideFieldDef): unknown {
  const value = override[def.key];
  if (value !== undefined) return value;
  if (def.kind === 'number') return def.min ?? 1;
  if (def.kind === 'select') return def.options?.[0]?.value ?? '';
  if (def.kind === 'lines') return [];
  return true;
}

export function applyField(
  override: Record<string, unknown>,
  def: OverrideFieldDef,
  state: OverrideState,
  value?: unknown,
): Record<string, unknown> {
  const next = { ...override };
  if (state === 'inherit') delete next[def.key];
  else if (state === 'off') next[def.key] = def.offValue;
  else next[def.key] = value === undefined || Number.isNaN(value) ? fieldValue(override, def) : value;
  return next;
}

export function toGroupRows(value: unknown): GroupOverrideRow[] {
  const parsed = parseOverrideJson(value, {});
  if (!isRecord(parsed)) return [];
  return Object.entries(parsed)
    .filter(([, override]) => isRecord(override))
    .map(([id, override]) => ({ id, override: { ...(override as Record<string, unknown>) } }));
}

export function fromGroupRows(rows: GroupOverrideRow[]): Record<string, Record<string, unknown>> {
  const result: Record<string, Record<string, unknown>> = {};
  for (const row of rows) {
    const id = row.id.trim();
    if (!/^[1-9]\d*$/.test(id) || id in result) continue;
    if (Object.keys(row.override).length === 0) continue;
    result[id] = row.override;
  }
  return result;
}

export function toTimeRows(value: unknown): TimeRuleRow[] {
  const parsed = parseOverrideJson(value, []);
  if (!Array.isArray(parsed)) return [];
  const rows: TimeRuleRow[] = [];
  for (const item of parsed) {
    if (!isRecord(item)) continue;
    rows.push({
      start: typeof item.start === 'string' ? item.start : '',
      end: typeof item.end === 'string' ? item.end : '',
      settings: isRecord(item.settings) ? { ...item.settings } : {},
    });
  }
  return rows;
}

const TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

export function fromTimeRows(rows: TimeRuleRow[]): Array<{ start: string; end: string; settings: Record<string, unknown> }> {
  const result: Array<{ start: string; end: string; settings: Record<string, unknown> }> = [];
  for (const row of rows) {
    if (!TIME_PATTERN.test(row.start) || !TIME_PATTERN.test(row.end) || row.start === row.end) continue;
    if (Object.keys(row.settings).length === 0) continue;
    result.push({ start: row.start, end: row.end, settings: row.settings });
  }
  return result;
}
