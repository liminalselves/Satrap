import { afterEach, describe, expect, it, vi } from 'vitest';

import { chatApi, type ChatPreloadSettings, type ModelConfigItem } from './chat';

const SETTINGS: ChatPreloadSettings = {
  model: 'deepseek-v4-flash',
  think: 'high',
  temperature: 0.35,
  systemPrompt: '测试提示词',
  projectId: 'project-1',
};

function mockFetch(payload: object = { ok: true, conversation_id: 'conv-1' }) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true,
    json: async () => payload,
  } as Response);
}

describe('chatApi conversation preload', () => {
  afterEach(() => {
    vi.restoreAllMocks();
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
      project_id: 'project-1',
    });
  });

  it('repeats current settings on the first send for backend validation', async () => {
    const fetchSpy = mockFetch({ ok: true });

    await chatApi.send('conv-1', '你好', 'high', undefined, SETTINGS);

    const [, request] = fetchSpy.mock.calls[0];
    expect(JSON.parse(String(request?.body))).toMatchObject({
      conversation: 'conv-1',
      text: '你好',
      think: 'high',
      model: 'deepseek-v4-flash',
      temperature: 0.35,
      system_prompt: '测试提示词',
      project_id: 'project-1',
    });
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
