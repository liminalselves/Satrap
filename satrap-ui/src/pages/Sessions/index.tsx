import { RagManager } from '@/components/common/RagManager';
import { Modal } from '@/components/ui/Modal';
import { SessionPluginSettingsModal } from '@/components/common/SessionPluginSettingsModal';
import { controlApi } from '@/api/control';
import { useEffect, useState, useCallback, useMemo } from 'react';
import { useConfigStore } from '@/stores/useConfigStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { toast } from '@/components/ui/Toast';
import { sessionApi } from '@/api/session';
import { edictumApi } from '@/api/edictum';
import { classNameToConfigName } from '@/utils/adminMigration';
import { PageHeader, DataTable, FormModal, ActionButtons } from '@/components/common';
import { Plus, Power, PowerOff, Trash2, Settings, Search, FolderPlus, Play, RefreshCw, RotateCcw } from 'lucide-react';
import type { Column, FormField } from '@/components/common';
import type { DiscoveredSessionClass, RuntimeSession, SessionClassConfig } from '@/api/types';
import { EdictumSessionsPanel } from './EdictumSessionsPanel';

interface SessionClassItem {
  name: string;
  config: SessionClassConfig;
}

const runtimeKey = (session: Pick<RuntimeSession, 'platform_id' | 'session_id'>) => (
  `${session.platform_id}\u0000${session.session_id}`
);
const PLATFORM_SESSION_EXCLUDED_IDS = new Set(['chat']);

