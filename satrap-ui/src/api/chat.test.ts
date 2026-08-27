import { afterEach, describe, expect, it, vi } from 'vitest';

import { chatApi, type ChatPreloadSettings } from './chat';

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
});
