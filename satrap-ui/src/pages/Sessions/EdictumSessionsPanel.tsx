import { useCallback, useEffect, useMemo, useState } from 'react';
import { Play, Plus, Power, PowerOff, Puzzle, RefreshCw, Settings, Trash2 } from 'lucide-react';

import { edictumApi } from '@/api/edictum';
import { sessionApi } from '@/api/session';
import { controlApi } from '@/api/control';
import type { ConfigReloadResult } from '@/api/backend';
import { useBackendStore } from '@/stores/useBackendStore';
import type {
  EdictumAvailablePlugin,
  EdictumSessionConfig,
  EdictumTypeDefinition,
  PlatformConfig,
} from '@/api/types';
import { ActionButtons, DataTable, FormModal } from '@/components/common';
import type { Column, FormField } from '@/components/common';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { toast } from '@/components/ui/Toast';
import { EdictumPluginManager } from './EdictumPluginManager';

interface EdictumConfigItem {
  name: string;
  config: EdictumSessionConfig;
}

interface EdictumSessionsPanelProps {
  llmNames: string[];
  onRuntimeCreated?: () => void | Promise<void>;
}

function parseJsonObject(value: string, label: string): Record<string, unknown> {
  const parsed = JSON.parse(value) as unknown;
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function parsePlugins(value: string): EdictumSessionConfig['plugins'] {
  const parsed = JSON.parse(value) as unknown;
  if (!Array.isArray(parsed)) {
    throw new Error('插件配置必须是 JSON 数组');
  }
  for (const item of parsed) {
    if (typeof item === 'string' && item.trim()) continue;
    if (
      typeof item === 'object'
      && item !== null
      && !Array.isArray(item)
      && typeof (item as { name?: unknown }).name === 'string'
      && Boolean((item as { name: string }).name.trim())
    ) continue;
    throw new Error('插件项必须是非空名称或包含 name 的对象');
  }
  return parsed as EdictumSessionConfig['plugins'];
}

export function EdictumSessionsPanel({ llmNames, onRuntimeCreated }: EdictumSessionsPanelProps) {
  const { isRunning, reloadConfig } = useBackendStore();
  const [types, setTypes] = useState<EdictumTypeDefinition[]>([]);
  const [availablePlugins, setAvailablePlugins] = useState<EdictumAvailablePlugin[]>([]);
  const [configs, setConfigs] = useState<Record<string, EdictumSessionConfig>>({});
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [creatingName, setCreatingName] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [pluginManagerName, setPluginManagerName] = useState<string | null>(null);
  const [pluginSaving, setPluginSaving] = useState(false);
  const [runtimeResult, setRuntimeResult] = useState<ConfigReloadResult | null>(null);
  const [retryingRuntime, setRetryingRuntime] = useState(false);
  const [runtimeTargetName, setRuntimeTargetName] = useState<string | null>(null);
  const [runtimePlatformId, setRuntimePlatformId] = useState('');
  const [form, setForm] = useState({
    name: '',
    edictum_type: '',
    enabled: true,
    description: '',
    model_name: '',
    params: '{}',
    plugins: '[]',
  });

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [availableTypes, plugins, storedConfigs, platformResult] = await Promise.all([
        edictumApi.listTypes(),
        edictumApi.listPlugins(),
        edictumApi.list(),
        controlApi.listPlatforms(),
      ]);
      setTypes(availableTypes);
      setAvailablePlugins(plugins);
      setConfigs(storedConfigs);
      setPlatforms(platformResult.platforms || []);
    } catch (error) {
      toast('error', '读取 Edictum 配置失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openCreate = useCallback(() => {
    setEditingName(null);
    setForm({
      name: '',
      edictum_type: types[0]?.name || '',
      enabled: true,
      description: '',
      model_name: llmNames[0] || '',
      params: '{}',
      plugins: '[]',
    });
    setModalOpen(true);
  }, [llmNames, types]);

  const openEdit = useCallback((name: string) => {
    const config = configs[name];
    if (!config) return;
    setEditingName(name);
    setForm({
      name,
      edictum_type: config.edictum_type,
      enabled: config.enabled,
      description: config.description || '',
      model_name: config.model_name || '',
      params: JSON.stringify(config.params || {}, null, 2),
      plugins: JSON.stringify(config.plugins || [], null, 2),
    });
    setModalOpen(true);
  }, [configs]);

  const save = useCallback(async () => {
    const name = form.name.trim();
    const typeName = form.edictum_type.trim();
    if (!name || !typeName) {
      toast('error', '名称和 Edictum 类型为必填项');
      return;
    }
    setSaving(true);
    try {
      const payload = {
        name,
        edictum_type: typeName,
        enabled: form.enabled,
        description: form.description,
        model_name: form.model_name,
        params: parseJsonObject(form.params, '参数'),
        plugins: parsePlugins(form.plugins),
      };
      if (isRunning && editingName) {
        const preview = await edictumApi.previewFullRuntimeChanges(editingName, payload);
        const affected = preview.edictum_sessions.filter((item) => item.action !== 'noop');
        if (
          affected.length > 0
          && !confirm('本次配置将立即协调 ' + affected.length + ' 个活跃会话, 是否继续?')
        ) return;
      }
      if (editingName) await edictumApi.update(editingName, payload);
      else await edictumApi.create(payload);
      const reloadResult = isRunning ? await edictumApi.applyRuntimeChanges() : null;
      if (reloadResult) setRuntimeResult(reloadResult);
      const reloaded = !reloadResult || reloadResult.ok;
      toast(
        reloaded ? 'success' : 'warning',
        reloaded
          ? (editingName ? 'Edictum 配置已保存' : 'Edictum 配置已创建')
          : 'Edictum 配置已保存, 但后端热加载失败',
      );
      setModalOpen(false);
      await refresh();
    } catch (error) {
      toast('error', '保存失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setSaving(false);
    }
  }, [editingName, form, isRunning, refresh]);

  const setEnabled = useCallback(async (name: string, enabled: boolean) => {
    try {
      if (enabled) await edictumApi.enable(name);
      else await edictumApi.disable(name);
      const reloaded = !isRunning || await reloadConfig();
      toast(
        reloaded ? 'success' : 'warning',
        reloaded ? `${name} 已${enabled ? '启用' : '禁用'}` : `${name} 已更新, 但后端热加载失败`,
      );
      await refresh();
    } catch (error) {
      toast('error', '状态更新失败: ' + (error instanceof Error ? error.message : '未知错误'));
    }
  }, [isRunning, refresh, reloadConfig]);

  const remove = useCallback(async (name: string) => {
    if (!confirm(`确定要删除 Edictum 配置 "${name}" 吗?`)) return;
    try {
      await edictumApi.remove(name);
      const reloaded = !isRunning || await reloadConfig();
      toast(
        reloaded ? 'success' : 'warning',
        reloaded ? 'Edictum 配置已删除' : 'Edictum 配置已删除, 但后端热加载失败',
      );
      await refresh();
    } catch (error) {
      toast('error', '删除失败: ' + (error instanceof Error ? error.message : '未知错误'));
    }
  }, [isRunning, refresh, reloadConfig]);

  const createRuntime = useCallback(async (name: string, selectedPlatformId?: string) => {
    const config = configs[name];
    if (!config) return;
    const boundPlatforms = platforms.filter(
      (platform) => platform.session_provider === 'edictum' && platform.session_type === name,
    );
    if (boundPlatforms.length > 1 && !selectedPlatformId) {
      setRuntimeTargetName(name);
      setRuntimePlatformId(boundPlatforms[0]?.id || '');
      return;
    }
    const targetPlatform = selectedPlatformId
      ? boundPlatforms.find((platform) => platform.id === selectedPlatformId)
      : boundPlatforms[0];
    if (selectedPlatformId && !targetPlatform) {
      toast('error', `目标平台不再绑定 Edictum 配置: ${selectedPlatformId}`);
      return;
    }
    setCreatingName(name);
    try {
      const result = await sessionApi.createRuntime({
        session_provider: 'edictum',
        session_type: name,
        platform_id: targetPlatform?.id,
        adapter_id: targetPlatform?.id,
        activate: isRunning,
      }, isRunning);
      toast(
        'success',
        isRunning
          ? `Edictum 会话已创建并激活: ${result.session.session_id}`
          : `Edictum 会话已冷创建: ${result.session.session_id}`,
      );
      await onRuntimeCreated?.();
    } catch (error) {
      toast('error', '创建运行时会话失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setCreatingName(null);
    }
  }, [configs, isRunning, onRuntimeCreated, platforms]);

  const runtimePlatformOptions = useMemo(
    () => platforms
      .filter(
        (platform) => platform.session_provider === 'edictum'
          && platform.session_type === runtimeTargetName,
      )
      .map((platform) => ({ value: platform.id, label: `${platform.id} (${platform.type})` })),
    [platforms, runtimeTargetName],
  );

  const savePlugins = useCallback(async (plugins: EdictumSessionConfig['plugins']) => {
    if (!pluginManagerName) return;
    setPluginSaving(true);
    try {
      if (isRunning) {
        const preview = await edictumApi.previewRuntimeChanges(pluginManagerName, plugins);
        const affected = preview.edictum_sessions.filter(
          (item) => item.config_name === pluginManagerName && (item.plugins?.length || 0) > 0,
        );
        const pluginChanges = affected.reduce((total, item) => total + (item.plugins?.length || 0), 0);
        if (
          pluginChanges > 0
          && !confirm(`本次插件配置将影响 ${affected.length} 个活跃会话, 共 ${pluginChanges} 项运行时变更, 是否继续?`)
        ) return;
      }
      await edictumApi.update(pluginManagerName, { plugins });
      if (isRunning) {
        const result = await edictumApi.applyRuntimeChanges();
        setRuntimeResult(result);
        const affected = result.edictum_sessions.filter(
          (item) => item.config_name === pluginManagerName,
        );
        const failed = affected.filter((item) => !item.ok);
        toast(
          failed.length === 0 ? 'success' : 'warning',
          failed.length === 0
            ? `Edictum 插件配置已保存, 已同步 ${affected.length} 个活跃会话`
            : `插件配置已保存, ${failed.length}/${affected.length} 个活跃会话同步失败`,
        );
      } else {
        toast('success', 'Edictum 插件配置已保存, 将在会话激活时生效');
      }
      setPluginManagerName(null);
      await refresh();
    } catch (error) {
      toast('error', '插件配置保存失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setPluginSaving(false);
    }
  }, [isRunning, pluginManagerName, refresh]);

  const retryRuntimeChanges = useCallback(async () => {
    setRetryingRuntime(true);
    try {
      const result = await edictumApi.retryRuntimeChanges({});
      setRuntimeResult(result);
      const failed = result.edictum_sessions.filter((item) => !item.ok).length;
      toast(
        failed === 0 ? 'success' : 'warning',
        failed === 0 ? '插件漂移重试完成' : `插件漂移重试完成, 仍有 ${failed} 个会话未同步`,
      );
      await onRuntimeCreated?.();
    } catch (error) {
      toast('error', '重试失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setRetryingRuntime(false);
    }
  }, [onRuntimeCreated]);

  const typeMap = useMemo(
    () => Object.fromEntries(types.map((definition) => [definition.name, definition])),
    [types],
  );
  const tableData = useMemo<EdictumConfigItem[]>(
    () => Object.entries(configs).map(([name, config]) => ({ name, config })),
    [configs],
  );
  const columns = useMemo<Column<EdictumConfigItem>[]>(() => [
    { key: 'name', title: '名称', render: (item) => <span className="font-medium">{item.name}</span> },
    {
      key: 'edictum_type',
      title: 'Edictum 类型',
      render: (item) => {
        const definition = typeMap[item.config.edictum_type];
        return (
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm">{item.config.edictum_type}</span>
            {definition && <Badge variant="info">{definition.is_async ? 'async' : 'sync'}</Badge>}
          </div>
        );
      },
    },
    { key: 'model_name', title: '模型', render: (item) => item.config.model_name || '-' },
    {
      key: 'plugins',
      title: '插件',
      render: (item) => {
        const enabledCount = item.config.plugins.filter(
          (plugin) => typeof plugin === 'string' || plugin.enabled !== false,
        ).length;
        return item.config.plugins.length ? `${enabledCount}/${item.config.plugins.length} 启用` : '-';
      },
    },
    {
      key: 'enabled',
      title: '状态',
      render: (item) => (
        <Badge variant={item.config.enabled ? 'success' : 'default'}>
          {item.config.enabled ? '启用' : '禁用'}
        </Badge>
      ),
    },
    {
      key: 'actions',
      title: '操作',
      render: (item) => (
        <ActionButtons actions={[
          item.config.enabled
            ? { key: 'disable', icon: <PowerOff className="h-4 w-4" />, onClick: () => setEnabled(item.name, false), title: '禁用' }
            : { key: 'enable', icon: <Power className="h-4 w-4" />, onClick: () => setEnabled(item.name, true), title: '启用' },
          {
            key: 'create-runtime',
            icon: <Play className={`h-4 w-4 ${creatingName === item.name ? 'animate-pulse' : ''}`} />,
            onClick: () => createRuntime(item.name),
            title: isRunning ? '创建并激活会话' : '冷创建持久化会话',
            disabled: !item.config.enabled || creatingName !== null,
          },
          {
            key: 'plugins',
            icon: <Puzzle className="h-4 w-4" />,
            onClick: () => setPluginManagerName(item.name),
            title: '管理插件',
          },
          { key: 'edit', icon: <Settings className="h-4 w-4" />, onClick: () => openEdit(item.name), title: '编辑配置' },
          { key: 'delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => remove(item.name), title: '删除', className: 'text-error hover:text-error' },
        ]} />
      ),
    },
  ], [createRuntime, creatingName, isRunning, openEdit, remove, setEnabled, typeMap]);

  const fields = useMemo<FormField[]>(() => [
    { key: 'name', label: '配置名称', required: true, placeholder: '如: platform-assistant' },
    {
      key: 'edictum_type',
      label: 'Edictum 类型',
      type: 'select',
      required: true,
      options: types.map((item) => ({
        value: item.name,
        label: `${item.name} (${item.is_async ? '异步' : '同步'})`,
      })),
    },
    { key: 'enabled', label: '启用配置', type: 'checkbox', placeholder: '允许后续运行时创建此会话' },
    {
      key: 'model_name',
      label: '绑定 LLM',
      type: 'select',
      options: [{ value: '', label: '暂不绑定' }, ...llmNames.map((name) => ({ value: name, label: name }))],
    },
    { key: 'description', label: '描述', placeholder: '说明该命名会话的用途' },
    { key: 'params', label: '会话参数 (JSON 对象)', type: 'textarea', rows: 10 },
  ], [llmNames, types]);

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h3 className="text-lg font-semibold text-text-primary">可用 Edictum 类型</h3>
            <p className="mt-1 text-sm text-text-secondary">类型由后端注册表提供, 配置写入不需要启动平台后端</p>
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={refresh} disabled={loading}>
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </Button>
            <Button variant="primary" onClick={openCreate} disabled={types.length === 0}>
              <Plus className="mr-2 h-4 w-4" />新建 Edictum 配置
            </Button>
          </div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {types.map((definition) => (
            <div key={definition.name} className="rounded-sm bg-glass p-3">
              <div className="flex items-center gap-2">
                <span className="font-mono font-medium text-text-primary">{definition.name}</span>
                <Badge variant="info">{definition.is_async ? 'async' : 'sync'}</Badge>
              </div>
              <p className="mt-2 text-sm text-text-secondary">{definition.description || '暂无描述'}</p>
              <div className="mt-3 flex flex-wrap gap-2 text-xs text-text-secondary">
                {definition.capabilities.plugins && <Badge variant="default">插件</Badge>}
                {definition.capabilities.mcp && <Badge variant="default">MCP</Badge>}
                {definition.capabilities.stream && <Badge variant="default">流式</Badge>}
              </div>
            </div>
          ))}
          {!loading && types.length === 0 && (
            <p className="text-sm text-text-secondary">没有可用的 Edictum 类型</p>
          )}
        </div>
      </Card>

      <DataTable
        columns={columns}
        data={tableData}
        keyExtractor={(item) => item.name}
        emptyMessage="暂无 Edictum 命名配置"
      />

      {runtimeResult && runtimeResult.edictum_sessions.length > 0 && (
        <Card>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-base font-semibold text-text-primary">插件运行时同步结果</h3>
              <p className="mt-1 text-sm text-text-secondary">
                展示每个活跃会话的实际应用状态, 回滚结果和残留漂移
              </p>
            </div>
            <Button variant="ghost" size="sm" onClick={retryRuntimeChanges} disabled={retryingRuntime}>
              <RefreshCw className={`mr-2 h-4 w-4 ${retryingRuntime ? 'animate-spin' : ''}`} />
              重试未同步会话
            </Button>
          </div>
          <div className="mt-4 max-h-72 space-y-3 overflow-y-auto pr-1">
            {runtimeResult.edictum_sessions.map((session) => (
              <div
                key={`${session.platform_id}:${session.session_id}`}
                className="rounded-sm bg-glass p-3 text-sm"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-text-primary">{session.platform_id}:{session.session_id}</span>
                  <Badge variant={session.ok ? 'success' : 'warning'}>
                    {session.ok ? '已同步' : session.restart_required ? '需重启' : '存在漂移'}
                  </Badge>
                  {(session.drift || 0) > 0 && <Badge variant="warning">漂移 {session.drift}</Badge>}
                </div>
                {session.error && <p className="mt-2 text-error">{session.error}</p>}
                {(session.plugins || []).map((plugin) => (
                  <div key={plugin.plugin} className="mt-2 text-text-secondary">
                    <span className="font-mono">{plugin.plugin}</span>
                    <span className="mx-2">·</span>
                    <span>{plugin.action || '同步'} / {plugin.status}</span>
                    {plugin.error && <span className="ml-2 text-error">{plugin.error}</span>}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Card>
      )}

      <FormModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title={editingName ? `编辑 Edictum 配置: ${editingName}` : '新建 Edictum 配置'}
        fields={fields}
        values={form}
        onChange={(key, value) => setForm((current) => ({ ...current, [key]: value }))}
        onSubmit={save}
        submitText={editingName ? '保存' : '创建'}
        loading={saving}
        size="lg"
      />

      <FormModal
        open={runtimeTargetName !== null}
        onClose={() => setRuntimeTargetName(null)}
        title={`选择运行平台: ${runtimeTargetName || ''}`}
        fields={[{
          key: 'platform_id',
          label: '目标平台',
          type: 'select',
          required: true,
          options: runtimePlatformOptions,
        }]}
        values={{ platform_id: runtimePlatformId }}
        onChange={(_key, value) => setRuntimePlatformId(String(value))}
        onSubmit={() => {
          if (!runtimeTargetName || !runtimePlatformId) return;
          const name = runtimeTargetName;
          const platformId = runtimePlatformId;
          setRuntimeTargetName(null);
          void createRuntime(name, platformId);
        }}
        submitText="创建并激活"
        loading={creatingName !== null}
      />

      <EdictumPluginManager
        open={pluginManagerName !== null}
        configName={pluginManagerName}
        availablePlugins={availablePlugins}
        configuredPlugins={pluginManagerName ? configs[pluginManagerName]?.plugins || [] : []}
        saving={pluginSaving}
        onClose={() => setPluginManagerName(null)}
        onSave={savePlugins}
      />
    </div>
  );
}
