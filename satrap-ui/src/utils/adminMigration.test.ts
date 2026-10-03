import { describe, expect, it } from 'vitest';
import {
  TALK_VALUE_HINT,
  appendWithLimit,
  classNameToConfigName,
  filterLogs,
  normalizePlatformSettings,
  validatePlatformPolicy,
  visibleLogs,
} from './adminMigration';

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
