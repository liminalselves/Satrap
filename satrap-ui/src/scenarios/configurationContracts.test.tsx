import { describe, expect, it } from "vitest";
import { TALK_VALUE_HINT, appendWithLimit, classNameToConfigName, filterLogs, normalizePlatformSettings, validatePlatformPolicy, visibleLogs, PLATFORM_NUMERIC_KEYS } from "@/utils/adminMigration";
import { OVERRIDE_FIELDS, TIME_RULE_FIELDS, applyField, fieldState, fieldValue, fromGroupRows, fromTimeRows, toGroupRows, toTimeRows, GROUP_OVERRIDE_FIELDS } from "@/utils/wakeOverrides";
import policyCases from "@/../../tests/fixtures/wake_policy_cases.json";
import { POLICY_FIELD_BY_KEY, formNumericLimits, formRangeSuffix, formRangeText, overrideFieldKeys, policyField, policyIssuesMessage, previewFieldKeys, rangeText, validatePolicySettings, validatePolicyValue } from "@/utils/wakePolicyContract";
import { readEdictumParams, writeEdictumParams } from "@/utils/edictumParams";
import { renderToStaticMarkup } from "react-dom/server";
import { PluginConfigFields } from "@/components/common/PluginConfigFields";
import { pluginOptionNeeds } from "@/hooks/usePluginConfigOptions";

