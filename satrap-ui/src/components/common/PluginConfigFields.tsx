import type { EdictumPluginConfigField } from '@/api/types';
import { useId } from 'react';
import { AlertCircle, Link2Off, Lock, RotateCcw } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Input, Textarea } from '@/components/kit';
import { Select as KitSelect } from '@/components/kit/Select';

export interface ConfigOption { value: string; label: string; scope?: string }
export type ModelOptions = Record<string, ConfigOption[]>;

interface Props {
  schema: Record<string, EdictumPluginConfigField>;
  values: Record<string, unknown>;
  inherited?: Record<string, unknown>;
  sources?: Record<string, string>;
  modelOptions?: ModelOptions;
  knowledgeBases?: ConfigOption[];
  modelState?: 'loading' | 'error' | 'ready';
  knowledgeState?: 'loading' | 'error' | 'ready';
  sessionMode?: boolean;
  namedMode?: boolean;
  disabled?: boolean;
  onChange: (key: string, value: unknown) => void;
  onReset?: (key: string) => void;
}

const MODEL_TYPES = ['llm', 'embed', 'rerank', 'asr'];
const KNOWLEDGE_TYPES = ['knowledge_base', 'knowledge_bases'];

function inheritedLabel(source?: string) {
  if (source === 'named') return '继承命名配置';
  if (source === 'global') return '继承全局配置';
  return '继承默认值';
}

