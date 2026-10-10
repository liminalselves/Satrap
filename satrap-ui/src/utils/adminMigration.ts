import { policyIssuesMessage, validatePolicySettings } from '@/utils/wakePolicyContract';

export interface FilterableLog {
  content: string;
  level: string;
}

export function classNameToConfigName(className: string): string {
  return className.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().replace(/_session$/, '');
}

// 布尔字段只接受真正的 boolean 或明确的 true/false 字符串, 避免 Boolean('false') 反转语义
function toBoolean(value: unknown, fallback: boolean): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    const text = value.trim().toLowerCase();
    if (text === 'true' || text === '1' || text === 'yes' || text === 'on') return true;
    if (text === 'false' || text === '0' || text === 'no' || text === 'off' || text === '') return false;
  }
  if (typeof value === 'number') return value !== 0;
  return fallback;
}

// 平台表单里的数值策略字段: 顺序与表单一致, 归一化与取值范围由策略字段契约提供
export const PLATFORM_NUMERIC_KEYS = [
  'message_archive_retention_days',
  'message_text_limit',
  'input_text_limit',
  'input_media_limit',
  'wake_message_threshold',
  'wake_cooldown',
  'wake_max_wait',
  'wake_score_threshold',
  'wake_talk_value',
];

// 平台表单的数值字段校验: 只接受归一化后的设置, 校验规则来自生成的策略字段契约
export function validatePlatformPolicy(settings: Record<string, unknown>): string | null {
  return policyIssuesMessage(validatePolicySettings('platform', settings));
}

// talk_value 与显式阈值的优先级结论以后端试算响应为准 (threshold.hint/threshold.overridden), 前端不自行推导
export const TALK_VALUE_HINT = '留空表示未设置; 生效阈值与覆盖情况以试算结论为准';

export function normalizePlatformSettings(
  type: string,
  settings: Record<string, unknown>,
): Record<string, unknown> {
  settings = { ...settings };
  const archiveDays = typeof settings.message_archive_retention_days === 'string' ? settings.message_archive_retention_days.trim() : settings.message_archive_retention_days;
  if (archiveDays === '' || archiveDays === undefined || archiveDays === null) delete settings.message_archive_retention_days;
  else settings.message_archive_retention_days = typeof archiveDays === 'string' || typeof archiveDays === 'number' ? Number(archiveDays) : archiveDays;
  if (type === 'onebot' || type === 'aiocqhttp') {
    const selfId = String(settings.self_id ?? '').trim();
    const normalized: Record<string, unknown> = {
      ...settings,
      host: String(settings.host ?? '127.0.0.1'),
      port: Number(settings.port ?? 8080),
      access_token: String(settings.access_token ?? ''),
      secret: String(settings.secret ?? ''),
      enable_private: toBoolean(settings.enable_private, true),
      enable_group: toBoolean(settings.enable_group, true),
    };
    for (const key of ['reply_with_quote', 'reply_with_mention', 'quote_lookup', 'wake_on_quote_self', 'forward_lookup', 'attachment_extract', 'media_insecure_tls', 'media_plaintext_http']) {
      if (settings[key] !== undefined) normalized[key] = toBoolean(settings[key], false);
    }
    for (const key of ['group_whitelist', 'wake_words', 'wake_aliases']) {
      const value = settings[key];
      if (typeof value === 'string') normalized[key] = value.split('\n').map((item) => item.trim()).filter(Boolean);
    }
    for (const key of ['notice_types', 'media_trusted_hosts', 'command_operators']) {
      const value = settings[key];
      if (typeof value !== 'string') continue;
      const items = value.split('\n').map((item) => item.trim()).filter(Boolean);
      if (items.length) normalized[key] = items;
      else delete normalized[key];
    }
    if (typeof settings.asr_model === 'string' && !settings.asr_model.trim()) delete normalized.asr_model;
    if (settings.voice_transcribe === undefined || settings.voice_transcribe === 'asr') delete normalized.voice_transcribe;
    // PLATFORM_NUMERIC_KEYS 已包含文本长度, 频率阈值与冷却等全部数值字段, 不再另列一份
    for (const key of PLATFORM_NUMERIC_KEYS) {
      const value = settings[key];
      // 留空表示未设置 (删除键); 0 是有效取值, 必须按数字保存而不是当作空值丢弃
      const text = typeof value === 'string' ? value.trim() : value;
      if (text === '' || text === undefined || text === null) delete normalized[key];
      else normalized[key] = typeof text === 'string' || typeof text === 'number' ? Number(text) : text;
    }
    for (const key of ['wake_group_overrides', 'wake_time_rules']) {
      const value = settings[key];
      if (typeof value === 'string') {
        normalized[key] = value.trim() ? JSON.parse(value) : key === 'wake_time_rules' ? [] : {};
      }
    }
    if (selfId) normalized.self_id = selfId;
    else delete normalized.self_id;
    return normalized;
  }
  if (type === 'misskey') {
    const normalized: Record<string, unknown> = {
      ...settings,
      base_url: String(settings.base_url ?? ''),
      api_token: String(settings.api_token ?? ''),
      chat_enabled: Boolean(settings.chat_enabled ?? true),
      room_enabled: Boolean(settings.room_enabled ?? false),
      max_message_length: Number(settings.max_message_length ?? 3000),
      misskey_default_visibility: String(settings.misskey_default_visibility ?? 'public'),
      misskey_local_only: Boolean(settings.misskey_local_only ?? false),
    };
    // 命令操作员名单属平台级字段且对全部适配器生效, 表单同样以每行一项提交
    const operators = settings.command_operators;
    if (typeof operators === 'string') {
      const items = operators.split('\n').map((item) => item.trim()).filter(Boolean);
      if (items.length) normalized.command_operators = items;
      else delete normalized.command_operators;
    }
    return normalized;
  }
  return settings;
}

export function appendWithLimit<T>(items: T[], item: T, limit: number): T[] {
  return [...items.slice(-Math.max(0, limit - 1)), item];
}

export function filterLogs<T extends FilterableLog>(
  logs: T[],
  levels: string[],
  searchQuery: string,
): T[] {
  const normalizedQuery = searchQuery.toLowerCase();
  return logs.filter((log) => (
    levels.includes(log.level)
    && (!normalizedQuery || log.content.toLowerCase().includes(normalizedQuery))
  ));
}

export function visibleLogs<T>(logs: T[], pausedLogs: T[], paused: boolean): T[] {
  return paused ? pausedLogs : logs;
}


export function platformSettingsSummary(type: string, settings: Record<string, unknown>): string {
  if (type === 'onebot' || type === 'aiocqhttp') {
    const count = Array.isArray(settings.group_whitelist) ? settings.group_whitelist.length : 0;
    const scopes: Record<string, string> = { legacy_user: '旧用户映射', group_member: '群成员隔离', group: '群共享' };
    const scope = scopes[String(settings.context_scope ?? 'legacy_user')] ?? '未知范围';
    return `私聊${settings.enable_private === false ? '关闭' : '开启'} · 群聊${settings.enable_group === false ? '关闭' : '开启'} · ${count ? `${count} 个允许群` : '允许所有群'} · ${scope}`;
  }
  return `已配置 ${Object.keys(settings).length} 项设置`;
}
