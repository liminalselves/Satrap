import type { PluginCapabilityKind, PluginCapabilitySelection, PluginInteropDeclarations, PluginCapabilityState } from '@/api/types';

export const PLUGIN_CAPABILITY_LABELS: Record<string, string> = { tools: '工具', services: '插件服务', skills: '技能', mcp: 'MCP', handlers: '前处理', commands: '命令' };

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
  return <section className="space-y-3 border-t border-glass-border pt-4" aria-label="插件交互关系">
    <h3 className="font-medium text-text-primary">插件交互</h3>
    <dl className="space-y-3 text-sm">
      {interop.exports && Object.keys(interop.exports).length > 0 && <div>
        <dt className="text-text-tertiary">对外开放</dt><dd className="mt-1 break-all">{describe(interop.exports)}</dd>
      </div>}
      {Object.entries(interop.requires || {}).map(([plugin, entry]) => <div key={`requires:${plugin}`}>
        <dt className="text-text-tertiary">{entry !== true && entry.optional ? '可选依赖' : '必需依赖'} · {plugin}</dt>
        <dd className="mt-1 break-all">{entry === true ? '插件已启用' : describe(entry) || '插件已启用'}</dd>
      </div>)}
      {Object.entries(interop.replaces || {}).flatMap(([plugin, groups]) => Object.entries(groups).flatMap(([kind, pairs]) =>
        Object.entries(pairs || {}).map(([target, implementation]) => <div key={`replaces:${plugin}:${kind}:${target}`}>
          <dt className="text-text-tertiary">替代 · {plugin} · {PLUGIN_CAPABILITY_LABELS[kind]}</dt>
          <dd className="mt-1 break-all">{target} → {implementation}</dd>
        </div>)))}
      {Object.entries(interop.conflicts || {}).map(([plugin, entry]) => <div key={`conflicts:${plugin}`}>
        <dt className="text-text-tertiary">排斥 · {plugin}</dt>
        <dd className="mt-1 break-all">{entry === true ? '整个插件同时启用' : describe(entry)}</dd>
      </div>)}
    </dl>
  </section>;
}

export function PluginCapabilities({ capabilities, values = {}, active = true, disabled = false, onChange, effective }: Props) {
  return <div className="space-y-4">{Object.entries(PLUGIN_CAPABILITY_LABELS).map(([kind, label]) => {
    const items = Object.entries(capabilities[kind] || {});
    if (onChange && !items.length) return null;
    return <section key={kind}>
      <h3 className="font-medium text-text-primary">{label} · {items.length}</h3>
      {items.length ? <ul className="mt-2 space-y-2">{items.map(([name, description]) => {
        const enabled = values[kind]?.[name] !== false;
        const actual = effective?.[kind]?.find((item) => item.name === name);
        return <li key={name} className="rounded-lg bg-glass p-3">
          <label className="flex items-start justify-between gap-3">
            <span className="min-w-0"><span className={active && enabled ? 'break-all font-medium' : 'break-all font-medium text-text-tertiary'}>{name}</span>
              {description && <span className="mt-1 block whitespace-pre-wrap text-sm text-text-secondary">{description}</span>}
              {onChange && enabled && !active && <span className="mt-1 block text-xs text-text-tertiary">插件停用中</span>}
              {actual && <span className="mt-1 block text-xs text-text-secondary">期望: {enabled ? '启用' : '停用'} · {capabilityStatus(actual)}</span>}
            </span>
            {onChange && <input type="checkbox" aria-label={`${label} ${name}`} checked={enabled} disabled={disabled} onChange={(event) => onChange(kind, name, event.target.checked)} />}
          </label>
        </li>;
      })}</ul> : <p className="mt-1 text-sm text-text-tertiary">未声明</p>}
    </section>;
  })}</div>;
}
