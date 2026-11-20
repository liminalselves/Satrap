import { describe, expect, it } from 'vitest';

import type { EdictumPluginConfigField } from '@/api/types';
import { effectiveValues, fieldVisible, groupFields, validTime } from './configFieldLayout';

const field = (extra: Partial<EdictumPluginConfigField> = {}): EdictumPluginConfigField => ({
  type: 'string', default: '', description: '', ...extra,
});

describe('configFieldLayout', () => {
  it('visible_when 单值严格相等, 列表命中任意一个即显示', () => {
    const single = field({ visible_when: { check: 'version' } });
    const multi = field({ visible_when: { kind: ['keyword', 'sender'] } });
    expect(fieldVisible(single, { check: 'version' })).toBe(true);
    expect(fieldVisible(single, { check: 'nonempty' })).toBe(false);
    expect(fieldVisible(multi, { kind: 'sender' })).toBe(true);
    expect(fieldVisible(multi, { kind: 'mention' })).toBe(false);
    expect(fieldVisible(field(), {})).toBe(true);
  });

  it('visible_when 按生效值判断: 显式值 > 继承 > 默认', () => {
    const schema = {
      check: field({ type: 'select', default: 'nonempty' }),
      min_version: field({ type: 'number', default: 1, visible_when: { check: 'version' } }),
    };
    expect(fieldVisible(schema.min_version, effectiveValues(schema, {}, {}))).toBe(false);
    expect(fieldVisible(schema.min_version, effectiveValues(schema, {}, { check: 'version' }))).toBe(true);
    expect(fieldVisible(schema.min_version, effectiveValues(schema, { check: 'nonempty' }, { check: 'version' }))).toBe(false);
  });

  it('按 group 分段, 未分组在前, 分组保持首次出现顺序', () => {
    const sections = groupFields([
      ['model_name', field({ group: '模型' })],
      ['title', field()],
      ['on_busy', field({ group: '运行' })],
      ['thinking', field({ group: '模型' })],
    ]);
    expect(sections.map((s) => [s.group, s.fields.map(([k]) => k)])).toEqual([
      ['', ['title']],
      ['模型', ['model_name', 'thinking']],
      ['运行', ['on_busy']],
    ]);
  });

  it('time 与后端同口径: 00:00–23:59 以及 24:00', () => {
    for (const ok of ['00:00', '09:30', '23:59', '24:00']) expect(validTime(ok)).toBe(true);
    for (const bad of ['24:01', '9:30', '25:00', '12:60', '', '12-30', 930]) expect(validTime(bad)).toBe(false);
  });
});
