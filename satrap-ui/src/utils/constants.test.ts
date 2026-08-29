import { describe, expect, it } from 'vitest';
import { getThinkingOptions } from './constants';

describe('getThinkingOptions', () => {
  it('filters configured levels for strength fields', () => {
    expect(getThinkingOptions({
      thinking_fields: ['thinking.type', 'reasoning_effort'],
      thinking_levels: ['low', 'xhigh', 'ultra'],
    })).toEqual([
      { value: 'off', label: '关闭' },
      { value: 'low', label: '低' },
      { value: 'xhigh', label: '高+' },
      { value: 'ultra', label: '最高' },
    ]);
  });

  it('uses the standard three levels for an existing config without metadata', () => {
    expect(getThinkingOptions({ thinking_fields: ['thinking_level'] })).toEqual([
      { value: 'off', label: '关闭' },
      { value: 'low', label: '低' },
      { value: 'medium', label: '中' },
      { value: 'high', label: '高' },
    ]);
  });

  it('shows one enabled state for switch-only models', () => {
    expect(getThinkingOptions({ thinking_fields: ['thinking.type'] })).toEqual([
      { value: 'off', label: '关闭' },
      { value: 'high', label: '开启' },
    ]);
  });

  it('keeps only off when the model has no thinking request field', () => {
    expect(getThinkingOptions()).toEqual([{ value: 'off', label: '关闭' }]);
  });
});
