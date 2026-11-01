import { describe, expect, it } from 'vitest';
import {
  advanceConfigRevision, mergeAdministratorConfig, mergeCommonConfig, retentionPurgeDays, runSectionSaves,
} from './types';

describe('retentionPurgeDays', () => {
  it('只在已保存策略可用时返回保留天数', () => {
    expect(retentionPurgeDays({ trash_retention_days: 30 }, false, false)).toBe(30);
    expect(retentionPurgeDays(null, false, false)).toBeNull();
    expect(retentionPurgeDays({ trash_retention_days: 30 }, true, false)).toBeNull();
    expect(retentionPurgeDays({ trash_retention_days: 30 }, false, true)).toBeNull();
  });
});

describe('mergeAdministratorConfig', () => {
  it('把管理员结果合并到最新配置并保留其它区段', () => {
    const config = { api: { port: 19870 }, administrator_groups: [{ id: 'old' }], extra: true };
    const groups = [{ id: 'new' }];
    const overrides = [{ id: 'override' }];
    const merged = mergeAdministratorConfig(config, groups, overrides);
    expect(merged).toEqual({ api: { port: 19870 }, administrator_groups: groups, administrator_overrides: overrides, extra: true });
    expect(config.administrator_groups).toEqual([{ id: 'old' }]);
  });

  it('管理员保存后继续合并常用配置时不覆盖管理员字段', () => {
    const latest = { api: { port: 19870 }, administrator_groups: [{ id: 'new' }], administrator_overrides: [{ id: 'o' }] };
    const merged = mergeCommonConfig(latest, {
      host: '127.0.0.1', port: 19871, defaultSessionType: 'agent', maxSessions: 10,
      idleTimeout: 60, sessionTimeout: 30, rateLimit: 1, rateBurst: 2,
    });
    expect(merged.administrator_groups).toEqual([{ id: 'new' }]);
    expect(merged.administrator_overrides).toEqual([{ id: 'o' }]);
    expect(merged.api).toEqual({ host: '127.0.0.1', port: 19871 });
  });
});

describe('advanceConfigRevision', () => {
  it('仅在当前修订仍为保存前修订时推进', () => {
    expect(advanceConfigRevision('r1', 'r1', 'r2')).toBe('r2');
    expect(advanceConfigRevision(undefined, 'r1', 'r2')).toBe('r2');
    expect(advanceConfigRevision('r3', 'r1', 'r2')).toBe('r3');
  });
});

describe('runSectionSaves', () => {
  it('按顺序保存有修改的分节并在失败时停止', async () => {
    const calls: string[] = [];
    const actions = {
      admin: { save: async () => { calls.push('admin'); return true; } },
      common: { save: async () => { calls.push('common'); return false; } },
      raw: { save: async () => { calls.push('raw'); return true; } },
    };
    const failedSection = await runSectionSaves(['admin', 'common', 'raw'], { admin: true, common: true, raw: true }, actions);
    expect(failedSection).toBe('common');
    expect(calls).toEqual(['admin', 'common']);
  });

  it('跳过没有修改的分节并报告全部成功', async () => {
    const calls: string[] = [];
    const actions = {
      admin: { save: async () => { calls.push('admin'); return true; } },
      common: { save: async () => { calls.push('common'); return true; } },
    };
    const failedSection = await runSectionSaves(['admin', 'common'], { admin: false, common: true }, actions);
    expect(failedSection).toBeNull();
    expect(calls).toEqual(['common']);
  });

  it('缺少保存动作时返回失败分节', async () => {
    const failedSection = await runSectionSaves(['common'], { common: true }, {});
    expect(failedSection).toBe('common');
  });
});
