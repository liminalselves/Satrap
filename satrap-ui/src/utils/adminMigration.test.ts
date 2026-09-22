import { describe, expect, it } from 'vitest';
import {
  appendWithLimit,
  classNameToConfigName,
  filterLogs,
  normalizePlatformSettings,
  visibleLogs,
} from './adminMigration';

describe('管理前端迁移逻辑', () => {
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
