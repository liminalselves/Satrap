export const PLUGIN_CAPABILITY_LABELS: Record<string, string> = { tools: '工具', skills: '技能', mcp: 'MCP', handlers: '前处理', commands: '命令' };

interface Props {
  capabilities: Record<string, Record<string, string>>;
  values?: Record<string, Record<string, boolean>>;
  active?: boolean;
  disabled?: boolean;
  onChange?: (kind: string, name: string, enabled: boolean) => void;
}

export function PluginCapabilities({ capabilities, values = {}, active = true, disabled = false, onChange }: Props) {
  return <div className="space-y-4">{Object.entries(PLUGIN_CAPABILITY_LABELS).map(([kind, label]) => {
    const items = Object.entries(capabilities[kind] || {});
    if (onChange && !items.length) return null;
    return <section key={kind}>
      <h3 className="font-medium text-text-primary">{label} · {items.length}</h3>
      {items.length ? <ul className="mt-2 space-y-2">{items.map(([name, description]) => {
        const enabled = values[kind]?.[name] !== false;
        return <li key={name} className="rounded-lg bg-glass p-3">
          <label className="flex items-start justify-between gap-3">
            <span className="min-w-0"><span className={active && enabled ? 'break-all font-medium' : 'break-all font-medium text-text-tertiary'}>{name}</span>
              {description && <span className="mt-1 block whitespace-pre-wrap text-sm text-text-secondary">{description}</span>}
              {onChange && enabled && !active && <span className="mt-1 block text-xs text-text-tertiary">插件停用中</span>}
            </span>
            {onChange && <input type="checkbox" aria-label={`${label} ${name}`} checked={enabled} disabled={disabled} onChange={(event) => onChange(kind, name, event.target.checked)} />}
          </label>
        </li>;
      })}</ul> : <p className="mt-1 text-sm text-text-tertiary">未声明</p>}
    </section>;
  })}</div>;
}
