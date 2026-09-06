import { PluginConfigFields, type ModelOptions, type ConfigOption } from '@/components/common/PluginConfigFields';
import { ragApi } from '@/api/rag';
import { controlApi } from '@/api/control';
import { useEffect, useMemo, useState } from 'react';
import { Plus, Puzzle, Trash2 } from 'lucide-react';

import type {
  EdictumAvailablePlugin,
  EdictumPluginConfig,
  EdictumSessionConfig,
} from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';

interface ManagedPluginState {
  present: boolean;
  enabled: boolean;
  config: Record<string, unknown>;
  capabilities: Record<string, Record<string, boolean>>;
}

const CAPABILITY_LABELS: Record<string, string> = {
  tools: '工具',
  skills: '技能',
  mcp: 'MCP',
  handlers: '处理器',
  commands: '命令',
};

interface EdictumPluginManagerProps {
  open: boolean;
  configName: string | null;
  availablePlugins: EdictumAvailablePlugin[];
  configuredPlugins: EdictumSessionConfig['plugins'];
  saving: boolean;
  onClose: () => void;
  onSave: (plugins: EdictumSessionConfig['plugins']) => void | Promise<void>;
}

function normalizeConfiguredPlugins(
  plugins: EdictumSessionConfig['plugins'],
): Record<string, ManagedPluginState> {
  const normalized: Record<string, ManagedPluginState> = {};
  for (const item of plugins) {
    if (typeof item === 'string') {
      normalized[item] = { present: true, enabled: true, config: {}, capabilities: {} };
      continue;
    }
    normalized[item.name] = {
      present: true,
      enabled: item.enabled !== false,
      config: { ...(item.config || {}) },
      capabilities: Object.fromEntries(
        Object.entries(item.capabilities || {}).map(([kind, values]) => [kind, { ...values }]),
      ),
    };
  }
  return normalized;
}

