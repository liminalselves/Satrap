import type { ImgConfig } from '@/api/types';

export const IMG_PARAM_FIELDS = [
  'size', 'n', 'seed', 'output_format', 'negative_prompt', 'steps', 'scale', 'sampler',
  'cfg_rescale', 'noise_schedule', 'sm', 'sm_dyn', 'dynamic_thresholding', 'strength', 'noise',
  'color_correct', 'add_original_image', 'characters', 'use_coords', 'use_order',
  'inject_default_positive_prompt', 'inject_default_negative_prompt',
] as const;

const NAI_FIELDS = new Set<string>([...IMG_PARAM_FIELDS.filter((key) => !['size', 'n', 'seed', 'output_format'].includes(key)),
  'params_version', 'extra_noise_seed', 'skip_cfg_above_sigma']);

export interface ImgParamsDraft {
  values: Record<string, unknown>;
  resolution: { mode: 'default' | 'pixels' | 'value'; width: string; height: string; value: string; original?: unknown; changed: boolean };
  extraText: string;
}

export function readImgParams(params?: Record<string, unknown> | null): ImgParamsDraft {
  const source = structuredClone(params ?? {});
  const values: Record<string, unknown> = {};
  const extra: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(source)) {
    if (key !== 'size' && IMG_PARAM_FIELDS.includes(key as typeof IMG_PARAM_FIELDS[number])) values[key] = value;
    else if (key !== 'size') extra[key] = value;
  }
  const size = source.size;
  const pixels = typeof size === 'string' ? size.match(/^(\d+)\s*[xX×]\s*(\d+)$/) : null;
  const pair = Array.isArray(size) ? size : pixels ? [pixels[1], pixels[2]] : null;
  return { values, extraText: Object.keys(extra).length ? JSON.stringify(extra, null, 2) : '', resolution: {
    mode: pair ? 'pixels' : size == null ? 'default' : 'value',
    width: pair ? String(pair[0] ?? '') : '', height: pair ? String(pair[1] ?? '') : '',
    value: typeof size === 'string' && !pixels ? size : '', original: size, changed: false,
  } };
}

export function incompatibleImgFields(values: Record<string, unknown>, apiType: ImgConfig['api_type']): string[] {
  return Object.keys(values).filter((key) => values[key] != null && (
    (apiType !== 'novelai' && NAI_FIELDS.has(key))
    || (apiType === 'openai_images' && key === 'seed')
    || (apiType === 'seedream' && key === 'output_format')
    || (apiType === 'seedream' && key === 'n' && values[key] !== 1)
  ));
}