describe("utils/adminMigration", () => {
  describe('管理前端迁移逻辑', () => {
    it.each(['onebot', 'misskey', 'future-adapter'])('档案保留期对 %s 使用统一归一化与校验', (type) => {
      expect(normalizePlatformSettings(type, { message_archive_retention_days: ' 7 ' }).message_archive_retention_days).toBe(7);
      expect(normalizePlatformSettings(type, { message_archive_retention_days: '' })).not.toHaveProperty('message_archive_retention_days');
      expect(validatePlatformPolicy(normalizePlatformSettings(type, { message_archive_retention_days: true }))).toContain('档案保留天数');
      expect(validatePlatformPolicy(normalizePlatformSettings(type, { message_archive_retention_days: '0' }))).toContain('档案保留天数');
      expect(validatePlatformPolicy(normalizePlatformSettings(type, { message_archive_retention_days: '1.5' }))).toContain('必须为整数');
      expect(validatePlatformPolicy(normalizePlatformSettings(type, { message_archive_retention_days: '3651' }))).toContain('档案保留天数');
    });
    it('从 Session 类名生成默认配置名', () => {
      expect(classNameToConfigName('CustomChatSession')).toBe('custom_chat');
      expect(classNameToConfigName('Echo')).toBe('echo');
    });

    it('补齐平台默认值并保留扩展配置', () => {
      expect(normalizePlatformSettings('onebot', { extension: true })).toMatchObject({
        host: '127.0.0.1',
        port: 8080,
        enable_private: true,
        enable_group: true,
        extension: true,
      });
      expect(normalizePlatformSettings('onebot', { reply_with_quote: true })).toMatchObject({ reply_with_quote: true });
      expect('reply_with_mention' in normalizePlatformSettings('onebot', {})).toBe(false);
      expect(normalizePlatformSettings('onebot', { notice_types: 'notice.group_increase\n\nrequest' })).toMatchObject({ notice_types: ['notice.group_increase', 'request'] });
      expect('notice_types' in normalizePlatformSettings('onebot', { notice_types: '' })).toBe(false);
      expect(normalizePlatformSettings('onebot', { enable_private: 'false', enable_group: false, reply_with_quote: 'true', attachment_extract: '0' })).toMatchObject({
        enable_private: false, enable_group: false, reply_with_quote: true, attachment_extract: false,
      });
      expect(normalizePlatformSettings('onebot', { wake_max_wait: 0, media_insecure_tls: true })).toMatchObject({ wake_max_wait: 0, media_insecure_tls: true });
      expect(normalizePlatformSettings('onebot', { media_plaintext_http: 'true' })).toMatchObject({ media_plaintext_http: true });
      expect(normalizePlatformSettings('onebot', { media_plaintext_http: false })).toMatchObject({ media_plaintext_http: false });
      expect(normalizePlatformSettings('onebot', { media_trusted_hosts: 'a.local\n', asr_model: 'speech', attachment_extract: false })).toMatchObject({ media_trusted_hosts: ['a.local'], asr_model: 'speech', attachment_extract: false });
      expect('asr_model' in normalizePlatformSettings('onebot', { asr_model: '  ' })).toBe(false);
      expect(normalizePlatformSettings('onebot', { command_operators: ' 10001 \n\n 10002 ' })).toMatchObject({ command_operators: ['10001', '10002'] });
      expect('command_operators' in normalizePlatformSettings('onebot', { command_operators: '   ' })).toBe(false);
      expect(normalizePlatformSettings('misskey', { command_operators: '10001\n10002' })).toMatchObject({ command_operators: ['10001', '10002'] });
      expect('command_operators' in normalizePlatformSettings('misskey', { command_operators: '' })).toBe(false);
      expect('voice_transcribe' in normalizePlatformSettings('onebot', { voice_transcribe: 'asr' })).toBe(false);
      expect(normalizePlatformSettings('onebot', { voice_transcribe: 'platform' })).toMatchObject({ voice_transcribe: 'platform' });
      expect(normalizePlatformSettings('misskey', {})).toMatchObject({
        chat_enabled: true,
        room_enabled: false,
        max_message_length: 3000,
      });
    });

    it('限制日志缓冲并支持多级筛选和搜索', () => {
      const logs = appendWithLimit(
        [
          { level: 'DEBUG', content: 'debug detail' },
          { level: 'INFO', content: 'service ready' },
        ],
        { level: 'ERROR', content: 'service failed' },
        2,
      );
      expect(logs).toHaveLength(2);
      expect(filterLogs(logs, ['INFO', 'ERROR'], 'service')).toEqual(logs);
      expect(filterLogs(logs, ['ERROR'], 'failed')).toEqual([logs[1]]);
    });

    it('暂停时展示快照而不是丢弃新日志', () => {
      const snapshot = ['before'];
      const buffered = ['before', 'during'];
      expect(visibleLogs(buffered, snapshot, true)).toBe(snapshot);
      expect(visibleLogs(buffered, snapshot, false)).toBe(buffered);
    });
  });


  describe('OneBot 策略表单保存', () => {
    it('转换群与时段 JSON 配置, 非法输入拒绝保存', () => {
      expect(normalizePlatformSettings('onebot', {
        wake_group_overrides: '{"123":{"wake_mode":"necessity"}}',
        wake_time_rules: '[{"start":"23:00","end":"07:00","settings":{"wake_mode":"explicit"}}]',
      })).toMatchObject({
        wake_group_overrides: { '123': { wake_mode: 'necessity' } },
        wake_time_rules: [{ start: '23:00', end: '07:00', settings: { wake_mode: 'explicit' } }],
      });
      expect(() => normalizePlatformSettings('onebot', { wake_time_rules: 'invalid' })).toThrow();
    });
    it.each(['onebot', 'aiocqhttp'])('归一化 %s 的逐行配置并保留扩展字段', (type) => {
      expect(normalizePlatformSettings(type, {
        group_whitelist: ' 123\n\n456 ', wake_words: '小助手\n hello bot ', extension: true,
      })).toMatchObject({
        group_whitelist: ['123', '456'], wake_words: ['小助手', 'hello bot'], extension: true,
      });
      expect(normalizePlatformSettings(type, { group_whitelist: '', wake_words: '' })).toMatchObject({
        group_whitelist: [], wake_words: [],
      });
      expect(normalizePlatformSettings(type, { group_whitelist: ['123'] }).group_whitelist).toEqual(['123']);
    });
  });

  describe('B10 平台输入预算与 talk_value 往返', () => {
    it('0 按数字保存, 留空才表示未设置', () => {
      const zero = normalizePlatformSettings('onebot', { wake_talk_value: '0', input_text_limit: 0, input_media_limit: '2' });
      // 0 是有效取值: 关闭自动参与 / 预算为 0 由后端范围校验拒绝, 但不能被前端静默删掉
      expect(zero.wake_talk_value).toBe(0);
      expect(zero.input_text_limit).toBe(0);
      expect(zero.input_media_limit).toBe(2);
      expect('wake_talk_value' in normalizePlatformSettings('onebot', { wake_talk_value: '' })).toBe(false);
      expect('wake_talk_value' in normalizePlatformSettings('onebot', { wake_talk_value: '   ' })).toBe(false);
      expect('input_text_limit' in normalizePlatformSettings('onebot', { input_text_limit: undefined })).toBe(false);
      expect(normalizePlatformSettings('onebot', { wake_talk_value: '0.35' }).wake_talk_value).toBe(0.35);
    });

    it('校验拒绝非法值并给出字段名', () => {
      expect(validatePlatformPolicy({ wake_talk_value: 0 })).toBeNull();
      expect(validatePlatformPolicy({ input_text_limit: 1, input_media_limit: 32 })).toBeNull();
      expect(validatePlatformPolicy({ wake_talk_value: 1.5 })).toContain('发言频率偏好');
      expect(validatePlatformPolicy({ input_text_limit: 0 })).toContain('输入文本预算');
      expect(validatePlatformPolicy({ input_text_limit: 200001 })).toContain('输入文本预算');
      expect(validatePlatformPolicy({ input_media_limit: 2.5 })).toContain('必须为整数');
      expect(validatePlatformPolicy({ input_media_limit: 33 })).toContain('输入媒体上限');
      expect(validatePlatformPolicy({ message_text_limit: 32 })).toContain('每条消息文本上限');
      expect(validatePlatformPolicy({ wake_max_wait: 120 })).toContain('最长等待秒数');
      expect(validatePlatformPolicy({ wake_talk_value: Number.NaN })).toContain('必须为数字');
      // 非数字字段按契约报错, 不再只查 8 个数值键
      expect(validatePlatformPolicy({ voice_transcribe: 'auto' })).toContain('语音转写来源');
      expect(validatePlatformPolicy({ wake_words: '小助手' })).toContain('唤醒词');
    });

    it('talk_value 提示为静态文案, 数值结论以试算响应为准', () => {
      expect(TALK_VALUE_HINT).toContain('留空表示未设置');
      // 前端不再推导优先级结论: 显式阈值与 talk_value 都不改变提示
      expect(TALK_VALUE_HINT).not.toContain('被显式阈值覆盖');
      expect(TALK_VALUE_HINT).not.toContain('关闭自动参与');
    });
  });
});

