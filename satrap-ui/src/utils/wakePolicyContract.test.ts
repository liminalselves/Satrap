import { describe, expect, it } from 'vitest';
import policyCases from '../../../tests/fixtures/wake_policy_cases.json';
import {
  POLICY_FIELDS,
  POLICY_FIELD_LABELS,
  formNumericLimits,
  formRangeSuffix,
  formRangeText,
  overrideFieldKeys,
  policyField,
  policyFieldLabel,
  policyIssuesMessage,
  previewFieldKeys,
  rangeText,
  validatePolicySettings,
  validatePolicyValue,
} from './wakePolicyContract';
import { GROUP_OVERRIDE_FIELDS, OVERRIDE_FIELDS, TIME_RULE_FIELDS, fromGroupRows, fromTimeRows } from './wakeOverrides';
import { PLATFORM_NUMERIC_KEYS, normalizePlatformSettings, validatePlatformPolicy } from './adminMigration';

const CONTEXTS = ['platform', 'group', 'time'] as const;
type PolicyCaseContext = (typeof CONTEXTS)[number];

interface PolicyCase {
  id: string;
  context: PolicyCaseContext;
  field: string;
  valid: boolean;
  value: Record<string, unknown>;
}

function isContext(value: string): value is PolicyCaseContext {
  return (CONTEXTS as readonly string[]).includes(value);
}

// 样例文件与后端共用一份, 上下文非法时直接失败而不是静默跳过
const CASES: PolicyCase[] = policyCases.cases.map((item) => {
  if (!isContext(item.context)) throw new Error(`样例包含未知上下文: ${item.id}/${item.context}`);
  return { ...item, context: item.context };
});

function validOf(context: PolicyCaseContext, value: Record<string, unknown>): boolean {
  return validatePolicySettings(context, value).length === 0;
}

describe('策略字段契约加载', () => {
  it('字段数量与关键取值与生成的契约一致', () => {
    expect(POLICY_FIELDS.length).toBe(35);
    const maxWait = policyField('wake_max_wait');
    expect(maxWait?.max_exclusive).toBe(120);
    expect(maxWait?.max).toBeUndefined();
    // 冷却只有下界: 不接受一天上限这类额外业务范围
    const cooldown = policyField('wake_cooldown');
    expect(cooldown?.min).toBe(0);
    expect(cooldown?.max).toBeUndefined();
    expect(cooldown?.max_exclusive).toBeUndefined();
    expect(policyField('wake_talk_value')?.off_value).toBe(0);
    expect(policyField('wake_talk_value')?.nullable).toBe(true);
  });

  it('每个契约字段都有校验文案用的字段名', () => {
    for (const field of POLICY_FIELDS) {
      expect(POLICY_FIELD_LABELS[field.key], field.key).toBeDefined();
      expect(policyFieldLabel(field.key), field.key).not.toBe(field.key);
    }
  });

  it('范围文本按含上界/排他上界/只有下界生成', () => {
    expect(rangeText(policyField('wake_message_threshold')!)).toBe('1–32');
    expect(rangeText(policyField('wake_max_wait')!)).toBe('0 ≤ x < 120');
    expect(rangeText(policyField('wake_cooldown')!)).toBe('≥ 0');
  });

  it('覆盖字段集合与契约作用域一致, 时段规则只取 time 作用域', () => {
    const groupKeys = new Set(overrideFieldKeys('group'));
    const timeKeys = new Set(overrideFieldKeys('time'));
    expect(GROUP_OVERRIDE_FIELDS.map((field) => field.key).sort()).toEqual([...groupKeys].sort());
    expect(TIME_RULE_FIELDS.map((field) => field.key).sort()).toEqual([...timeKeys].sort());
    // 每个可覆盖字段都有编辑器文案与契约取值范围
    for (const field of OVERRIDE_FIELDS) {
      expect(field.label).not.toContain('undefined');
      expect(policyField(field.key)).toBeDefined();
    }
    expect(OVERRIDE_FIELDS.find((field) => field.key === 'wake_talk_value')?.label).toBe('发言频率偏好 (0–1)');
    expect(OVERRIDE_FIELDS.find((field) => field.key === 'wake_max_wait')?.label).toBe('最长等待秒数 (0 ≤ x < 120)');
    expect(OVERRIDE_FIELDS.find((field) => field.key === 'wake_cooldown')?.label).toBe('冷却秒数 (≥ 0)');
    expect(OVERRIDE_FIELDS.find((field) => field.key === 'wake_mode')?.options).toEqual([
      { value: 'frequency', label: '按消息数量触发' },
      { value: 'necessity', label: '按必要性评分' },
    ]);
  });

  it('试算重点展示键来自契约的 display_in_preview', () => {
    expect(previewFieldKeys().sort()).toEqual([
      'wake_cooldown', 'wake_max_wait', 'wake_message_threshold', 'wake_mode', 'wake_score_threshold', 'wake_talk_value',
    ]);
  });

  it('平台表单数值字段都在契约里且带范围与控件约束', () => {
    for (const key of PLATFORM_NUMERIC_KEYS) {
      const field = policyField(key);
      expect(field, key).toBeDefined();
      expect(['int', 'number'], key).toContain(field?.kind);
      expect(formRangeText(key), key).not.toBe('');
      const limits = formNumericLimits(key);
      expect(limits.min, key).toBe(field?.min);
      // 排他上界不写进 HTML max, 整数字段给整数步长
      expect(limits.max, key).toBe(field?.max);
      expect(limits.step, key).toBe(field?.kind === 'int' ? 1 : 'any');
    }
    expect(formRangeText('wake_max_wait')).toBe('0 ≤ x < 120');
    expect(formNumericLimits('wake_max_wait').max).toBeUndefined();
    expect(formRangeSuffix('wake_cooldown')).toBe('（≥ 0）');
  });
});

