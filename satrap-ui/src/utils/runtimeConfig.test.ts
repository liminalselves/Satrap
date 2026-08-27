import { describe, expect, it } from 'vitest';

import { resolveRuntimeConfig } from './constants';

describe('resolveRuntimeConfig', () => {
  it('uses the default control endpoint when runtime discovery fails', () => {
    expect(resolveRuntimeConfig(null, 'http://127.0.0.1:5173')).toEqual({
      backend_api: 'http://127.0.0.1:19870',
      control_api: 'http://127.0.0.1:19871',
      chat_api: 'http://127.0.0.1:19872',
    });
  });

  it('uses discovered endpoints and the current page origin for control fallback', () => {
    expect(resolveRuntimeConfig({
      backend_api: 'http://127.0.0.1:29970',
      chat_api: 'http://127.0.0.1:29972',
    }, 'http://127.0.0.1:29971')).toEqual({
      backend_api: 'http://127.0.0.1:29970',
      control_api: 'http://127.0.0.1:29971',
      chat_api: 'http://127.0.0.1:29972',
    });
  });

  it('keeps build-time overrides as the highest priority', () => {
    expect(resolveRuntimeConfig({
      backend_api: 'http://discovered:1',
      control_api: 'http://discovered:2',
      chat_api: 'http://discovered:3',
    }, 'http://page', {
      backend: 'http://override:1',
      control: 'http://override:2',
      chat: 'http://override:3',
    })).toEqual({
      backend_api: 'http://override:1',
      control_api: 'http://override:2',
      chat_api: 'http://override:3',
    });
  });
});
