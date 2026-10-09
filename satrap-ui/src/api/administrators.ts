import axios from 'axios';
import { controlClient } from './control';

export interface AdministratorMember {
  platform_id: string;
  platform_instance_id?: string;
  user_id: string;
}

export interface AdministratorGroup {
  id: string;
  name: string;
  enabled: boolean;
  protect?: boolean;
  members: AdministratorMember[];
  plugin_scope: { mode: 'selected' | 'all'; included: string[]; excluded: string[] };
}

// 成员例外: 对个人的额外允许与否决, 优先于管理组授权裁决
export interface AdministratorOverride {
  id: string;
  enabled: boolean;
  protect?: boolean;
  platform_id: string;
  platform_instance_id?: string;
  user_id: string;
  allow: string[];
  deny: string[];
}

export interface AdministratorRebind {
  group_id: string;
  platform_id: string;
  user_id: string;
}

export interface AdministratorOverrideRebind {
  override_id: string;
  platform_id: string;
  user_id: string;
}

export interface AdministratorInvalidOverride {
  override_id: string;
  platform_id: string;
  user_id: string;
}

export interface AdministratorRuntime {
  status: 'applied' | 'next_start' | 'unconfirmed';
  section_revision?: string;
  runtime_id?: string;
  error?: string;
}

export interface AdministratorPlugin {
  name: string;
  description: string;
  supports_administrators: boolean;
  management_permissions: Record<string, { description: string; system_admin: boolean; requirements?: string[] }>;
}

export interface AdministratorSnapshot {
  groups: AdministratorGroup[];
  overrides: AdministratorOverride[];
  revision: string;
  section_revision: string;
  platforms: Array<{ id: string; type: string; name: string; instance_id: string; enabled: boolean }>;
  plugins: AdministratorPlugin[];
  plugin_errors?: Array<{ name: string; error: string }>;
  invalid_members: AdministratorRebind[];
  invalid_overrides: AdministratorInvalidOverride[];
  migrated_from_legacy?: boolean;
  migration_pending?: string[];
  runtime: AdministratorRuntime;
}

export interface AdministratorPreview {
  members: Array<{
    platform_id: string;
    user_id: string;
    override_id?: string;
    plugins: Array<{
      name: string;
      allowed: boolean;
      group_ids?: string[];
      override_ids?: string[];
      sources?: { allow: string[]; deny: string[] };
      permissions: Array<{ id: string; description: string }>;
    }>;
  }>;
}

const root = '/config/administrator-groups';

export const administratorsApi = {
  read: async () => (await controlClient.get<AdministratorSnapshot>(root)).data,
  save: async (groups: AdministratorGroup[], overrides: AdministratorOverride[], revision: string, rebind: AdministratorRebind[], rebindOverrides: AdministratorOverrideRebind[]) =>
    (await controlClient.put<AdministratorSnapshot>(root, {
      groups, overrides, expected_revision: revision, rebind_members: rebind, rebind_overrides: rebindOverrides,
    })).data,
  preview: async (groups: AdministratorGroup[], overrides: AdministratorOverride[], rebind: AdministratorRebind[], rebindOverrides: AdministratorOverrideRebind[]) =>
    (await controlClient.post<AdministratorPreview>(root + '/preview', {
      groups, overrides, rebind_members: rebind, rebind_overrides: rebindOverrides,
    })).data,
  apply: async (revision: string) =>
    (await controlClient.post<{ runtime: AdministratorRuntime }>(root + '/apply', { section_revision: revision })).data,
};

export function administratorError(cause: unknown): string {
  if (axios.isAxiosError(cause)) return cause.response?.data?.error || cause.message;
  return cause instanceof Error ? cause.message : '管理员设置请求失败';
}

export function administratorRuntimeLabel(runtime: AdministratorRuntime): string {
  if (runtime.status === 'applied') return '已保存并生效';
  if (runtime.status === 'next_start') return '已保存, 后端启动后生效';
  return '已保存, 无法确认运行时生效';
}
