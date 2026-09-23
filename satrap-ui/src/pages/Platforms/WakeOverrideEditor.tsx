import { useEffect, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import {
  GROUP_OVERRIDE_FIELDS,
  TIME_RULE_FIELDS,
  applyField,
  fieldState,
  fieldValue,
  fromGroupRows,
  fromTimeRows,
  toGroupRows,
  toTimeRows,
} from '@/utils/wakeOverrides';
import type { GroupOverrideRow, OverrideFieldDef, OverrideState, TimeRuleRow } from '@/utils/wakeOverrides';

interface WakeOverrideEditorProps {
  kind: 'group' | 'time';
  value: unknown;
  onChange: (value: unknown) => void;
}

const STATE_OPTIONS: Array<{ value: OverrideState; label: string }> = [
  { value: 'inherit', label: '继承' },
  { value: 'off', label: '显式关闭' },
  { value: 'value', label: '显式值' },
];

function FieldRow({
  def,
  override,
  onApply,
}: {
  def: OverrideFieldDef;
  override: Record<string, unknown>;
  onApply: (next: Record<string, unknown>) => void;
}) {
  const state = fieldState(override, def);
  const value = fieldValue(override, def);
  const states = def.offValue === undefined ? STATE_OPTIONS.filter((item) => item.value !== 'off') : STATE_OPTIONS;
  return (
    <div className="flex items-center gap-2 py-1">
      <span className="w-32 shrink-0 text-xs text-text-secondary" title={def.key}>{def.label}</span>
      <select
        aria-label={`${def.label}状态`}
        className="glass-input w-28 shrink-0 text-xs"
        value={state}
        onChange={(event) => onApply(applyField(override, def, event.target.value as OverrideState))}
      >
        {states.map((item) => (
          <option key={item.value} value={item.value}>
            {item.value === 'off' && def.offLabel ? def.offLabel : item.label}
          </option>
        ))}
      </select>
      {state === 'value' && def.kind === 'number' && (
        <input
          aria-label={`${def.label}值`}
          type="number"
          step="any"
          min={def.min}
          max={def.max}
          className="glass-input w-24 text-xs"
          value={typeof value === 'number' ? value : ''}
          placeholder={def.placeholder}
          onChange={(event) => onApply(applyField(override, def, 'value', event.target.value === '' ? undefined : Number(event.target.value)))}
        />
      )}
      {state === 'value' && def.kind === 'select' && (
        <select
          aria-label={`${def.label}值`}
          className="glass-input w-40 text-xs"
          value={String(value)}
          onChange={(event) => onApply(applyField(override, def, 'value', event.target.value))}
        >
          {def.options?.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      )}
      {state === 'value' && def.kind === 'lines' && (
        <textarea
          aria-label={`${def.label}值`}
          rows={2}
          className="glass-input w-full font-mono text-xs resize-none"
          value={Array.isArray(value) ? value.join('\n') : ''}
          placeholder={def.placeholder}
          onChange={(event) => onApply(applyField(override, def, 'value', event.target.value.split('\n').map((line) => line.trim()).filter(Boolean)))}
        />
      )}
      {state === 'value' && def.kind === 'bool' && <span className="text-xs text-text-secondary">开启</span>}
      {state === 'off' && def.kind !== 'bool' && (
        <span className="text-xs text-text-secondary">{def.offLabel || '已关闭'}</span>
      )}
    </div>
  );
}

function FieldList({
  fields,
  override,
  onApply,
}: {
  fields: OverrideFieldDef[];
  override: Record<string, unknown>;
  onApply: (next: Record<string, unknown>) => void;
}) {
  return (
    <div className="divide-y divide-border/50">
      {fields.map((def) => (
        <FieldRow key={def.key} def={def} override={override} onApply={onApply} />
      ))}
    </div>
  );
}

function GroupEditor({ value, onChange }: { value: unknown; onChange: (value: unknown) => void }) {
  const [rows, setRows] = useState<GroupOverrideRow[]>(() => toGroupRows(value));
  const valueKey = JSON.stringify(value ?? {});
  useEffect(() => {
    setRows(toGroupRows(JSON.parse(valueKey) as unknown));
  }, [valueKey]);
  const update = (next: GroupOverrideRow[]) => {
    setRows(next);
    onChange(fromGroupRows(next));
  };
  return (
    <div className="space-y-3" data-testid="wake-group-editor">
      {rows.map((row, index) => (
        <div key={index} className="rounded-sm bg-glass p-3">
          <div className="mb-2 flex items-center gap-2">
            <input
              aria-label="群号"
              className="glass-input w-40 text-xs"
              value={row.id}
              placeholder="群号 (正整数)"
              onChange={(event) => update(rows.map((item, i) => (i === index ? { ...item, id: event.target.value } : item)))}
            />
            <button
              type="button"
              aria-label="删除群覆盖"
              className="ml-auto text-text-secondary hover:text-error"
              onClick={() => update(rows.filter((_, i) => i !== index))}
            >
              <Trash2 className="h-4 w-4" />
            </button>
          </div>
          <FieldList
            fields={GROUP_OVERRIDE_FIELDS}
            override={row.override}
            onApply={(next) => update(rows.map((item, i) => (i === index ? { ...item, override: next } : item)))}
          />
        </div>
      ))}
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-accent hover:underline"
        onClick={() => update([...rows, { id: '', override: {} }])}
      >
        <Plus className="h-3 w-3" /> 添加群覆盖
      </button>
      {rows.length > 0 && (
        <p className="text-xs text-text-secondary">群号留空/非法或全部字段继承的行在保存时忽略</p>
      )}
    </div>
  );
}

function TimeEditor({ value, onChange }: { value: unknown; onChange: (value: unknown) => void }) {
  const [rows, setRows] = useState<TimeRuleRow[]>(() => toTimeRows(value));
  const valueKey = JSON.stringify(value ?? []);
  useEffect(() => {
    setRows(toTimeRows(JSON.parse(valueKey) as unknown));
  }, [valueKey]);
  const update = (next: TimeRuleRow[]) => {
    setRows(next);
    onChange(fromTimeRows(next));
  };
  return (
    <div className="space-y-3" data-testid="wake-time-editor">
      {rows.map((row, index) => (
        <div key={index} className="rounded-sm bg-glass p-3">
          <div className="mb-2 flex items-center gap-2">
            <input
              aria-label="开始时间"
              type="time"
              className="glass-input w-28 text-xs"
              value={row.start}
              onChange={(event) => update(rows.map((item, i) => (i === index ? { ...item, start: event.target.value } : item)))}
            />
            <span className="text-xs text-text-secondary">至</span>
            <input
              aria-label="结束时间"
              type="time"
              className="glass-input w-28 text-xs"
              value={row.end}
              onChange={(event) => update(rows.map((item, i) => (i === index ? { ...item, end: event.target.value } : item)))}
            />
            <button
              type="button"
              aria-label="删除时段规则"
              className="ml-auto text-text-secondary hover:text-error"
              onClick={() => update(rows.filter((_, i) => i !== index))}
            >
              <Trash2 className="h-4 w-4" />
            </button>
          </div>
          <FieldList
            fields={TIME_RULE_FIELDS}
            override={row.settings}
            onApply={(next) => update(rows.map((item, i) => (i === index ? { ...item, settings: next } : item)))}
          />
        </div>
      ))}
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-accent hover:underline"
        onClick={() => update([...rows, { start: '23:00', end: '07:00', settings: {} }])}
      >
        <Plus className="h-3 w-3" /> 添加时段规则
      </button>
      {rows.length > 0 && (
        <p className="text-xs text-text-secondary">起止相同或全部字段继承的行在保存时忽略; 重叠时段以列表后项为准</p>
      )}
    </div>
  );
}

export function WakeOverrideEditor({ kind, value, onChange }: WakeOverrideEditorProps) {
  return kind === 'group'
    ? <GroupEditor value={value} onChange={onChange} />
    : <TimeEditor value={value} onChange={onChange} />;
}
