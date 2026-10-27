import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Plus, Puzzle, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { Modal } from '@/components/kit/Modal';
import { Skeleton } from '@/components/kit/Feedback';
import { Switch } from '@/components/kit/Switch';
import { PluginConfigFields } from '@/components/common/PluginConfigFields';
import { PluginCapabilities, PluginInteractions } from '@/components/common/PluginCapabilities';
import { globalPluginRead } from '@/api/pluginReads';
import { usePluginConfigOptions } from '@/hooks/usePluginConfigOptions';
import { errorText } from '@/utils/errorText';
import { cn } from '@/utils/cn';
import type {
  EdictumAvailablePlugin,
  EdictumPluginConfig,
  EdictumSessionConfig,
  GlobalPluginConfig,
} from '@/api/types';

interface ManagedPluginState {
  config_version?: number;
  migration_state?: Record<string, unknown>;
  present: boolean;
  enabled: boolean;
  config: Record<string, unknown>;
  capabilities: Record<string, Record<string, boolean>>;
}

export interface PluginManagerProps {
  open: boolean;
  configName: string | null;
  availablePlugins: EdictumAvailablePlugin[];
  configuredPlugins: EdictumSessionConfig['plugins'] | undefined;
  saving: boolean;
  progress?: string;
  statusContent?: React.ReactNode;
  onClose: () => void;
  onSave: (plugins: EdictumSessionConfig['plugins']) => void | Promise<void>;
}

