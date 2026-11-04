import { useId, type ReactNode } from 'react';
import { Select } from '@/components/kit/Select';
import type { ImgConfig } from '@/api/types';
import { incompatibleImgFields, readImgParams, type ImgParamsDraft } from './imgModel';

interface Props {
  apiType: ImgConfig['api_type'];
  value: ImgParamsDraft;
  onChange: (value: ImgParamsDraft) => void;
}

export function ImgParamsFields({ apiType, value, onChange }: Props) {
  const id = useId();
  const nai = apiType === 'novelai';
  const values = value.values;
  const resolution = value.resolution;
  const set = (key: string, next: unknown) => {
    const updated = { ...values };
    if (next === undefined) delete updated[key];
    else updated[key] = next;
    onChange({ ...value, values: updated });
  };
  const resize = (patch: Partial<ImgParamsDraft['resolution']>) => onChange({ ...value,
    resolution: { ...resolution, ...patch, changed: true } });
  const field = (key: string, label: string, control: ReactNode) => <div key={key} className="space-y-1">
    <label className="block text-sm text-text-secondary" htmlFor={`${id}-${key}`}>{label}</label>{control}
  </div>;
  const number = (key: string, label: string, options: { min?: number; max?: number; step?: number | 'any'; placeholder?: string } = {}) =>
    field(key, label, <input id={`${id}-${key}`} type="number" className="glass-input w-full" step="any" {...options}
      value={typeof values[key] === 'number' ? values[key] as number : ''}
      onChange={(event) => set(key, event.target.value === '' ? undefined : Number(event.target.value))} />);
  const text = (key: string, label: string, suggestions: string[] = []) => field(key, label,
    <Select id={`${id}-${key}`} className="field w-full" editable placeholder="使用默认值"
      value={typeof values[key] === 'string' ? values[key] as string : ''} onChange={(next) => set(key, next || undefined)}
      options={[{ value: '', label: '使用默认值' }, ...suggestions.map((item) => ({ value: item, label: item }))]} />);
  const triState = (key: string, label: string) => field(key, label,
    <Select id={`${id}-${key}`} className="field w-full" value={typeof values[key] === 'boolean' ? String(values[key]) : ''}
      onChange={(next) => set(key, next === '' ? undefined : next === 'true')} options={[
        { value: '', label: '使用默认值' }, { value: 'true', label: '开启' }, { value: 'false', label: '关闭' },
      ]} />);
  const incompatible = incompatibleImgFields(values, apiType);
  const characters = Array.isArray(values.characters) ? values.characters as Array<Record<string, unknown>> : [];
  const updateCharacter = (index: number, patch: Record<string, unknown>) => set('characters',
    characters.map((character, current) => current === index ? { ...character, ...patch } : character));

  return <div className="space-y-5">
    <div className="flex items-center justify-between gap-3">
      <p className="text-xs text-text-tertiary">留空使用模型默认值，可用尺寸与参数取决于所选模型。</p>
      <button type="button" className="shrink-0 text-xs text-teal hover:underline" onClick={() => onChange(readImgParams())}>清空绘图默认值</button>
    </div>
    {incompatible.length > 0 && <div className="rounded-lg border border-line p-3 text-sm">
      <p>已有参数与当前协议不兼容: {incompatible.join(', ')}。切回原协议仍会保留。</p>
      <button type="button" className="mt-2 text-teal hover:underline" onClick={() => {
        const next = { ...values };
        for (const key of incompatible) delete next[key];
        onChange({ ...value, values: next });
      }}>清除不兼容参数</button>
    </div>}
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {field('resolution', '分辨率设置', <Select id={`${id}-resolution`} className="field w-full" value={resolution.mode}
        onChange={(mode) => resize({ mode: mode as typeof resolution.mode })} options={[
          { value: 'default', label: '使用模型默认值' }, { value: 'pixels', label: '指定宽高像素' },
          { value: 'value', label: '分辨率档位 / 自定义值', disabled: nai },
        ]} />)}
      {resolution.mode === 'pixels' && field('preset', '常用尺寸', <Select id={`${id}-preset`} className="field w-full"
        value={['1024x1024', '1024x1536', '1536x1024', '832x1216', '1216x832', '512x768'].includes(`${resolution.width}x${resolution.height}`)
          ? `${resolution.width}x${resolution.height}` : ''}
        onChange={(next) => { if (next) { const [width, height] = next.split('x'); resize({ width, height }); } }} options={[
          { value: '', label: '自定义尺寸' }, { value: '1024x1024', label: '正方形 · 1024 × 1024' },
          { value: '1024x1536', label: '竖图 · 1024 × 1536' }, { value: '1536x1024', label: '横图 · 1536 × 1024' },
          { value: '832x1216', label: '竖图 · 832 × 1216' }, { value: '1216x832', label: '横图 · 1216 × 832' },
          { value: '512x768', label: '竖图 · 512 × 768' },
        ]} />)}
      {resolution.mode === 'pixels' && <>
        {field('width', '图片宽度 (像素)', <input id={`${id}-width`} className="glass-input w-full" type="number" min={1} step={1}
          value={resolution.width} onChange={(event) => resize({ width: event.target.value })} />)}
        {field('height', '图片高度 (像素)', <input id={`${id}-height`} className="glass-input w-full" type="number" min={1} step={1}
          value={resolution.height} onChange={(event) => resize({ height: event.target.value })} />)}
      </>}
      {resolution.mode === 'value' && field('size-value', '分辨率档位或尺寸值',
        <Select id={`${id}-size-value`} className="field w-full" editable value={resolution.value}
          placeholder={apiType === 'seedream' ? '如 2K, 取决于模型' : '如 auto, 取决于模型'}
          onChange={(next) => resize({ value: next })} options={(apiType === 'seedream' ? ['1K', '2K', '4K'] : ['auto'])
            .map((item) => ({ value: item, label: item }))} />)}
      {apiType !== 'seedream' && number('n', '生成数量', { min: 1, max: 32, step: 1, placeholder: '默认 1 张' })}
      {apiType !== 'openai_images' && field('seed-mode', '随机种子模式', <Select id={`${id}-seed-mode`} className="field w-full"
        value={values.seed == null ? 'random' : 'fixed'} onChange={(next) => set('seed', next === 'random' ? undefined : values.seed ?? 0)}
        options={[{ value: 'random', label: '随机 / 不指定' }, { value: 'fixed', label: '固定种子' }]} />)}
      {apiType !== 'openai_images' && values.seed != null && number('seed', '固定随机种子', { min: 0, max: 4294967295, step: 1 })}
      {apiType !== 'seedream' && field('output_format', '输出格式', <Select id={`${id}-output_format`} className="field w-full"
        value={typeof values.output_format === 'string' ? values.output_format : ''} onChange={(next) => set('output_format', next || undefined)}
        options={[{ value: '', label: '使用默认格式' }, { value: 'png', label: 'PNG' }, { value: 'webp', label: 'WebP' },
          { value: 'jpeg', label: nai ? 'JPEG (当前协议不支持)' : 'JPEG', disabled: nai }]} />)}
      {nai && number('steps', '采样步数', { min: 1, step: 1, placeholder: '使用默认值' })}
      {nai && number('scale', '提示词引导强度', { placeholder: '使用默认值' })}
      {nai && text('sampler', '采样器', ['k_euler_ancestral', 'k_euler', 'k_dpmpp_2m', 'k_dpmpp_sde'])}
    </div>
    {nai && field('negative_prompt', '负向提示词', <textarea id={`${id}-negative_prompt`} className="glass-input w-full" rows={3}
      value={typeof values.negative_prompt === 'string' ? values.negative_prompt : ''}
      onChange={(event) => set('negative_prompt', event.target.value || undefined)} />)}
    {nai && <div className="space-y-3 rounded-xl border border-line p-3">
      {([
        ['inject_default_positive_prompt', '在提示词末尾注入默认正向提示词', '将内置质量标签追加到主提示词末尾'],
        ['inject_default_negative_prompt', '在负向提示词开头注入默认负向提示词', '将内置低质量与水印等排除词放在自定义负向词前'],
      ] as const).map(([key, label, hint]) => <div key={key}>
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <input type="checkbox" className="h-4 w-4 accent-teal" checked={values[key] === true}
            onChange={(event) => set(key, event.target.checked)} />
          <span>{label}</span>
        </label>
        <p className="ml-6 mt-1 text-xs text-text-tertiary">{hint}，默认关闭。</p>
      </div>)}
    </div>}
    {nai && <details className="rounded-xl border border-line p-3">
      <summary className="cursor-pointer text-sm font-medium">NAI 高级参数</summary>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
        {number('cfg_rescale', 'CFG 重缩放')}
        {text('noise_schedule', '噪声调度', ['native', 'karras', 'exponential', 'polyexponential'])}
        {triState('sm', 'SMEA')}{triState('sm_dyn', '动态 SMEA')}
        {triState('dynamic_thresholding', '动态阈值')}
        {triState('use_coords', '启用角色位置')}{triState('use_order', '按角色顺序生成')}
      </div>
      <h4 className="mb-2 mt-4 text-sm font-medium">编辑默认参数</h4>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {number('strength', '图生图修改强度')}{number('noise', '图生图噪声')}
        {triState('color_correct', '颜色校正')}{triState('add_original_image', '附带原图')}
      </div>
    </details>}
    {nai && <details className="rounded-xl border border-line p-3">
      <summary className="cursor-pointer text-sm font-medium">多角色提示词 ({characters.length})</summary>
      <div className="mt-3 space-y-3">
        {characters.map((character, index) => {
          const centers = Array.isArray(character.centers) ? character.centers as number[][] : [];
          return <fieldset key={index} className="space-y-2 rounded-lg border border-line p-3">
            <legend className="text-sm font-medium">角色 {index + 1}</legend>
            <div className="flex justify-end">
              <button type="button" className="text-xs text-red hover:underline" aria-label={`移除角色 ${index + 1}`}
                onClick={() => set('characters', characters.filter((_, current) => current !== index))}>移除角色</button></div>
            <label className="block space-y-1 text-sm"><span>角色 {index + 1} 提示词</span>
              <textarea aria-label={`角色 ${index + 1} 提示词`} className="glass-input w-full" rows={2} value={String(character.prompt ?? '')}
                onChange={(event) => updateCharacter(index, { prompt: event.target.value })} /></label>
            <label className="block space-y-1 text-sm"><span>角色 {index + 1} 负向提示词</span>
              <textarea aria-label={`角色 ${index + 1} 负向提示词`} className="glass-input w-full" rows={2} value={String(character.negative_prompt ?? '')}
                onChange={(event) => updateCharacter(index, { negative_prompt: event.target.value })} /></label>
            {centers.map((center, position) => <div key={position} className="flex items-end gap-2">
              {[0, 1].map((axis) => <label key={axis} className="min-w-0 flex-1 space-y-1 text-xs">
                <span>角色 {index + 1} 位置 {position + 1} {axis === 0 ? 'X' : 'Y'}</span>
                <input type="number" min={0} max={1} step="any" className="glass-input w-full" value={center[axis] ?? ''}
                  onChange={(event) => updateCharacter(index, { centers: centers.map((pair, current) => current !== position ? pair
                    : pair.map((coordinate, currentAxis) => currentAxis !== axis ? coordinate : event.target.value === '' ? null : Number(event.target.value))) })} />
              </label>)}
              <button type="button" className="py-2 text-xs text-red" aria-label={`移除角色 ${index + 1} 位置 ${position + 1}`}
                onClick={() => updateCharacter(index, { centers: centers.filter((_, current) => current !== position) })}>移除</button>
            </div>)}
            <button type="button" className="text-xs text-teal hover:underline" onClick={() => onChange({ ...value,
              values: { ...values, use_coords: true, characters: characters.map((item, current) => current !== index ? item
                : { ...item, centers: [...centers, [0.5, 0.5]] }) } })}>添加角色 {index + 1} 位置</button>
          </fieldset>;
        })}
        <button type="button" className="text-sm text-teal hover:underline" onClick={() => set('characters', [...characters, { prompt: '', negative_prompt: '' }])}>添加角色</button>
      </div>
    </details>}
    <details className="rounded-xl border border-line p-3">
      <summary className="cursor-pointer text-sm font-medium">扩展参数 (JSON)</summary>
      <p className="mb-2 mt-3 text-xs text-text-tertiary">仅填写未提供控件的参数。原生字段放在 native 中；已有扩展字段会保留。</p>
      <textarea aria-label="扩展参数 (JSON)" className="glass-input w-full font-mono text-sm" rows={6}
        placeholder={'{\n  "native": {}\n}'} value={value.extraText} onChange={(event) => onChange({ ...value, extraText: event.target.value })} />
    </details>
  </div>;
}