export function writeImgParams(draft: ImgParamsDraft, apiType: ImgConfig['api_type']): Record<string, unknown> | null {
  let extra: Record<string, unknown> = {};
  if (draft.extraText.trim()) {
    let parsed: unknown;
    try { parsed = JSON.parse(draft.extraText); } catch { throw new Error('扩展参数不是有效 JSON'); }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('扩展参数必须是 JSON 对象');
    extra = parsed as Record<string, unknown>;
    const duplicates = Object.keys(extra).filter((key) => IMG_PARAM_FIELDS.includes(key as typeof IMG_PARAM_FIELDS[number]));
    if (duplicates.length) throw new Error(`请在表单中配置这些参数, 不要重复填写 JSON: ${duplicates.join(', ')}`);
  }
  const params = { ...structuredClone(draft.values), ...extra };
  const resolution = draft.resolution;
  if (!resolution.changed && resolution.original !== undefined) params.size = structuredClone(resolution.original);
  else if (resolution.mode === 'pixels') {
    const width = Number(resolution.width), height = Number(resolution.height);
    if (!resolution.width.trim() || !resolution.height.trim() || !Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
      throw new Error('图片宽度和高度必须是正整数');
    }
    params.size = [width, height];
  } else if (resolution.mode === 'value') {
    if (!resolution.value.trim()) throw new Error('请输入分辨率档位或自定义尺寸值');
    params.size = resolution.value.trim();
  }
  if (apiType === 'novelai' && typeof params.size === 'string' && !/^\d+x\d+$/.test(params.size)) {
    throw new Error('NovelAI 请使用宽高像素设置分辨率');
  }
  const incompatible = incompatibleImgFields(params, apiType);
  if (incompatible.length) throw new Error(`当前协议不支持这些默认参数, 请先清除或切回原协议: ${incompatible.join(', ')}`);
  const native = params.native;
  if (native && typeof native === 'object' && !Array.isArray(native)) {
    const raw = apiType === 'novelai' ? (native as Record<string, unknown>).parameters : native;
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
      const aliases: Record<string, string[]> = apiType === 'novelai'
        ? { size: ['width', 'height'], n: ['n_samples'], output_format: ['image_format'], characters: ['v4_prompt', 'v4_negative_prompt'] } : {};
      for (const key of IMG_PARAM_FIELDS) {
        if (params[key] != null && (aliases[key] ?? [key]).some((name) => Object.prototype.hasOwnProperty.call(raw, name))) {
          throw new Error(`${key} 同时存在于表单和 native, 请移除扩展 JSON 中的重复字段`);
        }
      }
    }
  }
  for (const key of ['n', 'seed', 'steps']) {
    const value = params[key];
    if (value != null && (typeof value !== 'number' || !Number.isInteger(value) || value < (key === 'seed' ? 0 : 1))) {
      throw new Error(`${key} 必须是${key === 'seed' ? '非负' : '正'}整数`);
    }
  }
  if (typeof params.n === 'number' && params.n > 32) throw new Error('生成数量不能超过 32');
  if (typeof params.seed === 'number' && params.seed >= 2 ** 32) throw new Error('随机种子不能超过 32 位无符号整数范围');
  for (const key of ['scale', 'cfg_rescale', 'strength', 'noise']) {
    if (params[key] != null && (typeof params[key] !== 'number' || !Number.isFinite(params[key]))) throw new Error(`${key} 必须是有限数值`);
  }
  for (const key of ['inject_default_positive_prompt', 'inject_default_negative_prompt']) {
    if (params[key] != null && typeof params[key] !== 'boolean') throw new Error(`${key} 必须是布尔值`);
  }
  if (apiType === 'novelai' && params.output_format === 'jpeg') throw new Error('NovelAI 输出格式请选择 PNG 或 WebP');
  if (Array.isArray(params.characters)) {
    for (const character of params.characters) {
      if (!character || typeof character !== 'object' || typeof character.prompt !== 'string' || !character.prompt.trim()) {
        throw new Error('请填写每个角色的提示词, 或移除空角色');
      }
      if (character.centers != null && (!Array.isArray(character.centers) || character.centers.some((center: unknown) => !Array.isArray(center)
        || center.length !== 2 || center.some((value) => typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1)))) {
        throw new Error('角色位置必须是 0 到 1 之间的横纵坐标');
      }
    }
  }
  return Object.keys(params).length ? params : null;
}

export function normalizeImgConfig(draft: ImgConfig, inputParams: string | Record<string, unknown> | null, isNew: boolean): ImgConfig {
  const name = draft.name.trim();
  const model = draft.model?.trim() || '';
  const baseUrl = draft.base_url?.trim() || '';
  const apiType = draft.api_type ?? 'openai_images';
  if (!name) throw new Error('请输入配置名称');
  if (!model) throw new Error('请输入模型 ID');
  if (!['openai_images', 'seedream', 'novelai'].includes(apiType)) throw new Error('请选择支持的请求协议');
  for (const address of [baseUrl, draft.edit_base_url?.trim()].filter(Boolean)) {
    let parsed: URL;
    try { parsed = new URL(address!); } catch { throw new Error('请填写完整的 HTTP 或 HTTPS 请求地址'); }
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.hash) {
      throw new Error('请求地址必须使用 HTTP 或 HTTPS, 且不能包含用户名、密码或片段');
    }
  }
  if (!baseUrl) throw new Error('请输入完整生成请求地址');
  const timeout = draft.timeout ?? 180;
  if (!Number.isFinite(timeout) || timeout <= 0) throw new Error('网络超时必须大于 0');
  let params: unknown = inputParams;
  if (typeof inputParams === 'string' && inputParams.trim()) {
    try { params = JSON.parse(inputParams); } catch { throw new Error('默认绘图参数不是有效 JSON'); }
    if (!params || typeof params !== 'object' || Array.isArray(params)) throw new Error('默认绘图参数必须是 JSON 对象');
  } else if (typeof inputParams === 'string') params = null;
  if (params !== null && (typeof params !== 'object' || Array.isArray(params))) throw new Error('默认绘图参数必须是 JSON 对象');
  const config: ImgConfig = { ...draft, name, model, base_url: baseUrl,
    edit_base_url: draft.edit_base_url?.trim() || null, api_type: apiType, timeout,
    default_params: structuredClone(params) as Record<string, unknown> | null };
  const key = draft.api_key?.trim();
  if (key && !key.includes('*')) config.api_key = key;
  else delete config.api_key;
  if (isNew && !config.api_key) throw new Error('请输入 API Key');
  return config;
}