describe('共享样例 (与 pytest 同一份 JSON)', () => {
  it.each(CASES.map((item) => [item.id, item] as const))('%s', (_id, item) => {
    expect(validOf(item.context, item.value)).toBe(item.valid);
  });

  it('样例覆盖三种上下文与边界取值', () => {
    expect(new Set(CASES.map((item) => item.context))).toEqual(new Set(['platform', 'group', 'time']));
    expect(CASES.some((item) => item.id === 'platform-wake-max-wait-boundary')).toBe(true);
    expect(CASES.some((item) => item.id === 'platform-wake-cooldown-over-day')).toBe(true);
  });
});

describe('严格取值校验', () => {
  const cooldown = policyField('wake_cooldown')!;

  it('NaN 与 Infinity 不能作为有限数值', () => {
    expect(validatePolicyValue(cooldown, Number.NaN)).toContain('必须为数字');
    expect(validatePolicyValue(cooldown, Number.POSITIVE_INFINITY)).toContain('必须为数字');
    expect(validatePolicyValue(cooldown, Number.NEGATIVE_INFINITY)).toContain('必须为数字');
  });

  it('排他上界不写成 119 或 epsilon', () => {
    const maxWait = policyField('wake_max_wait')!;
    expect(validatePolicyValue(maxWait, 119.9999)).toBeNull();
    expect(validatePolicyValue(maxWait, 120)).toContain('小于 120');
    expect(validatePolicyValue(maxWait, 120)).not.toContain('119');
  });

  it('缺失表示继承, 显式 null 只在可空字段上合法', () => {
    expect(policyIssuesMessage(validatePolicySettings('group', {}))).toBeNull();
    expect(validatePolicyValue(policyField('wake_talk_value')!, null)).toBeNull();
    expect(validatePolicyValue(cooldown, null)).toContain('不能为空');
  });

  it('群白名单与上下文范围由后端专用校验器负责, 前端不复制其归一化', () => {
    expect(validOf('platform', { group_whitelist: '123', context_scope: 'legacy_user' })).toBe(true);
    expect(validOf('platform', { wake_group_overrides: { '20': {} }, wake_time_rules: [] })).toBe(true);
  });

  it('文本与列表项长度按 Unicode 码点计, 与后端 len() 同口径', () => {
    const asr = policyField('asr_model')!;
    // 128 个码点的非 BMP 名称在后端合法, 前端不能因为 UTF-16 码元翻倍而误拒
    expect(validatePolicyValue(asr, '😀'.repeat(128))).toBeNull();
    expect(validatePolicyValue(asr, '😀'.repeat(129))).toContain('128');
    const hosts = policyField('media_trusted_hosts')!;
    expect(validatePolicyValue(hosts, ['例'.repeat(126) + '😀'.repeat(127)])).toBeNull();
    expect(validatePolicyValue(hosts, ['例'.repeat(126) + '😀'.repeat(128)])).not.toBeNull();
  });

  it('平台未知扩展字段透传, 覆盖里的未知字段被拒绝', () => {
    expect(validOf('platform', { extension: 'keep' })).toBe(true);
    expect(validatePolicySettings('group', { extension: 'keep' })[0].message).toContain('不能出现在群覆盖中');
    expect(validatePolicySettings('time', { quote_lookup: true })[0].message).toContain('不能出现在时段规则中');
  });
});

