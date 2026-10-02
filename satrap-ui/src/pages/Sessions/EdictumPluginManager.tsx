import { PluginConfigFields, type ModelOptions, type ConfigOption } from '@/components/common/PluginConfigFields';
import { ragApi } from '@/api/rag';
import { controlApi } from '@/api/control';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Plus, Puzzle, Trash2 } from 'lucide-react';

import type {
  EdictumAvailablePlugin,
  EdictumPluginConfig,
  EdictumSessionConfig,
  GlobalPluginConfig,
} from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { Link } from 'react-router-dom';
import { PluginCapabilities } from '@/components/common/PluginCapabilities';

interface ManagedPluginState {
  present: boolean;
  enabled: boolean;
  config: Record<string, unknown>;
  capabilities: Record<string, Record<string, boolean>>;
}

interface EdictumPluginManagerProps {
  open: boolean;
  configName: string | null;
  availablePlugins: EdictumAvailablePlugin[];
  configuredPlugins: EdictumSessionConfig['plugins'] | undefined;
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
  const draftConfigName = useRef<string | null>(null);
  const [modelOptions, setModelOptions] = useState<ModelOptions>({});
  const [knowledgeBases, setKnowledgeBases] = useState<ConfigOption[]>([]);
  const [modelError, setModelError] = useState('');
  const [globalConfigs, setGlobalConfigs] = useState<Record<string, GlobalPluginConfig>>({});
  const [globalErrors, setGlobalErrors] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setGlobalConfigs({}); setGlobalErrors({});
    availablePlugins.filter((plugin) => Object.keys(plugin.config_schema).length).forEach((plugin) => {
      controlApi.getGlobalPluginConfig(plugin.name).then((result) => { if (!cancelled) setGlobalConfigs((previous) => ({ ...previous, [plugin.name]: result })); })
        .catch((error) => { if (!cancelled) setGlobalErrors((previous) => ({ ...previous, [plugin.name]: error.response?.data?.error || error.message })); });
    });
    return () => { cancelled = true; };
  }, [open, availablePlugins]);
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
    if (!open) {
      draftConfigName.current = null;
      return;
    }
    if (!configName || configuredPlugins === undefined || draftConfigName.current === configName) return;
    setStates(normalizeConfiguredPlugins(configuredPlugins));
    draftConfigName.current = configName; // 同一次编辑期间的后台刷新不能覆盖草稿
  }, [configName, configuredPlugins, open]);

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
                  <Link className="text-xs text-accent" target="_blank" rel="noopener noreferrer" to={`/plugins/${encodeURIComponent(plugin.name)}`}>打开插件详情</Link>
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
                  {globalErrors[plugin.name] && <p role="alert" className="text-sm text-error">读取继承参数失败: {globalErrors[plugin.name]}</p>}
                  <PluginConfigFields schema={plugin.config_schema} values={state.config} inherited={globalConfigs[plugin.name]?.config} namedMode
                    sources={Object.fromEntries(Object.keys(globalConfigs[plugin.name]?.overrides || {}).map((key) => [key, 'global']))}
                    modelOptions={modelOptions} knowledgeBases={knowledgeBases} disabled={saving || !globalConfigs[plugin.name]}
                    onChange={(key, value) => setConfigValue(plugin.name, key, value)} onReset={(key) => {
                      setStates((current) => {
                        const config = { ...current[plugin.name].config };
                        delete config[key];
                        return { ...current, [plugin.name]: { ...current[plugin.name], config } };
                      });
                    }} />
                </div>
              )}

              {present && capabilityCount > 0 && (
                <div className="mt-4 space-y-4 border-t border-glass-border pt-4">
                  <PluginCapabilities capabilities={plugin.capabilities} values={state.capabilities} active={state.enabled} disabled={saving}
                    onChange={(kind, capability, enabled) => setCapabilityEnabled(plugin.name, kind, capability, enabled)} />
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
