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