describe('实际行转换入口', () => {
  const groupCases = CASES.filter((item) => item.context === 'group');
  const timeCases = CASES.filter((item) => item.context === 'time');

  it.each(groupCases.map((item) => [item.id, item] as const))('群覆盖 %s', (_id, item) => {
    const conversion = fromGroupRows([{ rowId: 'group-row-1', id: '20', override: item.value }]);
    expect(conversion.issues.length === 0).toBe(item.valid);
    // 全部字段继承的行规范化为不写覆盖, 其余合法行原样保留
    if (item.valid) {
      expect(conversion.value['20'] ?? {}).toEqual(item.value);
    }
  });

  it.each(timeCases.map((item) => [item.id, item] as const))('时段规则 %s', (_id, item) => {
    const conversion = fromTimeRows([{ rowId: 'time-row-1', start: '23:00', end: '07:00', settings: item.value }]);
    expect(conversion.issues.length === 0).toBe(item.valid);
    if (item.valid) {
      expect(conversion.value[0]?.settings ?? {}).toEqual(item.value);
    }
  });

  it('非法取值保留草稿并阻止保存, 不进入提交值', () => {
    const conversion = fromGroupRows([{ rowId: 'group-row-1', id: '20', override: { wake_max_wait: 120, wake_cooldown: -1 } }]);
    expect(conversion.issues.map((issue) => issue.field)).toEqual(['wake_max_wait', 'wake_cooldown']);
    expect(conversion.value).toEqual({});
  });
});

describe('平台表单归一化后校验', () => {
  it('数字字符串与空白先归一化再校验', () => {
    const normalized = normalizePlatformSettings('onebot', {
      wake_cooldown: '30', wake_max_wait: ' 45 ', wake_talk_value: '', input_text_limit: '500', reply_with_quote: 'true',
    });
    expect(normalized.wake_cooldown).toBe(30);
    expect(normalized.wake_max_wait).toBe(45);
    expect('wake_talk_value' in normalized).toBe(false);
    expect(normalized.reply_with_quote).toBe(true);
    expect(validatePlatformPolicy(normalized)).toBeNull();
  });

  it('归一化不会放宽排他上界', () => {
    const normalized = normalizePlatformSettings('onebot', { wake_max_wait: '120' });
    expect(validatePlatformPolicy(normalized)).toContain('小于 120');
    expect(validatePlatformPolicy({ wake_max_wait: 119.9999 })).toBeNull();
    expect(validatePlatformPolicy({ wake_cooldown: 86401 })).toBeNull();
    expect(validatePlatformPolicy({ input_media_limit: 2.5 })).toContain('必须为整数');
    expect(validatePlatformPolicy({ wake_talk_value: Number.NaN })).toContain('必须为数字');
  });

  it('平台扩展字段与业务侧校验的字段不会被前端拒绝', () => {
    expect(validatePlatformPolicy(normalizePlatformSettings('aiocqhttp', { extension: 'keep', group_whitelist: '123\n456' }))).toBeNull();
    expect(validatePlatformPolicy(normalizePlatformSettings('onebot', { group_whitelist: 'abc' }))).toBeNull();
  });
});
