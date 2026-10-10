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
import { Toggle } from '@/components/ui/Toggle';
import { Link } from 'react-router-dom';
import { PluginCapabilities } from '@/components/common/PluginCapabilities';
import { cn } from '@/utils/cn';

interface ManagedPluginState {
  config_version?: number;
  migration_state?: Record<string, unknown>;
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
      config_version: item.config_version,
      migration_state: item.migration_state,
      config: { ...(item.config || {}) },
      capabilities: Object.fromEntries(
        Object.entries(item.capabilities || {}).map(([kind, values]) => [kind, { ...values }]),
      ),
    };
  }
  return normalized;
}

function capabilityCountOf(plugin: EdictumAvailablePlugin): number {
  return Object.values(plugin.capabilities).reduce((count, items) => count + Object.keys(items).length, 0);
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
  const [selected, setSelected] = useState('');
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
  const presentNames = useMemo(
    () => availablePlugins.map((plugin) => plugin.name).filter((name) => states[name]?.present),
    [availablePlugins, states],
  );
  const addableNames = useMemo(
    () => availablePlugins.map((plugin) => plugin.name).filter((name) => !states[name]?.present),
    [availablePlugins, states],
  );

  // 每次打开默认选中第一个已配置插件, 没有则选第一个可用插件
  useEffect(() => {
    if (!open) return;
    setSelected((current) => {
      if (current && (pluginMap[current] || unknownPluginNames.includes(current))) return current;
      return presentNames[0] || availablePlugins[0]?.name || unknownPluginNames[0] || '';
    });
  }, [open, availablePlugins, pluginMap, presentNames, unknownPluginNames]);

  const addPlugin = (name: string) => {
    setStates((current) => ({
      ...current,
      [name]: {
        present: true,
        enabled: true,
        config_version: current[name]?.config_version ?? availablePlugins.find((plugin) => plugin.name === name)?.config_version,
        migration_state: current[name]?.migration_state,
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
        config_version: state.config_version,
        migration_state: state.migration_state,
        config: state.config,
        capabilities: state.capabilities,
      }));
    await onSave(plugins);
  };

  const renderNavItem = (name: string, tone: 'present' | 'addable' | 'unknown') => (
    <button
      key={name}
      onClick={() => setSelected(name)}
      className={cn('glass-nav-item nav-accent nav-fill', selected === name && 'active', tone !== 'present' && 'opacity-70')}
      title={name}
    >
      <Puzzle className={cn('h-4 w-4 shrink-0', tone === 'unknown' && 'text-error')} />
      <span className="min-w-0 flex-1 truncate text-left">{name}</span>
      {tone === 'present' && states[name]?.enabled === false && (
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-text-tertiary" title="已停用" />
      )}
    </button>
  );

  const selectedPlugin = selected ? pluginMap[selected] : undefined;
  const selectedState = selected ? states[selected] : undefined;
  const selectedUnknown = !!selected && unknownPluginNames.includes(selected);
  const selectedPresent = selectedState?.present === true;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={configName ? `管理 Edictum 插件: ${configName}` : '管理 Edictum 插件'}
      size="3xl"
    >
      <p className="mb-3 text-sm text-text-secondary">
        插件配置属于当前 Edictum 命名会话, 后端运行时会同步能力变化到活跃会话
      </p>
      <div className="flex h-[60vh] min-h-[420px] gap-4">
        {/* 左侧插件导航: 已配置 / 可添加 / 已失效 */}
        <nav className="w-44 shrink-0 overflow-y-auto custom-scrollbar">
          {presentNames.length > 0 && (
            <>
              <p className="px-3 pb-1 text-xs text-text-tertiary">已配置</p>
              {presentNames.map((name) => renderNavItem(name, 'present'))}
            </>
          )}
          {addableNames.length > 0 && (
            <>
              <p className="px-3 pb-1 pt-2 text-xs text-text-tertiary">可添加</p>
              {addableNames.map((name) => renderNavItem(name, 'addable'))}
            </>
          )}
          {unknownPluginNames.length > 0 && (
            <>
              <p className="px-3 pb-1 pt-2 text-xs text-text-tertiary">已失效</p>
              {unknownPluginNames.map((name) => renderNavItem(name, 'unknown'))}
            </>
          )}
          {availablePlugins.length === 0 && unknownPluginNames.length === 0 && (
            <p className="px-3 text-sm text-text-tertiary">没有扫描到可用插件</p>
          )}
        </nav>

        {/* 右侧插件详情与配置 */}
        <div className="min-w-0 flex-1 overflow-y-auto border-l border-glass-border pl-4 pr-1 custom-scrollbar">
          {!selected && <p className="text-sm text-text-tertiary">从左侧选择一个插件</p>}

          {selectedUnknown && (
            <div className="space-y-4">
              <div className="flex items-center justify-between gap-3">
                <span className="font-medium text-text-primary">{selected}</span>
                <Button size="sm" variant="danger" onClick={() => removePlugin(selected)}>
                  <Trash2 className="mr-1 h-4 w-4" />移除
                </Button>
              </div>
              <p className="text-sm text-error">插件目录当前不可用, 可从配置中移除</p>
            </div>
          )}

          {selectedPlugin && (
            <div className="space-y-5">
              {/* 标题行: 名称/版本/启停或添加 */}
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-text-primary">{selectedPlugin.name}</span>
                    {selectedPlugin.version && <Badge variant="default">v{selectedPlugin.version}</Badge>}
                    <Badge variant="info">{capabilityCountOf(selectedPlugin)} 项能力</Badge>
                    {Object.keys(selectedPlugin.config_schema).length > 0 && (
                      <Badge variant="default">{Object.keys(selectedPlugin.config_schema).length} 项配置</Badge>
                    )}
                  </div>
                  <p className="mt-1 text-sm text-text-secondary">
                    {selectedPlugin.description || '暂无描述'}
                  </p>
                  <Link className="text-xs text-accent" target="_blank" rel="noopener noreferrer" to={`/plugins/${encodeURIComponent(selectedPlugin.name)}`}>打开插件详情</Link>
                </div>
                {!selectedPresent ? (
                  <Button size="sm" variant="subtle" onClick={() => addPlugin(selectedPlugin.name)}>
                    <Plus className="mr-1 h-4 w-4" />添加
                  </Button>
                ) : (
                  <div className="flex items-center gap-3">
                    <label className="flex items-center gap-2 text-sm text-text-secondary">
                      <Toggle
                        checked={selectedState?.enabled ?? false}
                        onChange={(value) => setPluginEnabled(selectedPlugin.name, value)}
                        title={selectedState?.enabled ? '停用插件' : '启用插件'}
                      />
                      启用
                    </label>
                    <Button size="sm" variant="danger" title="从配置移除" onClick={() => removePlugin(selectedPlugin.name)}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                )}
              </div>

              {selectedPresent && Object.keys(selectedPlugin.config_schema).length > 0 && (
                <div className="grid gap-3 border-t border-glass-border pt-4 md:grid-cols-2">
                  {modelError && <p role="alert" className="text-sm text-error">{modelError}</p>}
                  {globalErrors[selectedPlugin.name] && <p role="alert" className="text-sm text-error">读取继承参数失败: {globalErrors[selectedPlugin.name]}</p>}
                  <PluginConfigFields schema={selectedPlugin.config_schema} values={selectedState?.config || {}} inherited={globalConfigs[selectedPlugin.name]?.config} namedMode
                    sources={Object.fromEntries(Object.keys(globalConfigs[selectedPlugin.name]?.overrides || {}).map((key) => [key, 'global']))}
                    modelOptions={modelOptions} knowledgeBases={knowledgeBases} disabled={saving || !globalConfigs[selectedPlugin.name]}
                    onChange={(key, value) => setConfigValue(selectedPlugin.name, key, value)} onReset={(key) => {
                      setStates((current) => {
                        const config = { ...current[selectedPlugin.name].config };
                        delete config[key];
                        return { ...current, [selectedPlugin.name]: { ...current[selectedPlugin.name], config } };
                      });
                    }} />
                </div>
              )}

              {selectedPresent && capabilityCountOf(selectedPlugin) > 0 && (
                <div className="space-y-4 border-t border-glass-border pt-4">
                  <PluginCapabilities capabilities={selectedPlugin.capabilities} values={selectedState?.capabilities || {}} active={selectedState?.enabled ?? false} disabled={saving}
                    onChange={(kind, capability, enabled) => setCapabilityEnabled(selectedPlugin.name, kind, capability, enabled)} />
                </div>
              )}

              {!selectedPresent && (
                <p className="border-t border-glass-border pt-4 text-sm text-text-tertiary">
                  添加后才能编辑此插件的会话配置与能力开关
                </p>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="flex justify-end gap-2 pt-3">
        <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
        <Button variant="primary" onClick={() => void submit()} disabled={saving}>
          {saving ? '保存中…' : '保存插件配置'}
        </Button>
      </div>
    </Modal>
  );
}