export function PluginConfigFields({ schema, values, inherited = {}, sources = {}, modelOptions = {}, knowledgeBases = [], modelState = 'ready', knowledgeState = 'ready', sessionMode = false, namedMode = false, disabled = false, onChange, onReset }: Props) {
  const prefix = useId();
  const fields = Object.entries(schema);
  if (!fields.length) {
    return <p className="rounded-xl border border-dashed border-line px-4 py-10 text-center text-[13px] text-fg-3">此插件没有可配置的参数</p>;
  }
  return (
    <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2">
      {fields.map(([key, field]) => {
        const fieldId = `${prefix}-${key}`;
        const fieldLabel = field.label || key;
        const overridden = Object.prototype.hasOwnProperty.call(values, key);
        const value = overridden ? values[key] : Object.prototype.hasOwnProperty.call(inherited, key) ? inherited[key] : field.default;
        const nullSelected = Boolean(field.nullable && value === null);
        const locked = disabled || (sessionMode && field.session_overridable === false);
        const modelField = MODEL_TYPES.includes(field.type);
        const knowledgeField = KNOWLEDGE_TYPES.includes(field.type);
        const optionState = modelField ? modelState : knowledgeField ? knowledgeState : 'ready';
        const optionLocked = locked || optionState !== 'ready';
        const stateLabel = optionState === 'loading' ? '正在读取选项…' : optionState === 'error' ? '选项读取失败' : '引用失效';
        const options: ConfigOption[] = modelField ? modelOptions[field.type] || [] : knowledgeField
          ? knowledgeBases.filter((item) => !field.scope || item.scope === field.scope)
          : (field.options || []).map((item) => ({ value: item, label: field.option_labels?.[item] || item }));
        const selections = Array.isArray(value) ? value.map(String) : value ? [String(value)] : [];
        const missing = selections.filter((item) => !options.some((option) => option.value === item));
        const wide = ['textarea', 'knowledge_bases'].includes(field.type) || field.type === 'text';
        const fallbackValue = () => field.type === 'number'
          ? (typeof field.default === 'number' ? field.default : field.minimum ?? 0)
          : field.type === 'knowledge_bases' ? [] : field.type === 'bool' ? false : field.default ?? '';

        const labelRow = (
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <label htmlFor={fieldId} className="flex flex-wrap items-center gap-1.5 text-[13px] font-medium text-fg-2">
                {fieldLabel}
                {field.required && <span className="text-red" aria-hidden>*</span>}
                {sessionMode && field.session_overridable === false && (
                  <span className="chip !h-[18px] !gap-1 !px-1.5 !text-[10.5px]"><Lock className="h-2.5 w-2.5" />不可覆盖</span>
                )}
              </label>
              {field.description && <p className="mt-0.5 text-[11.5px] leading-4 text-fg-3">{field.description}</p>}
            </div>
            <div className="flex flex-none flex-col items-end gap-1">
              {(sessionMode || namedMode) && (
                <span className={cn('whitespace-nowrap text-[11px]', overridden ? 'font-medium text-teal' : 'text-fg-4')}>
                  {overridden ? (namedMode ? '命名配置' : '会话自定义') : inheritedLabel(sources[key])}
                </span>
              )}
              {overridden && onReset && (
                <button type="button" disabled={locked} onClick={() => onReset(key)}
                  className="inline-flex items-center gap-1 text-[11px] font-medium text-teal transition hover:underline disabled:opacity-40">
                  <RotateCcw className="h-3 w-3" />恢复继承
                </button>
              )}
            </div>
          </div>
        );

        const nullableRow = field.nullable ? (
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-[11.5px] text-fg-3">
            <input
              type="checkbox"
              className="h-3.5 w-3.5 accent-[rgb(var(--rgb-teal))]"
              checked={value === null}
              disabled={locked}
              aria-label={`${fieldLabel} 使用空值`}
              onChange={(event) => onChange(key, event.target.checked ? null : fallbackValue())}
            />
            使用空值
          </label>
        ) : null;

        if (field.type === 'bool') {
          return (
            <div key={key} className="rounded-xl border border-line bg-glass px-3.5 py-3">
              <div className="flex items-center justify-between gap-4">
                {labelRow}
                <input
                  id={fieldId}
                  type="checkbox"
                  aria-label={fieldLabel}
                  className="h-4 w-4 flex-none accent-[rgb(var(--rgb-teal))]"
                  checked={Boolean(value)}
                  disabled={locked || nullSelected}
                  onChange={(event) => onChange(key, event.target.checked)}
                />
              </div>
              {nullableRow && <div className="mt-1.5">{nullableRow}</div>}
            </div>
          );
        }

        return (
          <div key={key} className={cn('space-y-1.5', wide && 'sm:col-span-2')}>
            {labelRow}
            {nullableRow}
            {field.type === 'knowledge_bases' ? (
              <div className={cn('space-y-1 rounded-xl border border-line bg-glass p-2.5', nullSelected && 'opacity-50')}>
                {!options.length && (
                  <p className="px-1 py-2 text-[12.5px] text-fg-3">
                    {optionState === 'loading' ? '正在读取知识库选项…' : optionState === 'error' ? '知识库选项读取失败' : '没有可选择的知识库'}
                  </p>
                )}
                {[...options, ...missing.map((item) => ({ value: item, label: `${item} (${stateLabel})` }))].map((option) => (
                  <label key={option.value} className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-[13px] text-fg-2 transition hover:bg-glass-hover">
                    <input
                      type="checkbox"
                      className="h-3.5 w-3.5 accent-[rgb(var(--rgb-teal))]"
                      disabled={optionLocked || nullSelected}
                      checked={selections.includes(option.value)}
                      onChange={(event) => onChange(key, event.target.checked ? [...selections, option.value] : selections.filter((selection) => selection !== option.value))}
                    />
                    <span className={cn('min-w-0 truncate', missing.includes(option.value) && 'text-orange')}>{option.label}</span>
                    {missing.includes(option.value) && <Link2Off className="ml-auto h-3 w-3 flex-none text-orange" />}
                  </label>
                ))}
              </div>
            ) : modelField || knowledgeField || field.type === 'select' ? (
              <>
                <KitSelect
                  id={fieldId}
                  aria-label={fieldLabel}
                  className={cn('field h-9 w-full', optionLocked && 'pointer-events-none opacity-55')}
                  disabled={optionLocked || nullSelected}
                  value={String(value ?? '')}
                  onChange={(value) => onChange(key, value)}
                >
                  <option value="">{optionState === 'loading' ? '正在读取选项…' : optionState === 'error' ? '选项读取失败' : field.required ? '请选择' : '不使用 / 未选择'}</option>
                  {missing.map((item) => <option key={item} value={item}>{item} ({stateLabel})</option>)}
                  {options.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                </KitSelect>
                {optionState === 'loading' && <p className="text-[11.5px] text-fg-3">{stateLabel}</p>}
                {optionState === 'error' && <p role="alert" className="flex items-center gap-1 text-[11.5px] text-red"><AlertCircle className="h-3 w-3" />{stateLabel}</p>}
                {missing.length > 0 && optionState === 'ready' && <p className="flex items-center gap-1 text-[11.5px] text-orange"><Link2Off className="h-3 w-3" />{missing.join('、')} 引用已失效，请重新选择</p>}
              </>
            ) : field.type === 'textarea' ? (
              <Textarea
                id={fieldId}
                aria-label={fieldLabel}
                rows={4}
                className="font-mono text-[12.5px]"
                disabled={locked || nullSelected}
                value={String(value ?? '')}
                onChange={(event) => onChange(key, event.target.value)}
              />
            ) : (
              <Input
                id={fieldId}
                aria-label={fieldLabel}
                type={field.type === 'number' ? 'number' : 'text'}
                min={field.minimum ?? undefined}
                max={field.maximum ?? undefined}
                step={field.integer ? 1 : 'any'}
                disabled={locked || nullSelected}
                value={String(value ?? '')}
                onChange={(event) => {
                  if (field.type === 'number') onChange(key, event.target.value === '' ? '' : Number(event.target.value));
                  else onChange(key, event.target.value);
                }}
              />
            )}
            {sessionMode && field.session_overridable === false && <p className="text-[11px] text-fg-4">此参数不允许会话覆盖</p>}
            {field.type === 'number' && (field.minimum !== undefined || field.maximum !== undefined) && (
              <p className="num text-[11px] text-fg-4">范围 {field.minimum ?? '—'} – {field.maximum ?? '—'}{field.integer ? ' · 整数' : ''}</p>
            )}
          </div>
        );
      })}
    </div>
  );
}
