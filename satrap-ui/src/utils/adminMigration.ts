export interface FilterableLog {
  content: string;
  level: string;
}

export function classNameToConfigName(className: string): string {
  return className.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().replace(/_session$/, '');
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
      enable_private: Boolean(settings.enable_private ?? true),
      enable_group: Boolean(settings.enable_group ?? true),
    };
    for (const key of ['reply_with_quote', 'reply_with_mention', 'quote_lookup', 'wake_on_quote_self']) {
      if (settings[key] !== undefined) normalized[key] = Boolean(settings[key]);
    }
    for (const key of ['group_whitelist', 'wake_words', 'wake_aliases']) {
      const value = settings[key];
      if (typeof value === 'string') normalized[key] = value.split('\n').map((item) => item.trim()).filter(Boolean);
    }
    if (typeof settings.notice_types === 'string') {
      const items = settings.notice_types.split('\n').map((item) => item.trim()).filter(Boolean);
      if (items.length) normalized.notice_types = items;
      else delete normalized.notice_types;
    }
    for (const key of ['message_text_limit', 'wake_message_threshold', 'wake_cooldown', 'wake_score_threshold', 'wake_max_wait']) {
      const value = settings[key];
      if (value === '' || value === undefined) delete normalized[key];
      else normalized[key] = Number(value);
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