describe("utils/wakeOverrides", () => {
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

  describe('覆盖编辑入口解析', () => {
    it.each(['', '   ', '{oops'])('空串与非法 JSON 在两个编辑入口都回退为空集合: %s', (value) => {
      expect(toGroupRows(value)).toEqual([]);
      expect(toTimeRows(value)).toEqual([]);
    });
  });

  describe('toGroupRows', () => {
    it('对象与 JSON 字符串都可解析, 非对象覆盖被丢弃, 每行带稳定 rowId', () => {
      const rows = toGroupRows({ '20': { wake_mode: 'frequency' }, bad: 3 });
      expect(rows).toHaveLength(1);
      expect(rows[0].id).toBe('20');
      expect(rows[0].override).toEqual({ wake_mode: 'frequency' });
      expect(rows[0].rowId).toBeTruthy();
      const again = toGroupRows('{"20": {"wake_cooldown": 30}}');
      expect(again[0].rowId).not.toBe(rows[0].rowId);
    });

    it('非对象输入回退为空行', () => {
      expect(toGroupRows(undefined)).toEqual([]);
      expect(toGroupRows('[]')).toEqual([]);
      expect(toGroupRows(42)).toEqual([]);
    });
  });

  describe('fromGroupRows', () => {
    it('空号/非法号/重复号/起止保留为错误, 不静默丢弃也不进入提交值', () => {
      const result = fromGroupRows([
        { rowId: 'r1', id: '  ', override: { wake_mode: 'frequency' } },
        { rowId: 'r2', id: '0', override: { wake_mode: 'frequency' } },
        { rowId: 'r3', id: 'abc', override: { wake_mode: 'frequency' } },
        { rowId: 'r4', id: '12a', override: { wake_mode: 'frequency' } },
        { rowId: 'r5', id: '20', override: { wake_mode: 'frequency' } },
        { rowId: 'r6', id: '20', override: { wake_mode: 'necessity' } },
        { rowId: 'r7', id: '30', override: {} },
      ]);
      // 重复群号只保留首行, 第二行报错; 全继承行是显式删除覆盖的结果, 不计错误
      expect(result.value).toEqual({ '20': { wake_mode: 'frequency' } });
      expect(result.issues.map((issue) => issue.rowId)).toEqual(['r1', 'r2', 'r3', 'r4', 'r6']);
      expect(result.issues.every((issue) => issue.field === 'id')).toBe(true);
      expect(result.issues[0].message).toContain('群号');
    });

    it('合法行保留显式关闭值且无错误', () => {
      const result = fromGroupRows([{ rowId: 'r1', id: '20', override: { wake_words: [] } }]);
      expect(result.value).toEqual({ '20': { wake_words: [] } });
      expect(result.issues).toEqual([]);
    });

    it('群号前后空白被裁剪后仍视为合法', () => {
      const result = fromGroupRows([{ rowId: 'r1', id: ' 20 ', override: { wake_mode: 'frequency' } }]);
      expect(result.value).toEqual({ '20': { wake_mode: 'frequency' } });
      expect(result.issues).toEqual([]);
    });
  });

  describe('toTimeRows', () => {
    it('数组解析并补齐缺失字段, 非对象项被丢弃', () => {
      const rows = toTimeRows([{ start: '08:00', end: '12:00', settings: { wake_cooldown: 30 } }, 'bad', { start: '09:00' }]);
      expect(rows.map(({ start, end, settings }) => ({ start, end, settings }))).toEqual([
        { start: '08:00', end: '12:00', settings: { wake_cooldown: 30 } },
        { start: '09:00', end: '', settings: {} },
      ]);
      expect(new Set(rows.map((row) => row.rowId)).size).toBe(rows.length);
    });

    it('非数组输入回退为空行', () => {
      expect(toTimeRows(undefined)).toEqual([]);
      expect(toTimeRows('{}')).toEqual([]);
    });
  });

  describe('fromTimeRows', () => {
    it('非法时间/起止相同的行报错并保留, 不进入提交值', () => {
      const result = fromTimeRows([
        { rowId: 't1', start: '8:00', end: '12:00', settings: { wake_cooldown: 30 } },
        { rowId: 't2', start: '25:00', end: '12:00', settings: { wake_cooldown: 30 } },
        { rowId: 't3', start: '08:00', end: '08:00', settings: { wake_cooldown: 30 } },
        { rowId: 't4', start: '08:00', end: '12:00', settings: {} },
        { rowId: 't5', start: '23:59', end: '23:58', settings: { wake_cooldown: 30 } },
      ]);
      expect(result.value).toEqual([{ start: '23:59', end: '23:58', settings: { wake_cooldown: 30 } }]);
      expect(result.issues.map((issue) => [issue.rowId, issue.field])).toEqual([
        ['t1', 'start'],
        ['t2', 'start'],
        ['t3', 'end'],
      ]);
      // 空设置行是全部继承的合法结果, 不是错误
      expect(result.issues.some((issue) => issue.rowId === 't4')).toBe(false);
    });

    it('合法行原样输出且时段规则字段集不含群专属字段', () => {
      const result = fromTimeRows([{ rowId: 't1', start: '00:00', end: '06:00', settings: { wake_mode: 'explicit' } }]);
      expect(result.value).toEqual([{ start: '00:00', end: '06:00', settings: { wake_mode: 'explicit' } }]);
      expect(result.issues).toEqual([]);
      expect(TIME_RULE_FIELDS.some((field) => field.groupOnly)).toBe(false);
    });
  });
});

