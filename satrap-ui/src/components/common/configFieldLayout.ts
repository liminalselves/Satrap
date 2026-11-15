import type { EdictumPluginConfigField } from '@/api/types';

/** 按分组排好的字段段落; 无分组的字段 group 为空串, 排在最前 */
export interface ConfigFieldSection {
  /** 分组名; 空串表示未分组 */
  group: string;
  /** 本段字段, 保持 schema 声明顺序 */
  fields: Array<[string, EdictumPluginConfigField]>;
}

/**
 * 计算字段在当前取值下是否显示
 *
 * visible_when 的每一项都要满足: 期望值是数组时命中其中任意一个即可, 否则严格相等;
 * 只影响显示, 隐藏字段的值保留并由后端照常校验
 */
export function fieldVisible(field: EdictumPluginConfigField, effective: Record<string, unknown>): boolean {
  const conditions = field.visible_when;
  if (!conditions) return true;
  return Object.entries(conditions).every(([other, expected]) => {
    const actual = effective[other];
    return Array.isArray(expected) ? expected.some((item) => item === actual) : expected === actual;
  });
}

/** 按 group 分段, 未分组字段在最前, 其余分组按首次出现顺序 */
export function groupFields(fields: Array<[string, EdictumPluginConfigField]>): ConfigFieldSection[] {
  const sections: ConfigFieldSection[] = [];
  const byGroup = new Map<string, ConfigFieldSection>();
  const ungrouped: ConfigFieldSection = { group: '', fields: [] };
  for (const entry of fields) {
    const group = entry[1].group?.trim() || '';
    if (!group) {
      ungrouped.fields.push(entry);
      continue;
    }
    let section = byGroup.get(group);
    if (!section) {
      section = { group, fields: [] };
      byGroup.set(group, section);
      sections.push(section);
    }
    section.fields.push(entry);
  }
  return ungrouped.fields.length ? [ungrouped, ...sections] : sections;
}

/** 与后端 time 字段同口径: HH:MM, 00:00 到 24:00 (24 点只允许 24:00) */
export function validTime(value: unknown): boolean {
  if (typeof value !== 'string') return false;
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  if (!match) return false;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour === 24) return minute === 0;
  return hour >= 0 && hour < 24 && minute >= 0 && minute < 60;
}

/** 字段的生效值: 显式值 > 继承值 > schema 默认 */
export function effectiveValues(
  schema: Record<string, EdictumPluginConfigField>,
  values: Record<string, unknown>,
  inherited: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, field] of Object.entries(schema)) {
    out[key] = Object.prototype.hasOwnProperty.call(values, key) ? values[key]
      : Object.prototype.hasOwnProperty.call(inherited, key) ? inherited[key] : field.default;
  }
  return out;
}
