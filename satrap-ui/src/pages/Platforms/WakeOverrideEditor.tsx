import { Plus, Trash2 } from 'lucide-react';
import {
  GROUP_OVERRIDE_FIELDS,
  TIME_RULE_FIELDS,
  applyField,
  fieldState,
  fieldValue,
  newGroupRow,
  newTimeRow,
} from '@/utils/wakeOverrides';
import type { GroupOverrideRow, OverrideFieldDef, OverrideState, RowIssue, TimeRuleRow } from '@/utils/wakeOverrides';

// 受控编辑器: 行草稿由平台编辑表单持有, 组件不再保存本地副本, 也不用父 value 的每次变化覆盖行。
// 行以稳定 rowId 标识; 校验失败保留行并提示, 只在保存/试算处阻止提交。
type WakeOverrideEditorProps =
  | {
      kind: 'group';
      rows: GroupOverrideRow[];
      onChange: (rows: GroupOverrideRow[]) => void;
      issues: RowIssue[];
    }
  | {
      kind: 'time';
      rows: TimeRuleRow[];
      onChange: (rows: TimeRuleRow[]) => void;
      issues: RowIssue[];
    };

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
          step={def.integer ? 1 : 'any'}
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
    <div className="divide-y divide-glass-border">
      {fields.map((def) => (
        <FieldRow key={def.key} def={def} override={override} onApply={onApply} />
      ))}
    </div>
  );
}

function RowIssues({ issues }: { issues: RowIssue[] }) {
  if (issues.length === 0) return null;
  return (
    <p className="mt-1 text-xs text-error" data-testid="wake-override-row-error">
      {issues.map((issue) => issue.message).join('; ')}
    </p>
  );
}

function GroupEditor({ rows, onChange, issues }: Extract<WakeOverrideEditorProps, { kind: 'group' }>) {
  const patchRow = (rowId: string, patch: Partial<GroupOverrideRow>) => {
    onChange(rows.map((row) => (row.rowId === rowId ? { ...row, ...patch } : row)));
  };
  return (
    <div className="space-y-3" data-testid="wake-group-editor">
      {rows.map((row) => {
        const rowIssues = issues.filter((issue) => issue.rowId === row.rowId);
        const idInvalid = rowIssues.some((issue) => issue.field === 'id');
        return (
          <div key={row.rowId} className="rounded-sm bg-glass p-3" data-row-id={row.rowId}>
            <div className="mb-2 flex items-center gap-2">
              <input
                aria-label="群号"
                aria-invalid={idInvalid || undefined}
                className={`glass-input w-40 text-xs${idInvalid ? ' border-error' : ''}`}
                value={row.id}
                placeholder="群号 (正整数)"
                onChange={(event) => patchRow(row.rowId, { id: event.target.value })}
              />
              <button
                type="button"
                aria-label="删除群覆盖"
                className="ml-auto text-text-secondary hover:text-error"
                onClick={() => onChange(rows.filter((item) => item.rowId !== row.rowId))}
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
            <FieldList
              fields={GROUP_OVERRIDE_FIELDS}
              override={row.override}
              onApply={(next) => patchRow(row.rowId, { override: next })}
            />
            <RowIssues issues={rowIssues} />
          </div>
        );
      })}
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-accent hover:underline"
        onClick={() => onChange([...rows, newGroupRow()])}
      >
        <Plus className="h-3 w-3" /> 添加群覆盖
      </button>
      {rows.length > 0 && (
        <p className="text-xs text-text-secondary">
          群号为空/重复或格式非法的行会保留并提示, 需补全或删除后才能保存; 删除整行表示该群恢复继承。
        </p>
      )}
    </div>
  );
}

function TimeEditor({ rows, onChange, issues }: Extract<WakeOverrideEditorProps, { kind: 'time' }>) {
  const patchRow = (rowId: string, patch: Partial<TimeRuleRow>) => {
    onChange(rows.map((row) => (row.rowId === rowId ? { ...row, ...patch } : row)));
  };
  return (
    <div className="space-y-3" data-testid="wake-time-editor">
      {rows.map((row) => {
        const rowIssues = issues.filter((issue) => issue.rowId === row.rowId);
        const invalidFields = new Set(rowIssues.map((issue) => issue.field));
        return (
          <div key={row.rowId} className="rounded-sm bg-glass p-3" data-row-id={row.rowId}>
            <div className="mb-2 flex items-center gap-2">
              <input
                aria-label="开始时间"
                type="time"
                aria-invalid={invalidFields.has('start') || undefined}
                className={`glass-input w-28 text-xs${invalidFields.has('start') ? ' border-error' : ''}`}
                value={row.start}
                onChange={(event) => patchRow(row.rowId, { start: event.target.value })}
              />
              <span className="text-xs text-text-secondary">至</span>
              <input
                aria-label="结束时间"
                type="time"
                aria-invalid={invalidFields.has('end') || undefined}
                className={`glass-input w-28 text-xs${invalidFields.has('end') ? ' border-error' : ''}`}
                value={row.end}
                onChange={(event) => patchRow(row.rowId, { end: event.target.value })}
              />
              <button
                type="button"
                aria-label="删除时段规则"
                className="ml-auto text-text-secondary hover:text-error"
                onClick={() => onChange(rows.filter((item) => item.rowId !== row.rowId))}
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
            <FieldList
              fields={TIME_RULE_FIELDS}
              override={row.settings}
              onApply={(next) => patchRow(row.rowId, { settings: next })}
            />
            <RowIssues issues={rowIssues} />
          </div>
        );
      })}
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-accent hover:underline"
        onClick={() => onChange([...rows, newTimeRow()])}
      >
        <Plus className="h-3 w-3" /> 添加时段规则
      </button>
      {rows.length > 0 && (
        <p className="text-xs text-text-secondary">
          起止时间非法或相同的行会保留并提示, 需补全或删除后才能保存; 重叠时段以列表后项为准。
        </p>
      )}
    </div>
  );
}

export function WakeOverrideEditor(props: WakeOverrideEditorProps) {
  return props.kind === 'group'
    ? <GroupEditor {...props} />
    : <TimeEditor {...props} />;
}
