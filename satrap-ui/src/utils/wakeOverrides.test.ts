import { describe, expect, it } from 'vitest';
import {
  OVERRIDE_FIELDS,
  TIME_RULE_FIELDS,
  applyField,
  fieldState,
  fieldValue,
  fromGroupRows,
  fromTimeRows,
  parseOverrideJson,
  toGroupRows,
  toTimeRows,
} from './wakeOverrides';

const modeField = OVERRIDE_FIELDS.find((field) => field.key === 'wake_mode');
const thresholdField = OVERRIDE_FIELDS.find((field) => field.key === 'wake_message_threshold');
const wordsField = OVERRIDE_FIELDS.find((field) => field.key === 'wake_words');
const quoteField = OVERRIDE_FIELDS.find((field) => field.key === 'reply_with_quote');

if (!modeField || !thresholdField || !wordsField || !quoteField) {
  throw new Error('OVERRIDE_FIELDS 缺少测试所需字段定义');
}

describe('fieldState', () => {
  it('键缺失时为继承态', () => {
    expect(fieldState({}, modeField)).toBe('inherit');
  });

  it('值等于 offValue 时为关闭态', () => {
    expect(fieldState({ wake_mode: 'explicit' }, modeField)).toBe('off');
    expect(fieldState({ wake_words: [] }, wordsField)).toBe('off');
  });

  it('其它显式值为取值态', () => {
    expect(fieldState({ wake_mode: 'frequency' }, modeField)).toBe('value');
    expect(fieldState({ wake_words: ['bot'] }, wordsField)).toBe('value');
  });

  it('无 offValue 的字段不会出现关闭态', () => {
    expect(thresholdField.offValue).toBeUndefined();
    expect(fieldState({ wake_message_threshold: 0 }, thresholdField)).toBe('value');
  });
});

describe('fieldValue', () => {
  it('已显式设置的值原样返回', () => {
    expect(fieldValue({ wake_message_threshold: 5 }, thresholdField)).toBe(5);
  });

  it('缺省时按字段类型给默认: 数字取 min, 下拉取首项, 行列表取空数组, 布尔取 true', () => {
    expect(fieldValue({}, thresholdField)).toBe(thresholdField.min ?? 1);
    expect(fieldValue({}, modeField)).toBe('frequency');
    expect(fieldValue({}, wordsField)).toEqual([]);
    expect(fieldValue({}, quoteField)).toBe(true);
  });
});

describe('applyField', () => {
  it('继承态删除键且保留未知键', () => {
    const next = applyField({ wake_mode: 'frequency', custom_x: 1 }, modeField, 'inherit');
    expect(next).toEqual({ custom_x: 1 });
  });

  it('关闭态写入 offValue', () => {
    expect(applyField({}, modeField, 'off')).toEqual({ wake_mode: 'explicit' });
    expect(applyField({}, wordsField, 'off')).toEqual({ wake_words: [] });
  });

  it('取值态写入给定值', () => {
    expect(applyField({}, thresholdField, 'value', 8)).toEqual({ wake_message_threshold: 8 });
  });

  it('取值态未给值或 NaN 时回退到字段默认值', () => {
    expect(applyField({}, thresholdField, 'value')).toEqual({ wake_message_threshold: 1 });
    expect(applyField({}, thresholdField, 'value', Number.NaN)).toEqual({ wake_message_threshold: 1 });
  });
});

describe('parseOverrideJson', () => {
  it('非字符串原样返回', () => {
    const value = { a: 1 };
    expect(parseOverrideJson(value, {})).toBe(value);
  });

  it('空串与非法 JSON 回退 fallback', () => {
    expect(parseOverrideJson('', { fb: 1 })).toEqual({ fb: 1 });
    expect(parseOverrideJson('   ', { fb: 1 })).toEqual({ fb: 1 });
    expect(parseOverrideJson('{oops', { fb: 1 })).toEqual({ fb: 1 });
  });

  it('合法 JSON 字符串被解析', () => {
    expect(parseOverrideJson('{"20": {"wake_mode": "frequency"}}', {})).toEqual({ '20': { wake_mode: 'frequency' } });
  });
});

describe('toGroupRows', () => {
  it('对象与 JSON 字符串都可解析, 非对象覆盖被丢弃', () => {
    expect(toGroupRows({ '20': { wake_mode: 'frequency' }, bad: 3 })).toEqual([
      { id: '20', override: { wake_mode: 'frequency' } },
    ]);
    expect(toGroupRows('{"20": {"wake_cooldown": 30}}')).toEqual([
      { id: '20', override: { wake_cooldown: 30 } },
    ]);
  });

  it('非对象输入回退为空行', () => {
    expect(toGroupRows(undefined)).toEqual([]);
    expect(toGroupRows('[]')).toEqual([]);
    expect(toGroupRows(42)).toEqual([]);
  });
});

describe('fromGroupRows', () => {
  it('丢弃空号/非法号/重复号/全继承行', () => {
    const result = fromGroupRows([
      { id: '  ', override: { wake_mode: 'frequency' } },
      { id: '0', override: { wake_mode: 'frequency' } },
      { id: 'abc', override: { wake_mode: 'frequency' } },
      { id: '12a', override: { wake_mode: 'frequency' } },
      { id: '20', override: { wake_mode: 'frequency' } },
      { id: '20', override: { wake_mode: 'necessity' } },
      { id: '30', override: {} },
    ]);
    expect(result).toEqual({ '20': { wake_mode: 'frequency' } });
  });

  it('合法行保留显式关闭值', () => {
    expect(fromGroupRows([{ id: '20', override: { wake_words: [] } }])).toEqual({ '20': { wake_words: [] } });
  });
});

describe('toTimeRows', () => {
  it('数组解析并补齐缺失字段, 非对象项被丢弃', () => {
    expect(toTimeRows([{ start: '08:00', end: '12:00', settings: { wake_cooldown: 30 } }, 'bad', { start: '09:00' }])).toEqual([
      { start: '08:00', end: '12:00', settings: { wake_cooldown: 30 } },
      { start: '09:00', end: '', settings: {} },
    ]);
  });

  it('非数组输入回退为空行', () => {
    expect(toTimeRows(undefined)).toEqual([]);
    expect(toTimeRows('{}')).toEqual([]);
  });
});

describe('fromTimeRows', () => {
  it('丢弃非法时间/起止相同/空设置行', () => {
    const result = fromTimeRows([
      { start: '8:00', end: '12:00', settings: { wake_cooldown: 30 } },
      { start: '25:00', end: '12:00', settings: { wake_cooldown: 30 } },
      { start: '08:00', end: '08:00', settings: { wake_cooldown: 30 } },
      { start: '08:00', end: '12:00', settings: {} },
      { start: '23:59', end: '23:58', settings: { wake_cooldown: 30 } },
    ]);
    expect(result).toEqual([{ start: '23:59', end: '23:58', settings: { wake_cooldown: 30 } }]);
  });

  it('合法行原样输出且时段规则字段集不含群专属字段', () => {
    expect(fromTimeRows([{ start: '00:00', end: '06:00', settings: { wake_mode: 'explicit' } }])).toEqual([
      { start: '00:00', end: '06:00', settings: { wake_mode: 'explicit' } },
    ]);
    expect(TIME_RULE_FIELDS.some((field) => field.groupOnly)).toBe(false);
  });
});