function normalizeConfiguredPlugins(plugins: EdictumSessionConfig['plugins']): Record<string, ManagedPluginState> {
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

export function PluginManager({
  open,
  configName,
  availablePlugins,
  configuredPlugins,
  saving,
  progress,
  statusContent,
  onClose,
  onSave,
}: PluginManagerProps) {
  const [states, setStates] = useState<Record<string, ManagedPluginState>>({});
  const draftConfigName = useRef<string | null>(null);
  const [selected, setSelected] = useState('');
  const [globalConfigs, setGlobalConfigs] = useState<Record<string, GlobalPluginConfig>>({});
  const [globalErrors, setGlobalErrors] = useState<Record<string, string>>({});

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

  const resetConfigValue = (name: string, key: string) => {
    setStates((current) => {
      const config = { ...(current[name]?.config || {}) };
      delete config[key];
      return { ...current, [name]: { ...current[name], config } };
    });
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

  const selectedPlugin = selected ? pluginMap[selected] : undefined;
  const selectedState = selected ? states[selected] : undefined;
  const selectedUnknown = !!selected && unknownPluginNames.includes(selected);
  const selectedPresent = selectedState?.present === true;
  const options = usePluginConfigOptions(selectedPlugin?.config_schema, open && selectedPresent);
  const selectedName = selectedPlugin?.name;
  const selectedSchema = selectedPlugin?.config_schema;

  useEffect(() => {
    if (!open) { setGlobalConfigs({}); setGlobalErrors({}); return; }
    if (!selectedName || !selectedPresent || !selectedSchema || !Object.keys(selectedSchema).length) return;
    const controller = new AbortController();
    setGlobalConfigs((previous) => { const next = { ...previous }; delete next[selectedName]; return next; });
    setGlobalErrors((previous) => ({ ...previous, [selectedName]: '' }));
    globalPluginRead(selectedName).read({ signal: controller.signal }).then((result) => {
      if (!controller.signal.aborted) setGlobalConfigs((previous) => ({ ...previous, [selectedName]: result }));
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setGlobalErrors((previous) => ({ ...previous, [selectedName]: errorText(error) }));
    });
    return () => controller.abort();
  }, [open, selectedName, selectedSchema, selectedPresent]);

  const navItem = (name: string, tone: 'present' | 'addable' | 'unknown') => (
    <button
      key={name}
      type="button"
      onClick={() => setSelected(name)}
      title={name}
      className={cn(
        'flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-[13px] transition-colors',
        selected === name ? 'bg-[rgb(var(--tint)/0.16)] font-medium text-tint' : 'text-fg-2 hover:bg-glass-hover hover:text-fg',
        tone !== 'present' && 'opacity-70',
      )}
    >
      <Puzzle className={cn('h-3.5 w-3.5 flex-none', tone === 'unknown' && 'text-red')} />
      <span className="min-w-0 flex-1 truncate">{name}</span>
      {tone === 'present' && states[name]?.enabled === false && <span className="h-1.5 w-1.5 flex-none rounded-full bg-[var(--text-4)]" title="已停用" />}
      {tone === 'addable' && <Plus className="h-3 w-3 flex-none text-fg-4" />}
      {tone === 'unknown' && <span className="text-[10px] text-red">失效</span>}
    </button>
  );

  return (
    <Modal
      open={open}
      onClose={() => { if (!saving) onClose(); }}
      title={configName ? `管理 Edictum 插件: ${configName}` : '管理 Edictum 插件'}
      description="插件配置属于当前命名会话; 保存后运行时会同步能力变化到活跃会话."
      icon={<Puzzle className="h-[18px] w-[18px]" />}
      size="xl"
      className="tint-teal"
      footer={<>
        <span role={saving ? 'status' : undefined} className="mr-auto text-xs text-fg-3">{saving && (progress || '正在同步插件配置…')}</span>
        <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
        <Button variant="tinted" tint="teal" loading={saving} onClick={() => void submit()}>
          {saving ? '保存中…' : '保存插件配置'}
        </Button>
      </>}
    >
      <div className="flex min-h-[440px] flex-col gap-3 sm:flex-row sm:gap-4">
        <nav aria-label="插件列表" className="scroll flex max-h-44 w-full shrink-0 flex-col gap-0.5 overflow-y-auto rounded-xl border border-line bg-glass p-1.5 sm:max-h-none sm:w-48 sm:border-0 sm:bg-transparent sm:p-0">
          {presentNames.length > 0 && (
            <>
              <p className="px-2.5 pb-0.5 text-[10.5px] font-medium uppercase tracking-wider text-fg-4">已配置</p>
              {presentNames.map((name) => navItem(name, 'present'))}
            </>
          )}
          {addableNames.length > 0 && (
            <>
              <p className="px-2.5 pb-0.5 pt-2 text-[10.5px] font-medium uppercase tracking-wider text-fg-4">可添加</p>
              {addableNames.map((name) => navItem(name, 'addable'))}
            </>
          )}
          {unknownPluginNames.length > 0 && (
            <>
              <p className="px-2.5 pb-0.5 pt-2 text-[10.5px] font-medium uppercase tracking-wider text-fg-4">已失效</p>
              {unknownPluginNames.map((name) => navItem(name, 'unknown'))}
            </>
          )}
          {presentNames.length === 0 && addableNames.length === 0 && unknownPluginNames.length === 0 && (
            <p className="px-2.5 py-3 text-xs text-fg-3">没有扫描到可用插件</p>
          )}
        </nav>

        <div className="scroll min-h-0 min-w-0 flex-1 overflow-y-auto border-t border-line pt-3 sm:border-l sm:border-t-0 sm:pl-4 sm:pt-0">
          {!selected && <p className="text-sm text-fg-3">从左侧选择一个插件</p>}

          {selectedUnknown && (
            <div className="space-y-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="font-mono text-sm font-semibold text-fg">{selected}</h3>
                  <p className="mt-1 text-xs text-red">插件目录当前不可用, 可从配置中移除.</p>
                </div>
                <Button size="sm" variant="danger" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => removePlugin(selected)}>移除</Button>
              </div>
              <div className="rounded-xl border border-line bg-glass px-3 py-2 text-xs text-fg-3">
                该插件仍在命名配置中, 但未在插件目录中扫描到. 运行时可能已停止加载它.
              </div>
            </div>
          )}

          {selectedPlugin && (
            <div className="space-y-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <h3 className="font-mono text-sm font-semibold text-fg">{selectedPlugin.name}</h3>
                    {selectedPlugin.version && <Badge>v{selectedPlugin.version}</Badge>}
                    <Badge tone="teal">{capabilityCountOf(selectedPlugin)} 项能力</Badge>
                    {Object.keys(selectedPlugin.config_schema).length > 0 && <Badge>{Object.keys(selectedPlugin.config_schema).length} 项配置</Badge>}
                  </div>
                  <p className="mt-1 text-[12.5px] leading-5 text-fg-3">{selectedPlugin.description || '暂无描述'}</p>
                  <Link
                    to={`/plugins/${encodeURIComponent(selectedPlugin.name)}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-1 inline-flex text-[11px] text-accent hover:underline"
                  >
                    打开插件详情
                  </Link>
                </div>
                {selectedPresent ? (
                  <div className="flex flex-none items-center gap-2">
                    <label className="flex items-center gap-2 text-xs text-fg-3">
                      <Switch label="启用插件" size="sm" checked={selectedState?.enabled ?? false} disabled={saving} onChange={(value) => setPluginEnabled(selectedPlugin.name, value)} />
                      {selectedState?.enabled ? '启用' : '停用'}
                    </label>
                    <Button size="sm" variant="danger" aria-label={`移除插件 ${selectedPlugin.name}`} disabled={saving} onClick={() => removePlugin(selectedPlugin.name)}>
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                ) : (
                  <Button size="sm" variant="tinted" tint="teal" icon={<Plus className="h-3.5 w-3.5" />} onClick={() => addPlugin(selectedPlugin.name)}>添加</Button>
                )}
              </div>

              {selectedPresent && selectedState?.enabled === false && (
                <div className="rounded-xl border border-[rgb(var(--rgb-orange)/0.24)] bg-[rgb(var(--rgb-orange)/0.08)] px-3 py-2 text-xs text-fg-2">
                  插件已停用: 配置会保留, 但不会在会话中加载能力.
                </div>
              )}

              {selectedPresent && Object.keys(selectedPlugin.config_schema).length > 0 && (
                <section className="space-y-2.5 border-t border-line pt-3.5">
                  <h4 className="flex items-center gap-1.5 text-[13px] font-medium text-fg"><Puzzle className="h-3.5 w-3.5 text-fg-3" />命名配置</h4>
                  {options.modelError && <p role="alert" className="text-xs text-red">模型选项读取失败: {options.modelError}</p>}
                  {options.knowledgeError && <p role="alert" className="text-xs text-red">知识库选项读取失败: {options.knowledgeError}</p>}
                  {globalErrors[selectedPlugin.name] && <p role="alert" className="text-xs text-red">读取继承参数失败: {globalErrors[selectedPlugin.name]}</p>}
                  {!globalConfigs[selectedPlugin.name] && !globalErrors[selectedPlugin.name] && (
                    <div role="status" className="space-y-2">
                      <Skeleton className="h-14 w-full" />
                      <Skeleton className="h-14 w-full" />
                    </div>
                  )}
                  {globalConfigs[selectedPlugin.name] && (
                    <PluginConfigFields
                      schema={selectedPlugin.config_schema}
                      values={selectedState?.config || {}}
                      inherited={globalConfigs[selectedPlugin.name]?.config}
                      namedMode
                      sources={Object.fromEntries(Object.keys(globalConfigs[selectedPlugin.name]?.overrides || {}).map((key) => [key, 'global']))}
                      modelOptions={options.models}
                      knowledgeBases={options.bases}
                      modelState={options.modelState}
                      knowledgeState={options.knowledgeState}
                      disabled={saving}
                      onChange={(key, value) => setConfigValue(selectedPlugin.name, key, value)}
                      onReset={(key) => resetConfigValue(selectedPlugin.name, key)}
                    />
                  )}
                </section>
              )}

              {selectedPresent && capabilityCountOf(selectedPlugin) > 0 && (
                <section className="space-y-4 border-t border-line pt-4">
                  <PluginCapabilities
                    capabilities={selectedPlugin.capabilities}
                    values={selectedState?.capabilities || {}}
                    active={selectedState?.enabled ?? false}
                    disabled={saving}
                    onChange={(kind, capability, enabled) => setCapabilityEnabled(selectedPlugin.name, kind, capability, enabled)}
                  />
                </section>
              )}

              <PluginInteractions interop={selectedPlugin.interop} />

              {!selectedPresent && (
                <p className="border-t border-line pt-3 text-xs text-fg-3">添加后才能编辑此插件的会话配置与能力开关.</p>
              )}
            </div>
          )}
        </div>
      </div>

      {statusContent}
      {!saving && selectedPresent && (
        <p className="mt-3 flex items-center gap-1.5 text-[11px] text-fg-4"><Check className="h-3 w-3" />本次编辑只在当前命名配置生效; 保存后同步活跃会话</p>
      )}
    </Modal>
  );
}
