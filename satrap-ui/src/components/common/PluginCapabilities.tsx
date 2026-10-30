import type { PluginCapabilityKind, PluginCapabilitySelection, PluginInteropDeclarations, PluginCapabilityState } from '@/api/types';
import { cn } from '@/utils/cn';

export const PLUGIN_CAPABILITY_LABELS: Record<string, string> = { tools: '工具', services: '插件服务', skills: '技能', mcp: 'MCP', handlers: '前处理', commands: '命令' };
const CAPABILITY_TINT: Record<string, string> = { tools: 'accent', services: 'purple', skills: 'green', mcp: 'orange', handlers: 'pink', commands: 'teal' };

interface Props {
  capabilities: Record<string, Record<string, string>>;
  values?: Record<string, Record<string, boolean>>;
  active?: boolean;
  disabled?: boolean;
  onChange?: (kind: string, name: string, enabled: boolean) => void;
  effective?: Record<string, PluginCapabilityState[]>;
}

export function capabilityStatus(item: PluginCapabilityState): string {
  const reasons: Record<string, string> = { disabled: '独立开关已关闭', plugin_disabled: '插件已停用', dependency_unavailable: '依赖不可用', replaced_by: '已被替代', not_installed: '未安装' };
  return item.enabled ? '实际可用' : `${reasons[item.reason || ''] || '实际不可用'}${item.replaced_by ? `: ${item.replaced_by}` : ''}`;
}

export function PluginInteractions({ interop }: { interop?: PluginInteropDeclarations }) {
  if (!interop || !Object.values(interop).some((group) => Object.keys(group || {}).length > 0)) return null;
  const describe = (selection: PluginCapabilitySelection) => Object.keys(PLUGIN_CAPABILITY_LABELS).flatMap((value) => {
    const kind = value as PluginCapabilityKind;
    const names = selection[kind];
    return names?.length ? [`${PLUGIN_CAPABILITY_LABELS[kind]}: ${names.join('、')}`] : [];
  }).join('；');
  return <section className="space-y-3" aria-label="插件交互关系">
    <dl className="space-y-2">
      {interop.exports && Object.keys(interop.exports).length > 0 && <div className="rounded-xl border border-line bg-glass px-3.5 py-2.5">
        <dt className="text-[11.5px] font-medium text-green">对外开放</dt><dd className="mt-1 break-all text-[12.5px] text-fg-2">{describe(interop.exports)}</dd>
      </div>}
      {Object.entries(interop.requires || {}).map(([plugin, entry]) => <div key={`requires:${plugin}`} className="rounded-xl border border-line bg-glass px-3.5 py-2.5">
        <dt className="text-[11.5px] font-medium text-accent">{entry !== true && entry.optional ? '可选依赖' : '必需依赖'} · {plugin}</dt>
        <dd className="mt-1 break-all text-[12.5px] text-fg-2">{entry === true ? '插件已启用' : describe(entry) || '插件已启用'}</dd>
      </div>)}
      {Object.entries(interop.replaces || {}).flatMap(([plugin, groups]) => Object.entries(groups).flatMap(([kind, pairs]) =>
        Object.entries(pairs || {}).map(([target, implementation]) => <div key={`replaces:${plugin}:${kind}:${target}`} className="rounded-xl border border-line bg-glass px-3.5 py-2.5">
          <dt className="text-[11.5px] font-medium text-purple">替代 · {plugin} · {PLUGIN_CAPABILITY_LABELS[kind]}</dt>
          <dd className="mt-1 break-all text-[12.5px] text-fg-2">{target} → {implementation}</dd>
        </div>)))}
      {Object.entries(interop.conflicts || {}).map(([plugin, entry]) => <div key={`conflicts:${plugin}`} className="rounded-xl border border-line bg-glass px-3.5 py-2.5">
        <dt className="text-[11.5px] font-medium text-orange">排斥 · {plugin}</dt>
        <dd className="mt-1 break-all text-[12.5px] text-fg-2">{entry === true ? '整个插件同时启用' : describe(entry)}</dd>
      </div>)}
    </dl>
  </section>;
}

export function PluginCapabilities({ capabilities, values = {}, active = true, disabled = false, onChange, effective }: Props) {
  return <div className="space-y-4">{Object.entries(PLUGIN_CAPABILITY_LABELS).map(([kind, label]) => {
    const items = Object.entries(capabilities[kind] || {});
    if (onChange && !items.length) return null;
    return <section key={kind} className={cn('rounded-2xl border border-line bg-glass p-4', items.length && `tint-${CAPABILITY_TINT[kind]}`)}>
      <h3 className="text-[13px] font-semibold text-fg">{label} · {items.length}</h3>
      {items.length ? <ul className="mt-2 space-y-2">{items.map(([name, description]) => {
        const enabled = values[kind]?.[name] !== false;
        const actual = effective?.[kind]?.find((item) => item.name === name);
        return <li key={name} className="rounded-lg bg-[rgb(var(--tint)/0.05)] px-3 py-2">
          <label className="flex items-start justify-between gap-3">
            <span className="min-w-0">
              <span className={active && enabled ? 'break-all font-mono text-[12.5px] font-medium text-fg' : 'break-all font-mono text-[12.5px] font-medium text-fg-3'}>{name}</span>
              {description && <span className="mt-1 block whitespace-pre-wrap text-[12px] leading-4 text-fg-3">{description}</span>}
              {onChange && enabled && !active && <span className="mt-1 block text-[11px] text-orange">插件停用中</span>}
              {actual && <span className="mt-1 block text-[11px] text-fg-3">期望: {enabled ? '启用' : '停用'} · {capabilityStatus(actual)}</span>}
            </span>
            {onChange && <input type="checkbox" className="mt-0.5 h-4 w-4 flex-none accent-[rgb(var(--rgb-teal))]" aria-label={`${label} ${name}`} checked={enabled} disabled={disabled} onChange={(event) => onChange(kind, name, event.target.checked)} />}
          </label>
        </li>;
      })}</ul> : <p className="mt-1 text-[12.5px] text-fg-3">未声明</p>}
    </section>;
  })}</div>;
}
