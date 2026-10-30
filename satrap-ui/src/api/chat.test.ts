import { afterEach, describe, expect, it, vi } from 'vitest';

import { chatApi, type ChatPreloadSettings, type ModelConfigItem } from './chat';
vi.mock('./auth', () => ({ ensureApiSession: vi.fn().mockResolvedValue(undefined), establishApiSession: vi.fn().mockResolvedValue(undefined) }));

const SETTINGS: ChatPreloadSettings = {
  model: 'deepseek-v4-flash',
  think: 'high',
  temperature: 0.35,
  systemPrompt: '测试提示词',
  defaultAgentPromptEnabled: true,
  projectId: 'project-1',
};

function mockFetch(payload: object = { ok: true, conversation_id: 'conv-1' }) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true,
    json: async () => payload,
  } as Response);
}

describe('chatApi conversation preload', () => {
  it('整体默认采用独立修订, 会话切换和重试携带已确认绑定', async () => {
    const fetchSpy = mockFetch({ default_model: 'fast', revision: 'r2', ok: true, model: 'fast', available: true });
    await chatApi.saveSettings('fast', 'r1');
    expect(JSON.parse(String(fetchSpy.mock.calls[0][1]?.body))).toEqual({ default_model: 'fast', expected_revision: 'r1' });
    fetchSpy.mockClear();
    await chatApi.setModel('conv & a', 'fast', 'default');
    expect(String(fetchSpy.mock.calls[0][0])).toContain('conversation=conv%20%26%20a');
    expect(JSON.parse(String(fetchSpy.mock.calls[0][1]?.body))).toEqual({ model: 'fast', expected_model: 'default' });
    fetchSpy.mockClear();
    await chatApi.send('conv & a', '问题', 'off', undefined, undefined, 'fast');
    expect(JSON.parse(String(fetchSpy.mock.calls[0][1]?.body)).expected_model).toBe('fast');
    fetchSpy.mockClear();
    await chatApi.retry('conv & a', 'off', 'fast');
    expect(JSON.parse(String(fetchSpy.mock.calls[0][1]?.body))).toEqual({ conversation: 'conv & a', think: 'off', expected_model: 'fast' });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('读写审批模式携带当前会话和权限版本', async () => {
    const fetchSpy = mockFetch({ ok: true, available: true, mode: 'auto', revision: 2, plan_mode: false });
    await chatApi.getApproval('conv & test');
    expect(String(fetchSpy.mock.calls[0][0])).toContain('conversation=conv+%26+test');
    fetchSpy.mockClear();
    const state = await chatApi.setApproval('conv & test', 'auto', 1);
    expect(state.mode).toBe('auto');
    expect(fetchSpy.mock.calls[0][1]?.method).toBe('PUT');
    expect(JSON.parse(String(fetchSpy.mock.calls[0][1]?.body))).toEqual({ mode: 'auto', expected_revision: 1 });
  });

  it('媒体只读请求传递取消信号, 切页可撤销网络读取', async () => {
    const fetchSpy = mockFetch({ data_url: 'data:image/png;base64,test' });
    const controller = new AbortController();
    await chatApi.previewMedia('conv-1', '/archive/test.png', controller.signal);
    expect(fetchSpy.mock.calls[0][0]).toContain('/api/chat/media?conversation=conv-1&source=');
    expect(fetchSpy.mock.calls[0][1]?.signal).toBe(controller.signal);
  });

  it('sends complete runtime settings when preloading', async () => {
    const fetchSpy = mockFetch();

    await chatApi.preloadConversation(SETTINGS);

    expect(fetchSpy).toHaveBeenCalledOnce();
    const [, request] = fetchSpy.mock.calls[0];
    expect(JSON.parse(String(request?.body))).toEqual({
      model: 'deepseek-v4-flash',
      think: 'high',
      temperature: 0.35,
      system_prompt: '测试提示词',
      default_agent_prompt_enabled: true,
      project_id: 'project-1',
    });
  });

  it('repeats current settings on the first send for backend validation', async () => {
    const fetchSpy = mockFetch({ ok: true });

    await chatApi.send('conv-1', '你好', 'high', undefined, { ...SETTINGS, approvalMode: 'auto' });

    const [, request] = fetchSpy.mock.calls[0];
    expect(JSON.parse(String(request?.body))).toMatchObject({
      conversation: 'conv-1',
      text: '你好',
      think: 'high',
      model: 'deepseek-v4-flash',
      temperature: 0.35,
      system_prompt: '测试提示词',
      default_agent_prompt_enabled: true,
      project_id: 'project-1',
      approval_mode: 'auto',
    });
  });

  it('内置提示开关只更新指定会话', async () => {
    const fetchSpy = mockFetch({ ok: true, default_agent_prompt_enabled: false });
    await chatApi.setDefaultAgentPrompt('conv & test', false);
    expect(String(fetchSpy.mock.calls[0][0])).toContain('conversation=conv%20%26%20test');
    expect(fetchSpy.mock.calls[0][1]?.method).toBe('PUT');
    expect(JSON.parse(String(fetchSpy.mock.calls[0][1]?.body))).toEqual({ default_agent_prompt_enabled: false });
  });

  it('submits an ask_user answer to the pending tool request', async () => {
    const fetchSpy = mockFetch({ ok: true });

    await chatApi.answerAskUser('conv-1', 'request-1', '继续');

    const [url, request] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/chat/ask-user/answer');
    expect(JSON.parse(String(request?.body))).toEqual({
      conversation: 'conv-1',
      request_id: 'request-1',
      answer: '继续',
    });
  });

  it('retries and selects a persisted response variant', async () => {
    const fetchSpy = mockFetch({
      ok: true,
      turn_id: 3,
      turn_index: 1,
      variant_index: 1,
    });

    await chatApi.retry('conv-1', 'high');
    let [url, request] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/chat/retry');
    expect(JSON.parse(String(request?.body))).toEqual({
      conversation: 'conv-1',
      think: 'high',
    });

    fetchSpy.mockClear();
    await chatApi.selectVariant('conv-1', 1, 0);
    [url, request] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/chat/turns/variant');
    expect(JSON.parse(String(request?.body))).toEqual({
      conversation: 'conv-1',
      turn_index: 1,
      variant_index: 0,
    });
  });

  it('queries and batch deletes managed history', async () => {
    const fetchSpy = mockFetch({
      items: [], total: 0, page: 1, page_size: 50, storage_size_bytes: 0, mode: 'hot',
    });

    await chatApi.queryHistory({ search: '测试', turn_count: 'single', page: 2 });

    const [queryUrl] = fetchSpy.mock.calls[0];
    expect(String(queryUrl)).toContain('/api/chat/history?');
    expect(String(queryUrl)).toContain('search=%E6%B5%8B%E8%AF%95');
    expect(String(queryUrl)).toContain('turn_count=single');

    fetchSpy.mockClear();
    await chatApi.deleteHistory({ mode: 'selected', conversation_ids: ['conv-1'] });

    const [deleteUrl, request] = fetchSpy.mock.calls[0];
    expect(String(deleteUrl)).toContain('/api/chat/history/delete');
    expect(JSON.parse(String(request?.body))).toEqual({
      mode: 'selected',
      conversation_ids: ['conv-1'],
    });
  });

  it('sends checkbox thinking fields and none omission when saving a model', async () => {
    const fetchSpy = mockFetch({ ok: true });
    const config: ModelConfigItem = {
      name: 'deepseek-v4-flash',
      model: 'deepseek-v4-flash',
      thinking_field_name: 'reasoning_content',
      thinking_fields: ['thinking.type', 'reasoning_effort'],
      thinking_levels: ['low', 'high', 'xhigh'],
      omit_none_thinking_fields: true,
    };

    await chatApi.addModel(config);

    const [url, request] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/chat/models');
    expect(JSON.parse(String(request?.body))).toEqual(config);
  });
});
