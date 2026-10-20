import { controlClient } from './control';

export interface ManagedSkill {
  name: string;
  description: string;
  markdown: string;
  source: string;
  revision: number;
  enabled: boolean;
  scopes: string[];
}

export interface SkillDraft {
  name?: string;
  description?: string;
  markdown?: string;
  filename?: string;
  url?: string;
  expected_revision?: number;
  enabled?: boolean;
  scopes?: string[];
}

export const skillsApi = {
  list: async (signal?: AbortSignal): Promise<ManagedSkill[]> => (await controlClient.get('/api/skills', { signal })).data.items,
  create: async (draft: SkillDraft): Promise<ManagedSkill> => (await controlClient.post('/api/skills', draft)).data.item,
  update: async (name: string, draft: SkillDraft): Promise<ManagedSkill> => (await controlClient.put(`/api/skills/${encodeURIComponent(name)}`, draft)).data.item,
  delete: async (skill: ManagedSkill): Promise<void> => {
    await controlClient.delete(`/api/skills/${encodeURIComponent(skill.name)}`, { data: { expected_revision: skill.revision } });
  },
};
