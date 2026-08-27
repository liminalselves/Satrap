import { describe, expect, it } from 'vitest';

import {
  parseEdictumConfigs,
  parseEdictumTypes,
  parseModelConfigs,
  parseSessionClassConfigs,
} from './control';

describe('parseModelConfigs', () => {
  it('accepts a named model configuration map', () => {
    const configs = {
      default: {
        name: 'default',
        model: 'test-model',
      },
    };

    expect(parseModelConfigs(configs)).toEqual(configs);
  });

  it('rejects an HTML response instead of treating characters as configs', () => {
    expect(() => parseModelConfigs('<!DOCTYPE html>')).toThrow(TypeError);
  });

  it('rejects arrays instead of treating indexes as config names', () => {
    expect(() => parseModelConfigs([])).toThrow(TypeError);
  });
});

describe('parseSessionClassConfigs', () => {
  it('accepts a named session class configuration map', () => {
    const configs = {
      demo: {
        class_path: 'example.DemoSession',
        is_async: false,
        enabled: true,
      },
    };

    expect(parseSessionClassConfigs(configs)).toEqual(configs);
  });

  it('rejects an HTML response instead of treating characters as configs', () => {
    expect(() => parseSessionClassConfigs('<!DOCTYPE html>')).toThrow(TypeError);
  });
});

describe('Edictum response parsers', () => {
  it('accepts registered type metadata and named configs', () => {
    const types = {
      types: [{
        name: 'async_simple',
        is_async: true,
        description: 'test',
        config_schema: {},
        capabilities: { plugins: true, mcp: true, stream: true },
      }],
    };
    const configs = {
      assistant: {
        provider: 'edictum',
        edictum_type: 'async_simple',
        enabled: true,
        description: '',
        model_name: 'default',
        params: {},
        plugins: [],
      },
    };

    expect(parseEdictumTypes(types)).toEqual(types.types);
    expect(parseEdictumConfigs(configs)).toEqual(configs);
  });

  it('rejects malformed type and config payloads', () => {
    expect(() => parseEdictumTypes({ types: {} })).toThrow(TypeError);
    expect(() => parseEdictumConfigs([])).toThrow(TypeError);
    expect(() => parseEdictumConfigs({ invalid: 'value' })).toThrow(TypeError);
  });
});
