import { describe, expect, it } from 'vitest';
import { readEdictumParams, writeEdictumParams } from './edictumParams';

describe('Edictum 提示词配置', () => {
  it('从旧 JSON 读取提示词并保留其他参数', () => {
    const form = readEdictumParams({ system_prompt: '助手', custom: { a: 1 } }, true);
    expect(form.system_prompt).toBe('助手');
    expect(form.system_prompt_enabled).toBe(true);
    expect(JSON.parse(form.params)).toEqual({ custom: { a: 1 } });
    expect(writeEdictumParams(form, true)).toEqual({ system_prompt: '助手', custom: { a: 1 } });
  });

  it('区分主动清空与未配置, 拒绝重复入口', () => {
    const form = { params: '{}', system_prompt: '', system_prompt_enabled: true };
    expect(writeEdictumParams(form, true)).toEqual({ system_prompt: '' });
    expect(writeEdictumParams({ ...form, system_prompt_enabled: false }, true)).toEqual({});
    expect(() => writeEdictumParams({ ...form, params: '{"system_prompt":"重复"}' }, true)).toThrow('输入框');
  });

  it('自定义会话类型继续使用原始参数', () => {
    const form = readEdictumParams({ system_prompt: '自定义', custom: 3 }, false);
    expect(writeEdictumParams(form, false)).toEqual({ system_prompt: '自定义', custom: 3 });
  });

  it('迁移旧生成参数, 保留零温度并支持恢复继承', () => {
    const form = readEdictumParams({ thinking: 'high', temperature: 0, model_params: { top_p: 0.8, max_tokens: 2048 }, custom: 1 }, true);
    expect(writeEdictumParams(form, true)).toEqual({ thinking: 'high', model_params: { temperature: 0, top_p: 0.8, max_tokens: 2048 }, custom: 1 });
    expect(writeEdictumParams({ ...form, thinking: '', temperature: undefined, top_p: undefined, max_tokens: undefined }, true)).toEqual({ custom: 1 });
    expect(() => writeEdictumParams({ ...form, top_p: 0 }, true)).toThrow('top_p');
    expect(() => writeEdictumParams({ ...form, max_tokens: 1.5 }, true)).toThrow('正整数');
  });
});