export function Sessions() {
  const [ragSession, setRagSession] = useState<RuntimeSession | null>(null);
  const [overrideSession, setOverrideSession] = useState<RuntimeSession | null>(null);
  const [overridePlugins, setOverridePlugins] = useState<string[]>([]);
  useEffect(() => {
    if (!overrideSession) return;
    let cancelled = false;
    controlApi.listEdictumPlugins().then((plugins) => { if (!cancelled) setOverridePlugins(plugins.map((plugin) => plugin.name)); })
      .catch((error) => toast('error', error.message));
    return () => { cancelled = true; };
  }, [overrideSession]);

  const { sessionClasses, llmConfigs, fetchSessionClasses, fetchModels } = useConfigStore();
  const { health, isRunning } = useBackendStore();
  const [showRegisterModal, setShowRegisterModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedClass, setSelectedClass] = useState<string | null>(null);
  const [scanPaths, setScanPaths] = useState<string[]>([]);
  const [selectedScanPath, setSelectedScanPath] = useState('');
  const [discovered, setDiscovered] = useState<DiscoveredSessionClass[]>([]);
  const [runtimeSessions, setRuntimeSessions] = useState<RuntimeSession[]>([]);
  const [selectedRuntimeIds, setSelectedRuntimeIds] = useState<Set<string>>(() => new Set());
  const [scanning, setScanning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [retryingRuntimeKey, setRetryingRuntimeKey] = useState<string | null>(null);
  const [registerForm, setRegisterForm] = useState({
    name: '', class_path: '', is_async: false, description: '', context_key: '', model_key: 'model_name', model_name: '', params: '{}',
  });
  const [editForm, setEditForm] = useState({
    name: '', class_path: '', is_async: false, description: '', context_key: '', model_key: '', model_name: '', params: '{}',
  });
  const [createForm, setCreateForm] = useState({
    session_id: '', adapter_id: '', llm_name: '', params: '{}',
  });
  const backendRunning = isRunning;

  const fetchRuntimeSessions = useCallback(async () => {
    try {
      const result = await sessionApi.listRuntime(backendRunning);
      const platformSessions = result.sessions.filter(
        (session) => !PLATFORM_SESSION_EXCLUDED_IDS.has(session.platform_id),
      );
      setRuntimeSessions(platformSessions);
      const availableIds = new Set(platformSessions.map(runtimeKey));
      setSelectedRuntimeIds((current) => new Set(
        Array.from(current).filter((sessionId) => availableIds.has(sessionId)),
      ));
    } catch {
      setRuntimeSessions([]);
    }
  }, [backendRunning]);

  const retryRuntimePlugins = useCallback(async (session: RuntimeSession) => {
    const key = runtimeKey(session);
    setRetryingRuntimeKey(key);
    try {
      const requiresRestart = Boolean(session.runtime?.config?.drift)
        || (session.runtime?.plugin_summary?.restart_required || 0) > 0;
      const applied = requiresRestart
        ? await sessionApi.restartRuntime(session.platform_id, session.session_id)
        : (await edictumApi.retryRuntimeChanges({
          session_refs: [{ platform_id: session.platform_id, session_id: session.session_id }],
        })).edictum_sessions[0];
      toast(
        applied?.ok ? 'success' : 'warning',
        applied?.ok
          ? (requiresRestart ? '会话已按冷配置重新激活' : '插件状态已同步')
          : '插件仍存在漂移, 请查看错误详情',
      );
      await fetchRuntimeSessions();
    } catch (error) {
      toast('error', '插件重试失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setRetryingRuntimeKey(null);
    }
  }, [fetchRuntimeSessions]);

  useEffect(() => {
    fetchSessionClasses();
    fetchModels('llm');
    fetchRuntimeSessions();
  }, [fetchModels, fetchRuntimeSessions, fetchSessionClasses]);

  const tableData: SessionClassItem[] = useMemo(
    () => Object.entries(sessionClasses).map(([name, config]) => ({ name, config })),
    [sessionClasses],
  );
  const llmNames = useMemo(() => Object.keys(llmConfigs), [llmConfigs]);
  const adapterIds = useMemo(() => Object.keys(health?.adapters || {}), [health?.adapters]);

  const handleDiscover = useCallback(async () => {
    setScanning(true);
    try {
      const result = await sessionApi.discover(selectedScanPath || undefined);
      setScanPaths(result.paths);
      setSelectedScanPath((current) => current || result.paths[0] || '');
      setDiscovered(result.results);
    } catch (e) {
      toast('error', '扫描失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setScanning(false);
    }
  }, [selectedScanPath]);

  const handleCreateScanDirectory = useCallback(async () => {
    try {
      const result = await sessionApi.createScanDirectory(selectedScanPath || undefined);
      toast('success', `目录已创建: ${result.path}`);
      await handleDiscover();
    } catch (e) {
      toast('error', '创建目录失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [handleDiscover, selectedScanPath]);

  const openRegister = useCallback((item?: DiscoveredSessionClass) => {
    setRegisterForm({
      name: item ? classNameToConfigName(item.class_name) : '',
      class_path: item?.class_path || '',
      is_async: item?.is_async || false,
      description: '',
      context_key: '',
      model_key: 'model_name',
      model_name: llmNames[0] || '',
      params: JSON.stringify(item?.init_params || {}, null, 2),
    });
    setShowRegisterModal(true);
  }, [llmNames]);

  const handleRegister = useCallback(async () => {
    if (!registerForm.name.trim() || !registerForm.class_path.trim()) {
      toast('error', '名称和 Class Path 为必填项');
      return;
    }
    setSaving(true);
    try {
      const params = JSON.parse(registerForm.params) as unknown;
      if (typeof params !== 'object' || params === null || Array.isArray(params)) throw new Error('参数必须是 JSON 对象');
      const normalizedParams = params as Record<string, unknown>;
      if (registerForm.model_key && registerForm.model_name) normalizedParams[registerForm.model_key] = registerForm.model_name;
      await sessionApi.register({
        name: registerForm.name.trim(),
        class_path: registerForm.class_path.trim(),
        is_async: registerForm.is_async,
        description: registerForm.description,
        context_key: registerForm.context_key,
        model_key: registerForm.model_key,
        params: normalizedParams,
      });
      toast('success', '会话类已注册');
      setShowRegisterModal(false);
      await fetchSessionClasses();
    } catch (e) {
      toast('error', '注册失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setSaving(false);
    }
  }, [fetchSessionClasses, registerForm]);

  const handleEnable = useCallback(async (name: string) => {
    try {
      await sessionApi.enable(name);
      toast('success', `已启用 ${name}`);
      await fetchSessionClasses();
    } catch (e) {
      toast('error', '启用失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [fetchSessionClasses]);

  const handleDisable = useCallback(async (name: string) => {
    try {
      await sessionApi.disable(name);
      toast('success', `已禁用 ${name}`);
      await fetchSessionClasses();
    } catch (e) {
      toast('error', '禁用失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [fetchSessionClasses]);

  const handleUnregister = useCallback(async (name: string) => {
    if (!confirm(`确定要注销会话类 "${name}" 吗?`)) return;
    try {
      await sessionApi.unregister(name);
      toast('success', '已注销');
      await fetchSessionClasses();
    } catch (e) {
      toast('error', '注销失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [fetchSessionClasses]);

  const openEdit = useCallback((name: string) => {
    const config = sessionClasses[name];
    const params = { ...(config?.params || {}) };
    const modelKey = config?.model_key || '';
    setSelectedClass(name);
    setEditForm({
      name,
      class_path: config?.class_path || '',
      is_async: config?.is_async || false,
      description: config?.description || '',
      context_key: config?.context_key || '',
      model_key: modelKey,
      model_name: modelKey ? String(params[modelKey] || llmNames[0] || '') : '',
      params: JSON.stringify(params, null, 2),
    });
    setShowEditModal(true);
  }, [llmNames, sessionClasses]);

  const handleSaveEdit = useCallback(async () => {
    if (!selectedClass) return;
    if (!editForm.name.trim() || !editForm.class_path.trim()) {
      toast('error', '名称和 Class Path 为必填项');
      return;
    }
    setSaving(true);
    try {
      const params = JSON.parse(editForm.params) as unknown;
      if (typeof params !== 'object' || params === null || Array.isArray(params)) throw new Error('参数必须是 JSON 对象');
      const normalizedParams = params as Record<string, unknown>;
      if (editForm.model_key && editForm.model_name) normalizedParams[editForm.model_key] = editForm.model_name;
      await sessionApi.update(selectedClass, {
        name: editForm.name.trim(),
        class_path: editForm.class_path.trim(),
        is_async: editForm.is_async,
        params: normalizedParams,
        description: editForm.description,
        context_key: editForm.context_key,
        model_key: editForm.model_key,
      });
      toast('success', '会话类配置已保存');
      setShowEditModal(false);
      await fetchSessionClasses();
    } catch (e) {
      toast('error', '保存失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setSaving(false);
    }
  }, [editForm, fetchSessionClasses, selectedClass]);

  const openCreateSession = useCallback((name: string) => {
    const config = sessionClasses[name];
    const modelKey = config?.model_key || '';
    setSelectedClass(name);
    setCreateForm({
      session_id: '',
      adapter_id: '',
      llm_name: modelKey ? String(config?.params?.[modelKey] || llmNames[0] || '') : '',
      params: '{}',
    });
    setShowCreateModal(true);
  }, [llmNames, sessionClasses]);

  const handleCreateSession = useCallback(async () => {
    if (!selectedClass) return;
    setSaving(true);
    try {
      const params = JSON.parse(createForm.params) as unknown;
      if (typeof params !== 'object' || params === null || Array.isArray(params)) throw new Error('附加参数必须是 JSON 对象');
      const result = await sessionApi.createRuntime({
        session_provider: 'session_class',
        session_type: selectedClass,
        session_id: createForm.session_id || undefined,
        adapter_id: createForm.adapter_id || undefined,
        llm_name: createForm.llm_name || undefined,
        params: params as Record<string, unknown>,
      }, backendRunning);
      toast(
        'success',
        backendRunning
          ? `会话已创建: ${result.session.session_id}`
          : `持久化会话已冷创建: ${result.session.session_id}`,
      );
      setShowCreateModal(false);
      await fetchRuntimeSessions();
    } catch (e) {
      toast('error', '创建失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setSaving(false);
    }
  }, [backendRunning, createForm, fetchRuntimeSessions, selectedClass]);

  const toggleRuntimeSelection = useCallback((sessionId: string, checked: boolean) => {
    setSelectedRuntimeIds((current) => {
      const next = new Set(current);
      if (checked) next.add(sessionId);
      else next.delete(sessionId);
      return next;
    });
  }, []);

  const toggleAllRuntimeSelection = useCallback((checked: boolean) => {
    setSelectedRuntimeIds(checked
      ? new Set(runtimeSessions.map(runtimeKey))
      : new Set());
  }, [runtimeSessions]);

  const handleDeleteRuntime = useCallback(async (session: RuntimeSession) => {
    if (!confirm(`确定要删除 ${session.platform_id} 的会话实例 "${session.session_id}" 吗? 此操作会同时清理用户绑定和上下文路由`)) return;
    setDeleting(true);
    try {
      await sessionApi.deleteRuntime(session.platform_id, session.session_id, backendRunning);
      toast('success', `会话实例已删除: ${session.session_id}`);
      await fetchRuntimeSessions();
    } catch (error) {
      toast('error', '删除失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setDeleting(false);
    }
  }, [backendRunning, fetchRuntimeSessions]);

  const handleBulkDeleteRuntime = useCallback(async (mode: 'empty' | 'single' | 'selected') => {
    const selectedRefs = runtimeSessions
      .filter((session) => selectedRuntimeIds.has(runtimeKey(session)))
      .map((session) => ({ platform_id: session.platform_id, session_id: session.session_id }));
    if (mode === 'selected' && selectedRefs.length === 0) {
      toast('warning', '请先选择要删除的会话实例');
      return;
    }
    const description = mode === 'empty'
      ? '所有消息数为 0 的会话实例'
      : mode === 'single'
        ? '所有消息数为 1 的会话实例'
        : `选中的 ${selectedRefs.length} 个会话实例`;
    if (!confirm(`确定要删除${description}吗? 此操作会同时清理用户绑定和上下文路由`)) return;
    setDeleting(true);
    try {
      const result = await sessionApi.bulkDeleteRuntime({
        mode,
        session_refs: mode === 'selected' ? selectedRefs : undefined,
      }, backendRunning);
      toast('success', `已删除 ${result.deleted_count} 个会话实例`);
      setSelectedRuntimeIds(new Set());
      await fetchRuntimeSessions();
    } catch (error) {
      toast('error', '批量删除失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setDeleting(false);
    }
  }, [backendRunning, fetchRuntimeSessions, runtimeSessions, selectedRuntimeIds]);

  const columns: Column<SessionClassItem>[] = useMemo(() => [
    { key: 'name', title: '名称', render: (item) => <span className="font-medium">{item.name}</span> },
    { key: 'class_path', title: 'Class Path', render: (item) => <span className="font-mono text-sm text-text-secondary">{item.config.class_path}</span> },
    {
      key: 'enabled', title: '状态', render: (item) => (
        <Badge variant={item.config.enabled ? 'success' : 'default'}>{item.config.enabled ? '启用' : '禁用'}</Badge>
      ),
    },
    { key: 'model_key', title: '模型键', render: (item) => item.config.model_key || '-' },
    {
      key: 'actions', title: '操作', render: (item) => (
        <ActionButtons actions={[
          item.config.enabled
            ? { key: 'disable', icon: <PowerOff className="h-4 w-4" />, onClick: () => handleDisable(item.name), title: '禁用' }
            : { key: 'enable', icon: <Power className="h-4 w-4" />, onClick: () => handleEnable(item.name), title: '启用' },
          { key: 'create', icon: <Play className="h-4 w-4" />, onClick: () => openCreateSession(item.name), title: '创建会话' },
          { key: 'edit', icon: <Settings className="h-4 w-4" />, onClick: () => openEdit(item.name), title: '编辑配置' },
          { key: 'delete', icon: <Trash2 className="h-4 w-4" />, onClick: () => handleUnregister(item.name), title: '注销', className: 'text-error hover:text-error' },
        ]} />
      ),
    },
  ], [handleDisable, handleEnable, handleUnregister, openCreateSession, openEdit]);

  const allRuntimeSelected = runtimeSessions.length > 0
    && runtimeSessions.every((session) => selectedRuntimeIds.has(runtimeKey(session)));
  const runtimeColumns: Column<RuntimeSession>[] = useMemo(() => [
    {
      key: 'selection',
      title: (
        <input
          type="checkbox"
          aria-label="选择全部会话实例"
          checked={allRuntimeSelected}
          onChange={(event) => toggleAllRuntimeSelection(event.target.checked)}
          className="h-4 w-4 accent-accent"
        />
      ),
      render: (item) => (
        <input
          type="checkbox"
          aria-label={`选择会话实例 ${item.session_id}`}
          checked={selectedRuntimeIds.has(runtimeKey(item))}
          onChange={(event) => toggleRuntimeSelection(runtimeKey(item), event.target.checked)}
          className="h-4 w-4 accent-accent"
        />
      ),
      className: 'w-12',
    },
    { key: 'platform_id', title: '平台', render: (item) => item.platform_id },
    { key: 'session_id', title: 'Session ID', render: (item) => <span className="font-mono text-sm">{item.session_id}</span> },
    { key: 'provider_name', title: 'Provider', render: (item) => item.provider_name || 'session_class' },
    { key: 'session_type', title: '会话类', render: (item) => item.session_type || item.session_type_name || '-' },
    { key: 'active', title: '状态', render: (item) => <Badge variant={item.active ? 'success' : 'default'}>{item.active ? '活跃' : '已持久化'}</Badge> },
    {
      key: 'runtime_config',
      title: '配置状态',
      render: (item) => {
        if ((item.provider_name || 'session_class') !== 'edictum' || !item.active) return '-';
        const configState = item.runtime?.config;
        if (!configState) return <Badge variant="default">未知</Badge>;
        if (configState.status === 'error') {
          return <Badge variant="error" title={configState.error || ''}>重启失败</Badge>;
        }
        if (configState.drift || configState.status === 'restart_pending') {
          return <Badge variant="warning">等待热重启</Badge>;
        }
        return <Badge variant="success">已应用</Badge>;
      },
    },
    {
      key: 'plugins',
      title: '插件状态',
      render: (item) => {
        if ((item.provider_name || 'session_class') !== 'edictum') return '-';
        if (!item.active) return <Badge variant="default">未加载</Badge>;
        const summary = item.runtime?.plugin_summary;
        const plugins = item.runtime?.plugins || [];
        if (!summary || summary.total === 0) return <Badge variant="default">无插件</Badge>;
        const errors = plugins
          .filter((plugin) => plugin.status === 'error' || plugin.drift)
          .map((plugin) => `${plugin.name}: ${plugin.error || plugin.last_error || '目标状态与实际状态不一致'}`)
          .join('\n');
        if ((summary.restart_required || 0) > 0) {
          return <Badge variant="warning" title={errors}>需重启 {summary.restart_required}</Badge>;
        }
        if ((summary.drift || 0) > 0) {
          return <Badge variant="warning" title={errors}>漂移 {summary.drift}/{summary.total}</Badge>;
        }
        if (summary.errors > 0) {
          return <Badge variant="error" title={errors}>失败 {summary.errors}/{summary.total}</Badge>;
        }
        if (summary.pending > 0) {
          return <Badge variant="warning">待加载 {summary.pending}/{summary.total}</Badge>;
        }
        return <Badge variant="success">已加载 {summary.loaded}/{summary.total}</Badge>;
      },
    },
    { key: 'message_count', title: '消息数' },
    { key: 'last_used_at', title: '最近使用', render: (item) => new Date(item.last_used_at * 1000).toLocaleString() },
    {
      key: 'actions',
      title: '操作',
      render: (item) => {
        const canRetry = backendRunning
          && item.active
          && (item.provider_name || 'session_class') === 'edictum'
          && (
            Boolean(item.runtime?.config?.drift)
            || item.runtime?.config?.status === 'error'
            || (item.runtime?.plugin_summary?.drift || 0) > 0
            || (item.runtime?.plugin_summary?.errors || 0) > 0
          );
        return (
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="sm" onClick={() => setRagSession(item)}>知识库</Button>
            <Button variant="ghost" size="sm" title="会话插件参数" onClick={() => setOverrideSession(item)}><Settings className="h-4 w-4" /></Button>
            {canRetry && (
              <Button
                variant="ghost"
                size="sm"
                title={(item.runtime?.plugin_summary?.restart_required || 0) > 0 ? '按冷配置重新激活' : '重试插件同步'}
                aria-label={`重试会话插件 ${item.session_id}`}
                disabled={retryingRuntimeKey !== null}
                onClick={() => retryRuntimePlugins(item)}
              >
                <RotateCcw className={`h-4 w-4 ${retryingRuntimeKey === runtimeKey(item) ? 'animate-spin' : ''}`} />
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              title="删除会话实例"
              aria-label={`删除会话实例 ${item.session_id}`}
              disabled={deleting}
              onClick={() => handleDeleteRuntime(item)}
              className="text-error hover:text-error"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        );
      },
    },
  ], [allRuntimeSelected, backendRunning, deleting, handleDeleteRuntime, retryingRuntimeKey, retryRuntimePlugins, selectedRuntimeIds, toggleAllRuntimeSelection, toggleRuntimeSelection]);

  const modelOptions = useMemo(
    () => [{ value: '', label: '不绑定' }, ...llmNames.map((name) => ({ value: name, label: name }))],
    [llmNames],
  );
  const registerFields: FormField[] = useMemo(() => [
    { key: 'name', label: '名称', required: true },
    { key: 'class_path', label: 'Class Path', placeholder: '如: misskey_session.MisskeySession', required: true },
    { key: 'is_async', label: '异步会话类', type: 'checkbox', placeholder: '使用 AsyncSession' },
    { key: 'description', label: '描述（可选）' },
    { key: 'context_key', label: '上下文键（可选）' },
    { key: 'model_key', label: '模型键', placeholder: '如: model_name' },
    { key: 'model_name', label: '绑定 LLM', type: 'select', options: modelOptions },
    { key: 'params', label: '参数模板（JSON）', type: 'textarea', rows: 10 },
  ], [modelOptions]);
  const editFields: FormField[] = useMemo(() => [
    { key: 'name', label: '名称', required: true },
    { key: 'class_path', label: 'Class Path', required: true },
    { key: 'is_async', label: '异步会话类', type: 'checkbox', placeholder: '使用 AsyncSession' },
    { key: 'description', label: '描述（可选）' },
    { key: 'context_key', label: '上下文键（可选）' },
    { key: 'model_key', label: '模型键（可选）' },
    { key: 'model_name', label: '绑定 LLM', type: 'select', options: modelOptions },
    { key: 'params', label: '参数（JSON）', type: 'textarea', rows: 12 },
  ], [modelOptions]);
  const createFields: FormField[] = useMemo(() => [
    { key: 'session_id', label: 'Session ID（留空自动生成）' },
    {
      key: 'adapter_id', label: '适配器初始化参数', type: 'select',
      options: [{ value: '', label: '自动（按消息来源）' }, ...adapterIds.map((id) => ({ value: id, label: id }))],
    },
    { key: 'llm_name', label: 'LLM 模型', type: 'select', options: modelOptions },
    { key: 'params', label: '附加参数（JSON）', type: 'textarea', rows: 8 },
  ], [adapterIds, modelOptions]);

  return (
    <div className="space-y-6">
      <Modal open={ragSession !== null} title="会话知识库" onClose={() => setRagSession(null)} size="lg">
        {ragSession && <RagManager context={{ platformId: ragSession.platform_id, sessionId: ragSession.session_id, via: 'control' }} />}
      </Modal>
      <SessionPluginSettingsModal
        context={overrideSession ? { platformId: overrideSession.platform_id, sessionId: overrideSession.session_id } : null}
        plugins={overridePlugins}
        onClose={() => setOverrideSession(null)}
      />
      <PageHeader
        title="会话管理"
        description="管理扫描式会话类、Edictum 命名配置和运行时会话"
      />

      <Tabs defaultValue="session-classes">
        <TabsList>
          <TabsTrigger value="session-classes">会话类</TabsTrigger>
          <TabsTrigger value="edictum">Edictum 会话</TabsTrigger>
        </TabsList>

        <TabsContent value="session-classes" className="space-y-4">
          <Card>
            <div className="mb-4 flex flex-wrap items-end gap-3">
              <div className="min-w-64 flex-1">
                <label className="mb-1 block text-sm font-medium text-text-secondary">Session 扫描目录</label>
                {scanPaths.length > 0 ? (
                  <select className="glass-input w-full" value={selectedScanPath} onChange={(event) => setSelectedScanPath(event.target.value)}>
                    {scanPaths.map((path) => <option key={path} value={path}>{path}</option>)}
                  </select>
                ) : <div className="glass-input w-full text-text-secondary">点击扫描读取配置目录</div>}
              </div>
              <Button variant="default" onClick={() => openRegister()}><Plus className="h-4 w-4 mr-2" />手动注册</Button>
              <Button variant="default" onClick={handleCreateScanDirectory}><FolderPlus className="h-4 w-4 mr-2" />创建目录</Button>
              <Button variant="primary" onClick={handleDiscover} disabled={scanning}>
                <Search className={`h-4 w-4 mr-2 ${scanning ? 'animate-spin' : ''}`} />扫描
              </Button>
            </div>
            {discovered.length > 0 && (
              <div className="space-y-2">
                {discovered.map((item) => (
                  <div key={`${item.file_path}:${item.class_name || item.error}`} className="flex items-center justify-between gap-4 rounded-sm bg-glass p-3">
                    <div className="min-w-0">
                      {item.error ? <p className="text-sm text-error">{item.file_path}: {item.error}</p> : (
                        <><p className="font-medium text-text-primary">{item.class_name} <Badge variant="info">{item.is_async ? 'async' : 'sync'}</Badge></p><p className="truncate font-mono text-xs text-text-secondary">{item.class_path}</p></>
                      )}
                    </div>
                    {!item.error && <Button size="sm" onClick={() => openRegister(item)}>注册</Button>}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <DataTable columns={columns} data={tableData} keyExtractor={(item) => item.name} emptyMessage="暂无已注册的会话类" />
        </TabsContent>

        <TabsContent value="edictum">
          <EdictumSessionsPanel llmNames={llmNames} onRuntimeCreated={fetchRuntimeSessions} />
        </TabsContent>
      </Tabs>

      <Card>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-lg font-semibold text-text-primary">会话实例</h3>
            <p className="mt-1 text-sm text-text-secondary">
              已持久化的平台 Provider 会话实例, 不包含 Chat 对话历史 · {backendRunning ? '后端热管理' : '后端已停止, 当前为冷管理'}
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button variant="danger" size="sm" disabled={deleting} onClick={() => handleBulkDeleteRuntime('empty')}>删除无消息</Button>
            <Button variant="danger" size="sm" disabled={deleting} onClick={() => handleBulkDeleteRuntime('single')}>删除仅 1 条</Button>
            <Button variant="danger" size="sm" disabled={deleting || selectedRuntimeIds.size === 0} onClick={() => handleBulkDeleteRuntime('selected')}>
              删除所选{selectedRuntimeIds.size > 0 ? ` (${selectedRuntimeIds.size})` : ''}
            </Button>
            <Button variant="ghost" size="sm" onClick={fetchRuntimeSessions} disabled={deleting} title="刷新会话实例">
              <RefreshCw className="h-4 w-4" />
            </Button>
          </div>
        </div>
        <DataTable
          columns={runtimeColumns}
          data={runtimeSessions}
          keyExtractor={runtimeKey}
          emptyMessage="暂无会话实例"
          scrollClassName="h-[32rem] max-h-[55vh]"
          stickyHeader
        />
      </Card>

      <FormModal open={showRegisterModal} onClose={() => setShowRegisterModal(false)} title="注册新会话类" fields={registerFields} values={registerForm} onChange={(key, value) => setRegisterForm((current) => ({ ...current, [key]: value }))} onSubmit={handleRegister} submitText="注册" loading={saving} size="lg" />
      <FormModal open={showEditModal} onClose={() => setShowEditModal(false)} title={`编辑会话类: ${selectedClass || ''}`} fields={editFields} values={editForm} onChange={(key, value) => setEditForm((current) => ({ ...current, [key]: value }))} onSubmit={handleSaveEdit} submitText="保存" loading={saving} size="lg" />
      <FormModal open={showCreateModal} onClose={() => setShowCreateModal(false)} title={`创建会话: ${selectedClass || ''}`} fields={createFields} values={createForm} onChange={(key, value) => setCreateForm((current) => ({ ...current, [key]: value }))} onSubmit={handleCreateSession} submitText="创建" loading={saving} size="lg" />
    </div>
  );
}
