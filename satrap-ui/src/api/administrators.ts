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
  members: AdministratorMember[];
  plugin_scope: { mode: 'selected' | 'all'; included: string[]; excluded: string[] };
}

export interface AdministratorRebind {
  group_id: string;
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
  revision: string;
  section_revision: string;
  platforms: Array<{ id: string; type: string; name: string; instance_id: string; enabled: boolean }>;
  plugins: AdministratorPlugin[];
  plugin_errors?: Array<{ name: string; error: string }>;
  invalid_members: AdministratorRebind[];
  runtime: AdministratorRuntime;
}

export interface AdministratorPreview {
  members: Array<{ platform_id: string; user_id: string; plugins: Array<{ name: string; permissions: Array<{ id: string; description: string }> }> }>;
}

const root = '/config/administrator-groups';

export const administratorsApi = {
  read: async () => (await controlClient.get<AdministratorSnapshot>(root)).data,
  save: async (groups: AdministratorGroup[], revision: string, rebind: AdministratorRebind[]) =>
    (await controlClient.put<AdministratorSnapshot>(root, { groups, expected_revision: revision, rebind_members: rebind })).data,
  preview: async (groups: AdministratorGroup[], rebind: AdministratorRebind[]) =>
    (await controlClient.post<AdministratorPreview>(root + '/preview', { groups, rebind_members: rebind })).data,
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
