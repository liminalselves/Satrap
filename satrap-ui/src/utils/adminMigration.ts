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
  if (type === 'onebot') {
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
