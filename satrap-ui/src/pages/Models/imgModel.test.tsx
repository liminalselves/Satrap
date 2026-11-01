import { describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { incompatibleImgFields, normalizeImgConfig, readImgParams, writeImgParams } from './imgModel';
import { ImgConfigModal } from './ImgConfigModal';
import { PluginConfigFields } from '@/components/common/PluginConfigFields';
import { pluginOptionNeeds } from '@/hooks/usePluginConfigOptions';
import { MODEL_TYPES } from '@/utils/constants';

vi.mock('@/components/ui/Modal', () => ({ Modal: ({ children }: { children: ReactNode }) => <div role="dialog">{children}</div> }));

const config = { name: 'paint', model: 'nai-diffusion-4-5-full', api_type: 'novelai' as const,
  base_url: 'https://image.test/exact/?v=1', api_key: 'new-key' };

describe('图片模型管理契约', () => {
  it('保留完整请求地址, 0 和扩展参数, 不泄漏旧脱敏密钥', () => {
    expect(normalizeImgConfig({ ...config, api_key: '****key' }, '{"seed":0,"steps":20,"native":{"parameters":{"future":false}}}', false))
      .toEqual({ ...config, api_key: undefined, timeout: 180, edit_base_url: null,
        default_params: { seed: 0, steps: 20, native: { parameters: { future: false } } } });
  });
  it('空值显式清除编辑地址和默认参数', () => {
    expect(normalizeImgConfig({ ...config, edit_base_url: '' }, '', false)).toMatchObject({ edit_base_url: null, default_params: null });
  });
  it.each(['[]', 'null', 'wrong', '3'])('非法默认参数 %s 阻止保存', (text) => {
    expect(() => normalizeImgConfig(config, text, true)).toThrow();
  });
  it('创建要求真实密钥, 编辑留空保留旧密钥', () => {
    expect(() => normalizeImgConfig({ ...config, api_key: '' }, '{}', true)).toThrow('API Key');
    expect(normalizeImgConfig({ ...config, api_key: '' }, '{}', false)).not.toHaveProperty('api_key');
  });
  it('表单展示协议和完整端点, 不回填脱敏密钥', () => {
    const markup = renderToStaticMarkup(<ImgConfigModal name="paint" initialConfig={{ ...config, api_key: '****key' }}
      onClose={() => {}} onSave={async () => true} />);
    expect(markup).toContain('生成请求地址');
    expect(markup).toContain('编辑请求地址');
    expect(markup).toContain('NovelAI');
    expect(markup).not.toContain('****key');
  });
  it('插件 img 字段使用模型选择器和按需读取', () => {
    const schema = { painter: { type: 'img', label: '绘图模型', default: '', description: '' } };
    expect(pluginOptionNeeds(schema)).toEqual({ models: true, knowledge: false });
    const markup = renderToStaticMarkup(<PluginConfigFields schema={schema} values={{ painter: 'paint' }}
      modelOptions={{ img: [{ value: 'paint', label: '画师' }] }} onChange={() => {}} />);
    expect(markup).toContain('role="combobox"');
    expect(markup).toContain('画师');
    expect(MODEL_TYPES.some((item) => item.value === 'img')).toBe(true);
  });

  it('旧参数回填再保存保留尺寸字符串, 关闭开关和扩展字段', () => {
    const params = { size: '832x1216', steps: 28, scale: 5, seed: 0, sm: false, output_format: 'webp',
      characters: [{ prompt: 'woman', negative_prompt: '', centers: [[0, 0.5], [1, 0.7]] }],
      native: { parameters: { future: false } }, extra_noise_seed: 0, future_parameter: { nested: [1, 2] } };
    const state = readImgParams(params);
    expect(state.resolution).toMatchObject({ mode: 'pixels', width: '832', height: '1216' });
    expect(JSON.parse(state.extraText)).not.toHaveProperty('steps');
    expect(writeImgParams(state, 'novelai')).toEqual(params);
    state.values.steps = 30;
    expect(params.steps).toBe(28);
  });

  it('像素表单和分辨率档位都保存为既有 size 字段', () => {
    const state = readImgParams();
    state.resolution = { ...state.resolution, mode: 'pixels', width: '512', height: '768', changed: true };
    expect(writeImgParams(state, 'novelai')).toEqual({ size: [512, 768] });
    state.resolution = { ...state.resolution, mode: 'value', value: '2K', changed: true };
    expect(writeImgParams(state, 'seedream')).toEqual({ size: '2K' });
    expect(() => writeImgParams(state, 'novelai')).toThrow('宽高像素');
    state.resolution.mode = 'default';
    expect(writeImgParams(state, 'novelai')).toBeNull();
  });

  it.each(['', '0', '-1', '12.5'])('像素值 %s 非法时阻止保存', (width) => {
    const state = readImgParams();
    state.resolution = { ...state.resolution, mode: 'pixels', width, height: '768', changed: true };
    expect(() => writeImgParams(state, 'novelai')).toThrow('正整数');
  });

  it('清除分辨率不影响其它参数, 随机种子不发送 -1', () => {
    const state = readImgParams({ size: [512, 768], seed: 0, steps: 20 });
    state.resolution = { ...state.resolution, mode: 'default', changed: true };
    expect(writeImgParams(state, 'novelai')).toEqual({ seed: 0, steps: 20 });
    delete state.values.seed;
    expect(writeImgParams(state, 'novelai')).toEqual({ steps: 20 });
    expect(writeImgParams(readImgParams(), 'novelai')).toBeNull();
  });

  it.each(['size', 'seed', 'steps', 'characters', 'sm'])('扩展 JSON 不允许重复配置控件字段 %s', (key) => {
    const state = readImgParams({ steps: 20 });
    state.extraText = JSON.stringify({ [key]: null });
    expect(() => writeImgParams(state, 'novelai')).toThrow('不要重复');
  });

  it('协议切换不丢弃默认值, 不兼容参数明确阻止保存', () => {
    const params = { seed: 0, steps: 20, sm: false, native: { custom: 1 } };
    const state = readImgParams(params);
    expect(incompatibleImgFields(state.values, 'openai_images')).toEqual(['seed', 'steps', 'sm']);
    expect(() => writeImgParams(state, 'openai_images')).toThrow('不支持');
    expect(writeImgParams(state, 'novelai')).toEqual(params);
  });

  it.each([
    { size: [512, 768], native: { parameters: { width: 512 } } },
    { steps: 20, native: { parameters: { steps: 30 } } },
    { n: 1, native: { parameters: { n_samples: 2 } } },
  ])('原生 JSON 与表单字段重复时保留输入并阻止提交', (params) => {
    const state = readImgParams(params);
    expect(() => writeImgParams(state, 'novelai')).toThrow('同时存在');
    expect(JSON.parse(state.extraText)).toEqual({ native: params.native });
  });

  it.each([{ n: 0 }, { n: 33 }, { seed: -1 }, { seed: 2 ** 32 }, { steps: 1.5 }, { scale: Infinity },
    { characters: [{ prompt: '' }] }, { characters: [{ prompt: 'woman', centers: [[2, 0]] }] }])('非法表单值 %j 不提交', (params) => {
    expect(() => writeImgParams(readImgParams(params), 'novelai')).toThrow();
  });

  it('Seedream 不将生成数量当作组图数量, 不兼容格式要求修改', () => {
    expect(() => writeImgParams(readImgParams({ n: 2 }), 'seedream')).toThrow('不支持');
    expect(() => writeImgParams(readImgParams({ output_format: 'jpeg' }), 'novelai')).toThrow('PNG');
  });

  it('表单回填角色列表和尺寸时不要求填写 JSON', () => {
    const markup = renderToStaticMarkup(<ImgConfigModal name="paint" initialConfig={{ ...config,
      default_params: { size: [832, 1216], seed: 0, steps: 28, sm: false, characters: [{ prompt: 'blue hair' }] } }}
      onClose={() => {}} onSave={async () => true} />);
    expect(markup).toContain('图片宽度 (像素)');
    expect(markup).toContain('采样步数');
    expect(markup).toContain('固定随机种子');
    expect(markup).toContain('角色 1 提示词');
    expect(markup).toContain('blue hair');
    expect(markup).not.toContain('默认绘图参数 (JSON)');
  });
});
