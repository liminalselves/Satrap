// 插件管理页的视图模型: 把目录/健康契约映射为稳定的展示字段
import type { Tint } from '@/components/kit';
import type { CatalogPlugin } from '@/api/pluginsContracts';
import type { PluginCapabilityKind } from '@/api/types';

type CapabilityKind = PluginCapabilityKind;

export const CAPABILITY_KINDS: CapabilityKind[] = ['tools', 'services', 'skills', 'mcp', 'handlers', 'commands'];

export const CAPABILITY_LABELS: Record<CapabilityKind, string> = {
  tools: '工具',
  services: '插件服务',
  skills: '技能',
  mcp: 'MCP',
  handlers: '前处理',
  commands: '命令',
};

export const CAPABILITY_TINT: Record<CapabilityKind, Tint> = {
  tools: 'accent',
  services: 'purple',
  skills: 'green',
  mcp: 'orange',
  handlers: 'pink',
  commands: 'teal',
};

export const SOURCE_LABELS: Record<string, string> = { builtin: '内置', user: '用户' };
export const SESSION_LABELS: Record<string, string> = { chat: 'Chat', platform: '平台会话', embedded: '嵌入会话' };

export type HealthState = 'healthy' | 'attention' | 'disabled' | 'unknown';

export const HEALTH_LABELS: Record<HealthState, string> = {
  healthy: '运行正常',
  attention: '部分异常',
  disabled: '已停用',
  unknown: '健康未知',
};

export const HEALTH_TINTS: Record<HealthState, Tint | 'neutral' | 'red'> = {
  healthy: 'green',
  attention: 'orange',
  disabled: 'neutral',
  unknown: 'neutral',
};

export type Capabilities = Record<string, Record<string, string>>;

export function pluginCapabilities(plugin: Pick<CatalogPlugin, 'capabilities'>): Capabilities {
  return Object.fromEntries(CAPABILITY_KINDS.map((kind) => [kind, plugin.capabilities?.[kind] ?? {}]));
}

export function displayName(plugin: Pick<CatalogPlugin, 'name' | 'display_name'>): string {
  const value = plugin.display_name?.trim();
  return value || plugin.name;
}

export function pluginHealth(plugin: Pick<CatalogPlugin, 'health'>): HealthState {
  const state = plugin.health?.state;
  return state === 'healthy' || state === 'attention' || state === 'disabled' ? state : 'unknown';
}

export function healthLabel(plugin: Pick<CatalogPlugin, 'health'>): string {
  return HEALTH_LABELS[pluginHealth(plugin)];
}

export function hasIssue(plugin: Pick<CatalogPlugin, 'health'>): boolean {
  return pluginHealth(plugin) === 'attention';
}

export function executionEnabled(plugin: Pick<CatalogPlugin, 'execution_enabled'>): boolean | undefined {
  return typeof plugin.execution_enabled === 'boolean' ? plugin.execution_enabled : undefined;
}

export function isEffectivelyEnabled(plugin: Pick<CatalogPlugin, 'execution_enabled'>): boolean {
  return executionEnabled(plugin) !== false;
}

const ACCENTS: Tint[] = ['teal', 'accent', 'purple', 'green', 'orange', 'pink'];

export function pluginAccent(plugin: Pick<CatalogPlugin, 'name' | 'capabilities'>): Tint {
  let hash = 0;
  for (const char of plugin.name) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  if (plugin.capabilities?.skills && Object.keys(plugin.capabilities.skills).length) return 'green';
  if (plugin.capabilities?.mcp && Object.keys(plugin.capabilities.mcp).length) return 'orange';
  return ACCENTS[hash % ACCENTS.length];
}

const ICON_KEYS = ['messages', 'book', 'brain', 'plug', 'search', 'clock', 'shield', 'languages', 'image', 'chart', 'mic'] as const;
export type PluginIconKey = typeof ICON_KEYS[number];

export function pluginIcon(plugin: Pick<CatalogPlugin, 'icon' | 'capabilities'>): PluginIconKey {
  const raw = plugin.icon?.trim().toLowerCase();
  if (raw && (ICON_KEYS as readonly string[]).includes(raw)) return raw as PluginIconKey;
  if (plugin.capabilities?.mcp && Object.keys(plugin.capabilities.mcp).length) return 'plug';
  if (plugin.capabilities?.skills && Object.keys(plugin.capabilities.skills).length) return 'book';
  if (plugin.capabilities?.commands && Object.keys(plugin.capabilities.commands).length) return 'messages';
  if (plugin.capabilities?.tools && Object.keys(plugin.capabilities.tools).length) return 'brain';
  return 'plug';
}

export function capabilityGroups(caps: Capabilities) {
  return CAPABILITY_KINDS
    .map((kind) => ({ kind, label: CAPABILITY_LABELS[kind], items: Object.entries(caps[kind] ?? {}) }))
    .filter((group) => group.items.length > 0);
}

export function capabilityCount(caps: Capabilities): number {
  return CAPABILITY_KINDS.reduce((total, kind) => total + Object.keys(caps[kind] ?? {}).length, 0);
}

export function sessionText(applicability?: { session_types?: string[] }): string {
  const values = applicability?.session_types;
  if (!values?.length) return '未限制';
  return values.map((value) => SESSION_LABELS[value] || value).join('、');
}

export function platformText(applicability?: { platforms?: string[] | '*' }): string {
  const values = applicability?.platforms;
  if (values === '*' || !values || !values.length) return '未限制';
  return values.join('、');
}

/** 仅把 http(s) 地址渲染为链接, 其他文本保持纯文本 */
export function externalUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.toString() : null;
  } catch {
    return null;
  }
}

/** 后端时间为 Unix 秒; 缺失或非法时返回 null, 展示层再决定用 “未知” */
export function timestampText(value?: number | null): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString('zh-CN', { hour12: false });
}

export function compactDate(value?: number | null): string {
  return timestampText(value) ?? '未知';
}