describe("utils/wakePolicyContract", () => {
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
      expect(POLICY_FIELD_BY_KEY.size).toBe(35);
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

    it('每个基础校验字段的错误文案使用字段名称', () => {
      for (const field of POLICY_FIELD_BY_KEY.values()) {
        if (['scope', 'group_ids', 'group_map', 'time_rules'].includes(field.kind)) continue;
        const error = validatePolicyValue(field, Symbol('invalid'));
        expect(error, field.key).not.toBeNull();
        expect(error?.startsWith(field.key), field.key).toBe(false);
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
        { value: 'all', label: '每条消息触发' },
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
});

describe("utils/edictumParams", () => {
  describe('Edictum 提示词配置', () => {
    it('从旧 JSON 读取提示词并保留其他参数', () => {
      const form = readEdictumParams({ system_prompt: '助手', custom: { a: 1 } }, true);
      expect(form.system_prompt).toBe('助手');
      expect(form.system_prompt_enabled).toBe(true);
      expect(readEdictumParams({ command_history_enabled: true }, true).command_history_enabled).toBe(true);
      expect(writeEdictumParams({ ...form, command_history_enabled: true }, true).command_history_enabled).toBe(true);
      expect(JSON.parse(form.params)).toEqual({ custom: { a: 1 } });
      expect(writeEdictumParams(form, true)).toEqual({ system_prompt: '助手', command_history_enabled: false, custom: { a: 1 } });
    });

    it('区分主动清空与未配置, 拒绝重复入口', () => {
      const form = { params: '{}', system_prompt: '', system_prompt_enabled: true };
      expect(writeEdictumParams(form, true)).toEqual({ system_prompt: '', command_history_enabled: false });
      expect(writeEdictumParams({ ...form, system_prompt_enabled: false }, true)).toEqual({ command_history_enabled: false });
      expect(() => writeEdictumParams({ ...form, params: '{"system_prompt":"重复"}' }, true)).toThrow('输入框');
    });

    it('自定义会话类型继续使用原始参数', () => {
      const form = readEdictumParams({ system_prompt: '自定义', custom: 3 }, false);
      expect(writeEdictumParams(form, false)).toEqual({ system_prompt: '自定义', custom: 3 });
    });

    it('迁移旧生成参数, 保留零温度并支持恢复继承', () => {
      const form = readEdictumParams({ thinking: 'high', temperature: 0, model_params: { top_p: 0.8, max_tokens: 2048 }, custom: 1 }, true);
      expect(writeEdictumParams(form, true)).toEqual({ thinking: 'high', command_history_enabled: false, model_params: { temperature: 0, top_p: 0.8, max_tokens: 2048 }, custom: 1 });
      expect(writeEdictumParams({ ...form, thinking: '', temperature: undefined, top_p: undefined, max_tokens: undefined }, true)).toEqual({ command_history_enabled: false, custom: 1 });
      expect(() => writeEdictumParams({ ...form, top_p: 0 }, true)).toThrow('top_p');
      expect(() => writeEdictumParams({ ...form, max_tokens: 1.5 }, true)).toThrow('正整数');
    });
  });
});

describe("components/common/PluginConfigFields", () => {
  describe('插件选项状态', () => {
    const schema = { note: { type: 'string', default: '', description: '' }, model: { type: 'llm', default: '', description: '' } };
    it('选项未就绪时保留原值, 不误报引用失效, 普通参数仍可编辑', () => {
      const markup = renderToStaticMarkup(<PluginConfigFields schema={schema} values={{ note: '草稿', model: 'saved' }} modelState="loading" onChange={() => undefined} />);
      expect(markup).toContain('正在读取选项');
      expect(markup).not.toContain('引用失效');
      expect(markup).toMatch(/<option[^>]*value="saved"[^>]*selected/);
      expect(markup.match(/<input[^>]*value="草稿"[^>]*>/)?.[0]).not.toContain('disabled');
    });
    it('成功读取后才标记确实缺失的引用', () => {
      const markup = renderToStaticMarkup(<PluginConfigFields schema={schema} values={{ model: 'gone' }} onChange={() => undefined} />);
      expect(markup).toContain('gone (引用失效)');
    });
    it('选项需求仅取决于引用字段, 普通配置不读取模型或知识库', () => {
      expect(pluginOptionNeeds({ note: schema.note })).toEqual({ models: false, knowledge: false });
      expect(pluginOptionNeeds(schema)).toEqual({ models: true, knowledge: false });
      expect(pluginOptionNeeds({ kb: { type: 'knowledge_bases', default: [], description: '' } })).toEqual({ models: false, knowledge: true });
    });
  });
});
