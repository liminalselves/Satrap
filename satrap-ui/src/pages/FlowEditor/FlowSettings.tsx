import { useState } from 'react';
import { Settings2 } from 'lucide-react';
import type { FlowBudget } from '@/api/types';
import { Button, Field, Input, Modal, Select, Switch, Tabs } from '@/components/kit';
import type { FlowDefaults, TypeIndex } from './flowModel';

export function FlowSettings({ open, onClose, types, defaults, budget, onDefaults, onBudget }: {
  open: boolean; onClose: () => void; types: TypeIndex; defaults: FlowDefaults; budget: FlowBudget | null | undefined;
  onDefaults: (next: FlowDefaults) => void; onBudget: (next: FlowBudget | null) => void;
}) {
  const [tab, setTab] = useState<'defaults' | 'budget'>('defaults');
  const entries = Object.entries(defaults).flatMap(([type, values]) => Object.entries(values).map(([key, value]) => ({ type, key, value })));
  const remove = (type: string, key: string) => {
    const next = { ...defaults, [type]: { ...defaults[type] } };
    delete next[type][key];
    if (!Object.keys(next[type]).length) delete next[type];
    onDefaults(next);
  };
  const patchBudget = (patch: Partial<FlowBudget>) => onBudget({ max_calls: 60, window_minutes: 60, count: 'all', ...(budget ?? {}), ...patch });
  return (
    <Modal open={open} onClose={onClose} title="流程设置" description="流程默认作用于本流程的同类节点; 预算限制整个流程的模型调用次数" icon={<Settings2 className="h-4 w-4" />} size="lg">
      <Tabs label="设置分组" size="sm" value={tab} onChange={setTab} items={[{ value: 'defaults', label: '流程默认', count: entries.length }, { value: 'budget', label: '预算' }]} />
      <div className="mt-4 min-h-[200px]">
        {tab === 'defaults' && (
          <div className="space-y-2">
            <p className="text-[12px] text-fg-3">合并顺序: 类型默认 &lt; 插件全局默认 &lt; 流程默认 &lt; 本节点。按字段继承, 在节点检查器里点"设为流程默认"添加。</p>
            {entries.length === 0 && <p className="py-4 text-center text-[13px] text-fg-3">尚未设置流程默认</p>}
            {entries.map(({ type, key, value }) => {
              const def = types.get(type);
              return (
                <div key={`${type}.${key}`} className="flex items-center gap-3 rounded-lg border border-line px-3 py-2 text-[13px]">
                  <span className="w-36 flex-none truncate text-fg-3">{def?.label ?? type}</span>
                  <span className="min-w-0 flex-1 truncate text-fg">{def?.schema[key]?.label || key} <span className="font-mono text-[10px] text-fg-4">{key}</span></span>
                  <span className="max-w-[200px] truncate font-mono text-[12px] text-accent">{JSON.stringify(value)}</span>
                  <Button size="sm" variant="ghost" onClick={() => remove(type, key)}>移除</Button>
                </div>
              );
            })}
          </div>
        )}
        {tab === 'budget' && (
          <div className="space-y-3">
            <div className="flex items-center justify-between rounded-lg border border-line px-3 py-2.5">
              <div>
                <div className="text-[13px] text-fg">启用硬预算</div>
                <div className="text-[12px] text-fg-3">窗口内调用达到上限后, 触发被拦截并记录 blocked 事件</div>
              </div>
              <Switch size="sm" label="启用硬预算" checked={Boolean(budget)} onChange={(on) => (on ? patchBudget({}) : onBudget(null))} />
            </div>
            {budget && (
              <div className="grid gap-3 sm:grid-cols-3">
                <Field label="调用上限" hint="窗口内允许的模型调用总数">
                  <Input type="number" min={1} step={1} aria-label="调用上限" value={String(budget.max_calls)}
                    onChange={(event) => patchBudget({ max_calls: Number(event.target.value) })} />
                </Field>
                <Field label="窗口分钟数">
                  <Input type="number" min={1} step={1} aria-label="窗口分钟数" value={String(budget.window_minutes ?? 60)}
                    onChange={(event) => patchBudget({ window_minutes: Number(event.target.value) })} />
                </Field>
                <Field label="计数范围">
                  <Select value={budget.count ?? 'all'} onChange={(value) => patchBudget({ count: value as FlowBudget['count'] })}
                    options={[{ value: 'all', label: 'WF 和插件节点' }, { value: 'wf', label: '只算 WF' }]} />
                </Field>
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
