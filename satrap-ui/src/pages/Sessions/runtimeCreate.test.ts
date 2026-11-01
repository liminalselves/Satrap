import { describe, expect, it } from 'vitest';
import type { AgentEntry } from './shared';
import { buildCreateRuntimePayload, resolveEdictumPluginTarget, type CreateSessionInput } from './runtimeCreate';

const agent = (provider: AgentEntry['provider'], name = 'assistant'): AgentEntry => ({
  name,
  provider,
  enabled: true,
  description: '',
  model_name: 'fast',
  edictum_type: provider === 'edictum' ? 'simple' : undefined,
  class_path: provider === 'session_class' ? 'pkg.Assistant' : undefined,
  params: {},
  plugins: [],
  activeInstances: 0,
  runningInstances: 0,
  updatedAt: null,
});

const input = (overrides: Partial<CreateSessionInput> = {}): CreateSessionInput => ({
  session_id: 'g8823-assistant',
  adapter_id: '',
  llm_name: 'deep',
  params: { mode: 'test' },
  ...overrides,
});

describe('Sessions 创建请求', () => {
  it('Edictum 创建透传 session_id, llm_name 与 params', () => {
    const payload = buildCreateRuntimePayload(agent('edictum'), input(), { platformId: 'onebot-main', running: true });
    expect(payload).toEqual({
      session_provider: 'edictum',
      session_type: 'assistant',
      session_id: 'g8823-assistant',
      platform_id: 'onebot-main',
      adapter_id: 'onebot-main',
      llm_name: 'deep',
      params: { mode: 'test' },
      activate: true,
    });
  });

  it('多平台表单保留所选平台与已填字段', () => {
    const payload = buildCreateRuntimePayload(
      agent('edictum'),
      input({ platform_id: 'misskey-main', session_id: 'custom', llm_name: 'other', params: { a: 1 } }),
      { platformId: 'onebot-main', running: false },
    );
    expect(payload.platform_id).toBe('misskey-main');
    expect(payload.adapter_id).toBe('misskey-main');
    expect(payload.session_id).toBe('custom');
    expect(payload.llm_name).toBe('other');
    expect(payload.params).toEqual({ a: 1 });
    expect(payload.activate).toBe(false);
  });

  it('未选择平台时回退到配置绑定平台, 空字段不提交', () => {
    const payload = buildCreateRuntimePayload(
      agent('edictum'),
      input({ session_id: '  ', llm_name: '' }),
      { platformId: 'onebot-main', running: true },
    );
    expect(payload.platform_id).toBe('onebot-main');
    expect(payload).not.toHaveProperty('session_id');
    expect(payload).not.toHaveProperty('llm_name');
  });

  it('会话类创建继续携带适配器字段', () => {
    const payload = buildCreateRuntimePayload(
      agent('session_class'),
      input({ adapter_id: 'adapter-1' }),
      { running: true },
    );
    expect(payload).toEqual({
      session_provider: 'session_class',
      session_type: 'assistant',
      session_id: 'g8823-assistant',
      adapter_id: 'adapter-1',
      llm_name: 'deep',
      params: { mode: 'test' },
    });
  });

  it('插件目标只解析 Edictum, 同名会话类不会抢占', () => {
    const sameNameSessionClass = agent('session_class');
    const sameNameEdictum = agent('edictum');
    expect(resolveEdictumPluginTarget('assistant', [sameNameSessionClass, sameNameEdictum])?.provider).toBe('edictum');
    expect(resolveEdictumPluginTarget('assistant', [sameNameSessionClass])).toBeNull();
    expect(resolveEdictumPluginTarget(null, [sameNameEdictum])).toBeNull();
  });
});
