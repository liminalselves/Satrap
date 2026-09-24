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

export interface PolicyRange {
  key: string;
  label: string;
  min: number;
  max: number;
  integer: boolean;
}

// 平台级数值策略字段: 与后端 validate_wake_policy 的范围一致, 仅平台级生效 (不进入群/时段覆盖)
export const PLATFORM_NUMERIC_KEYS = ['input_text_limit', 'input_media_limit', 'wake_talk_value'] as const;

export const POLICY_RANGES: PolicyRange[] = [
  { key: 'message_text_limit', label: '每条消息文本上限', min: 64, max: 32000, integer: true },
  { key: 'input_text_limit', label: '单条消息输入文本预算', min: 1, max: 200000, integer: true },
  { key: 'input_media_limit', label: '单条消息输入媒体上限', min: 1, max: 32, integer: true },
  { key: 'wake_message_threshold', label: '自动参与消息阈值', min: 1, max: 32, integer: true },
  { key: 'wake_cooldown', label: '自动参与冷却秒数', min: 0, max: Number.MAX_SAFE_INTEGER, integer: false },
  { key: 'wake_max_wait', label: '最长等待秒数', min: 0, max: 119.999, integer: false },
  { key: 'wake_score_threshold', label: '必要性评分阈值', min: 0, max: 1, integer: false },
  { key: 'wake_talk_value', label: '发言频率偏好', min: 0, max: 1, integer: false },
];

export function validatePlatformPolicyRanges(settings: Record<string, unknown>): string | null {
  for (const range of POLICY_RANGES) {
    const value = settings[range.key];
    if (value === undefined || value === null || value === '') continue;
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return `${range.label}必须为数字（${range.key}）`;
    if (range.integer && !Number.isInteger(numeric)) return `${range.label}必须为整数（${range.key}）`;
    if (numeric < range.min || numeric > range.max) {
      return `${range.label}必须在 ${range.min} 到 ${range.max} 之间（${range.key}）`;
    }
  }
  return null;
}

// 平台表单里 talk_value 与显式阈值的优先级提示, 与后端阈值解析保持同一结论
export function talkValuePriorityHint(settings: Record<string, unknown>): string {
  const talk = settings.wake_talk_value;
  const explicit = settings.wake_message_threshold;
  const hasTalk = talk !== undefined && talk !== null && talk !== '';
  const hasExplicit = explicit !== undefined && explicit !== null && explicit !== '';
  if (!hasTalk) return '留空表示未设置: 频率模式使用默认 3 条阈值';
  if (hasExplicit) return `已设置 wake_message_threshold=${String(explicit)}, talk_value 不参与频率判断（被显式阈值覆盖）`;
  if (Number(talk) === 0) return '0 表示关闭自动参与: 频率模式不触发, 到期最长等待也不会补偿';
  return '未设置显式阈值时, 由该偏好映射自动参与的条数阈值';
}

export function normalizePlatformSettings(
  type: string,
  settings: Record<string, unknown>,
): Record<string, unknown> {
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
    for (const key of ['notice_types', 'media_trusted_hosts']) {
      const value = settings[key];
      if (typeof value !== 'string') continue;
      const items = value.split('\n').map((item) => item.trim()).filter(Boolean);
      if (items.length) normalized[key] = items;
      else delete normalized[key];
    }
    if (typeof settings.asr_model === 'string' && !settings.asr_model.trim()) delete normalized.asr_model;
    if (settings.voice_transcribe === undefined || settings.voice_transcribe === 'asr') delete normalized.voice_transcribe;
    for (const key of ['message_text_limit', 'wake_message_threshold', 'wake_cooldown', 'wake_score_threshold', 'wake_max_wait', ...PLATFORM_NUMERIC_KEYS]) {
      const value = settings[key];
      // 留空表示未设置 (删除键); 0 是有效取值, 必须按数字保存而不是当作空值丢弃
      const text = typeof value === 'string' ? value.trim() : value;
      if (text === '' || text === undefined || text === null) delete normalized[key];
      else normalized[key] = Number(text);
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
    return {
      ...settings,
      base_url: String(settings.base_url ?? ''),
      api_token: String(settings.api_token ?? ''),
      chat_enabled: Boolean(settings.chat_enabled ?? true),
      room_enabled: Boolean(settings.room_enabled ?? false),
      max_message_length: Number(settings.max_message_length ?? 3000),
      misskey_default_visibility: String(settings.misskey_default_visibility ?? 'public'),
      misskey_local_only: Boolean(settings.misskey_local_only ?? false),
    };
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
