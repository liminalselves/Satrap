import type { EdictumPluginConfigField } from '@/api/types';
import { useId } from 'react';

export interface ConfigOption { value: string; label: string; scope?: string }
export type ModelOptions = Record<string, ConfigOption[]>;

interface Props {
  schema: Record<string, EdictumPluginConfigField>;
  values: Record<string, unknown>;
  inherited?: Record<string, unknown>;
  sources?: Record<string, string>;
  modelOptions?: ModelOptions;
  knowledgeBases?: ConfigOption[];
  sessionMode?: boolean;
  namedMode?: boolean;
  disabled?: boolean;
  onChange: (key: string, value: unknown) => void;
  onReset?: (key: string) => void;
}

export function PluginConfigFields({ schema, values, inherited = {}, sources = {}, modelOptions = {}, knowledgeBases = [], sessionMode = false, namedMode = false, disabled = false, onChange, onReset }: Props) {
  const prefix = useId();
  return <div className="space-y-4">{Object.entries(schema).map(([key, field]) => {
    const fieldId = `${prefix}-${key}`;
    const overridden = Object.prototype.hasOwnProperty.call(values, key);
    const value = overridden ? values[key] : Object.prototype.hasOwnProperty.call(inherited, key) ? inherited[key] : field.default;
    const nullSelected = Boolean(field.nullable && value === null);
    const locked = disabled || (sessionMode && field.session_overridable === false);
    const modelField = ['llm', 'embed', 'rerank', 'asr'].includes(field.type);
    const knowledgeField = ['knowledge_base', 'knowledge_bases'].includes(field.type);
    const options = modelField ? modelOptions[field.type] || [] : knowledgeField
      ? knowledgeBases.filter((item) => !field.scope || item.scope === field.scope)
      : (field.options || []).map((item) => ({ value: item, label: item }));
    const selections = Array.isArray(value) ? value.map(String) : value ? [String(value)] : [];
    const missing = selections.filter((item) => !options.some((option) => option.value === item));
    return <div key={key}>
      <div className="flex items-center justify-between gap-3">
        <label htmlFor={fieldId} className="text-sm font-medium text-text-primary">{key}{field.required ? ' *' : ''}</label>
        {(sessionMode || namedMode) && <div className="flex items-center gap-2 text-xs text-text-tertiary">
          <span>{overridden ? namedMode ? '命名配置' : '会话自定义' : `继承${sources[key] === 'named' ? '命名配置' : sources[key] === 'global' ? '全局配置' : '默认值'}`}</span>
          {overridden && onReset && <button type="button" disabled={locked} className="text-accent" onClick={() => onReset(key)}>恢复继承</button>}
        </div>}
      </div>
      {field.description && <p className="my-1 text-xs text-text-tertiary">{field.description}</p>}
      {field.nullable && <label className="mb-1 flex items-center gap-2 text-xs text-text-secondary">
        <input aria-label={`${key} 使用空值`} type="checkbox" disabled={locked} checked={value === null} onChange={(event) => onChange(key, event.target.checked ? null : field.default ?? (field.type === 'number' ? field.minimum ?? 0 : field.type === 'knowledge_bases' ? [] : field.type === 'bool' ? false : ''))} />使用空值
      </label>}
      {field.type === 'bool' ? <input id={fieldId} type="checkbox" disabled={locked || nullSelected} checked={Boolean(value)} onChange={(event) => onChange(key, event.target.checked)} />
        : field.type === 'knowledge_bases' ? <div className="space-y-1 rounded border border-glass-border p-2">
          {options.length === 0 && <p className="text-xs text-text-tertiary">没有可选择的知识库</p>}
          {[...options, ...missing.map((item) => ({ value: item, label: `${item} (引用失效)` }))].map((item) => <label key={item.value} className="flex items-center gap-2 text-sm">
            <input type="checkbox" disabled={locked || nullSelected} checked={selections.includes(item.value)} onChange={(event) => onChange(key, event.target.checked ? [...selections, item.value] : selections.filter((selection) => selection !== item.value))} />{item.label}
          </label>)}
        </div>
          : modelField || knowledgeField || field.type === 'select' ? <select id={fieldId} className="glass-input w-full text-sm" disabled={locked || nullSelected} value={String(value ?? '')} onChange={(event) => onChange(key, event.target.value)}>
            <option value="">{field.required ? '请选择' : '不使用 / 未选择'}</option>
            {missing.map((item) => <option key={item} value={item}>{item} (引用失效)</option>)}
            {options.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
            : field.type === 'textarea' ? <textarea id={fieldId} disabled={locked || nullSelected} className="glass-input w-full text-sm" rows={4} value={String(value ?? '')} onChange={(event) => onChange(key, event.target.value)} />
              : <input id={fieldId} disabled={locked || nullSelected} className="glass-input w-full text-sm" type={field.type === 'number' ? 'number' : 'text'} min={field.minimum ?? undefined} max={field.maximum ?? undefined} step={field.integer ? 1 : 'any'} value={String(value ?? '')} onChange={(event) => onChange(key, field.type === 'number' ? event.target.value === '' ? '' : Number(event.target.value) : event.target.value)} />}
      {sessionMode && field.session_overridable === false && <p className="mt-1 text-xs text-text-tertiary">此参数不允许会话覆盖</p>}
    </div>;
  })}</div>;
}