export function EdictumPluginManager({
  open,
  configName,
  availablePlugins,
  configuredPlugins,
  saving,
  onClose,
  onSave,
}: EdictumPluginManagerProps) {
  const [states, setStates] = useState<Record<string, ManagedPluginState>>({});
  const [modelOptions, setModelOptions] = useState<ModelOptions>({});
  const [knowledgeBases, setKnowledgeBases] = useState<ConfigOption[]>([]);
  const [modelError, setModelError] = useState('');
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    controlApi.pluginModelOptions().then((result) => { if (!cancelled) { setModelOptions(result.options); setModelError(''); } })
      .catch((error) => { if (!cancelled) setModelError(error.message); });
    ragApi.list({ platformId: 'local', via: 'control' }).then((result) => {
      if (!cancelled) setKnowledgeBases(result.knowledge_bases.filter((item) => item.scope === 'global').map((item) => ({ value: item.id, label: item.name, scope: item.scope })));
    }).catch((error) => { if (!cancelled) setModelError(error.message); });
    return () => { cancelled = true; };
  }, [open]);


  useEffect(() => {
    if (open) setStates(normalizeConfiguredPlugins(configuredPlugins));
  }, [configuredPlugins, open]);

  const pluginMap = useMemo(
    () => Object.fromEntries(availablePlugins.map((plugin) => [plugin.name, plugin])),
    [availablePlugins],
  );
  const unknownPluginNames = useMemo(
    () => Object.keys(states).filter((name) => states[name].present && !pluginMap[name]),
    [pluginMap, states],
  );

  const addPlugin = (name: string) => {
    setStates((current) => ({
      ...current,
      [name]: {
        present: true,
        enabled: true,
        config: current[name]?.config || {},
        capabilities: current[name]?.capabilities || {},
      },
    }));
  };

  const removePlugin = (name: string) => {
    setStates((current) => ({
      ...current,
      [name]: {
        ...(current[name] || { enabled: false, config: {}, capabilities: {} }),
        present: false,
      },
    }));
  };

  const setPluginEnabled = (name: string, enabled: boolean) => {
    setStates((current) => ({
      ...current,
      [name]: {
        ...(current[name] || { present: true, config: {}, capabilities: {} }),
        present: true,
        enabled,
      },
    }));
  };

  const setConfigValue = (name: string, key: string, value: unknown) => {
    setStates((current) => ({
      ...current,
      [name]: {
        ...(current[name] || { present: true, enabled: true, config: {}, capabilities: {} }),
        config: { ...(current[name]?.config || {}), [key]: value },
      },
    }));
  };

  const setCapabilityEnabled = (name: string, kind: string, capability: string, enabled: boolean) => {
    setStates((current) => ({
      ...current,
      [name]: {
        ...(current[name] || { present: true, enabled: true, config: {}, capabilities: {} }),
        capabilities: {
          ...(current[name]?.capabilities || {}),
          [kind]: {
            ...(current[name]?.capabilities?.[kind] || {}),
            [capability]: enabled,
          },
        },
      },
    }));
  };

  const submit = async () => {
    const plugins: EdictumPluginConfig[] = Object.entries(states)
      .filter(([, state]) => state.present)
      .map(([name, state]) => ({
        name,
        enabled: state.enabled,
        config: state.config,
        capabilities: state.capabilities,
      }));
    await onSave(plugins);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={configName ? `管理 Edictum 插件: ${configName}` : '管理 Edictum 插件'}
      size="xl"
    >
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          插件配置属于当前 Edictum 命名会话, 后端运行时会同步能力变化到活跃会话
        </p>

        {availablePlugins.length === 0 && (
          <p className="text-sm text-text-tertiary">没有扫描到可用插件</p>
        )}

        {availablePlugins.map((plugin) => {
          const state = states[plugin.name];
          const present = state?.present === true;
          const capabilityCount = Object.values(plugin.capabilities)
            .reduce((count, items) => count + Object.keys(items).length, 0);
          const schemaEntries = Object.entries(plugin.config_schema);
          return (
            <div key={plugin.name} className="glass-card rounded-lg p-4">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Puzzle className="h-4 w-4 shrink-0 text-accent" />
                    <span className="font-medium text-text-primary">{plugin.name}</span>
                    {plugin.version && <Badge variant="default">v{plugin.version}</Badge>}
                    <Badge variant="info">{capabilityCount} 项能力</Badge>
                    {schemaEntries.length > 0 && (
                      <Badge variant="default">{schemaEntries.length} 项配置</Badge>
                    )}
                  </div>
                  <p className="mt-1 text-sm text-text-secondary">
                    {plugin.description || '暂无描述'}
                  </p>
                </div>
                {!present ? (
                  <Button size="sm" variant="subtle" onClick={() => addPlugin(plugin.name)}>
                    <Plus className="mr-1 h-4 w-4" />添加
                  </Button>
                ) : (
                  <div className="flex items-center gap-2">
                    <label className="flex items-center gap-2 text-sm text-text-secondary">
                      <input
                        type="checkbox"
                        checked={state.enabled}
                        onChange={(event) => setPluginEnabled(plugin.name, event.target.checked)}
                        className="h-4 w-4 accent-accent"
                      />
                      启用
                    </label>
                    <Button size="sm" variant="danger" onClick={() => removePlugin(plugin.name)}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                )}
              </div>

              {present && schemaEntries.length > 0 && (
                <div className="mt-4 grid gap-3 border-t border-glass-border pt-4 md:grid-cols-2">
                  {modelError && <p role="alert" className="text-sm text-error">{modelError}</p>}
                  <PluginConfigFields schema={plugin.config_schema} values={state.config} modelOptions={modelOptions} knowledgeBases={knowledgeBases}
                    onChange={(key, value) => setConfigValue(plugin.name, key, value)} />
                </div>
              )}

              {present && capabilityCount > 0 && (
                <div className="mt-4 space-y-4 border-t border-glass-border pt-4">
                  {Object.entries(plugin.capabilities).map(([kind, capabilities]) => {
                    const capabilityEntries = Object.entries(capabilities);
                    if (capabilityEntries.length === 0) return null;
                    return (
                      <div key={kind}>
                        <div className="mb-2 flex items-center gap-2">
                          <span className="text-sm font-medium text-text-primary">
                            {CAPABILITY_LABELS[kind] || kind}
                          </span>
                          <Badge variant="default">{capabilityEntries.length}</Badge>
                        </div>
                        <div className="grid gap-2 md:grid-cols-2">
                          {capabilityEntries.map(([capability, description]) => {
                            const independentlyEnabled = state.capabilities[kind]?.[capability] !== false;
                            const effective = state.enabled && independentlyEnabled;
                            return (
                              <label
                                key={capability}
                                className="glass-card flex items-start justify-between gap-3 rounded-lg px-3 py-2"
                              >
                                <span className="min-w-0">
                                  <span className={effective ? 'text-sm text-text-primary' : 'text-sm text-text-tertiary'}>
                                    {capability}
                                  </span>
                                  {description && (
                                    <span className="mt-0.5 block text-xs text-text-tertiary">
                                      {description}
                                    </span>
                                  )}
                                  {!state.enabled && independentlyEnabled && (
                                    <span className="mt-0.5 block text-xs text-text-tertiary">插件停用中</span>
                                  )}
                                </span>
                                <input
                                  type="checkbox"
                                  checked={independentlyEnabled}
                                  onChange={(event) => setCapabilityEnabled(
                                    plugin.name,
                                    kind,
                                    capability,
                                    event.target.checked,
                                  )}
                                  className="mt-0.5 h-4 w-4 shrink-0 accent-accent"
                                />
                              </label>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}

        {unknownPluginNames.map((name) => (
          <div key={name} className="glass-card flex items-center justify-between gap-3 rounded-lg p-4">
            <div>
              <span className="font-medium text-text-primary">{name}</span>
              <p className="mt-1 text-xs text-error">插件目录当前不可用, 可从配置中移除</p>
            </div>
            <Button size="sm" variant="danger" onClick={() => removePlugin(name)}>
              <Trash2 className="mr-1 h-4 w-4" />移除
            </Button>
          </div>
        ))}

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
          <Button variant="primary" onClick={() => void submit()} disabled={saving}>
            {saving ? '保存中...' : '保存插件配置'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
