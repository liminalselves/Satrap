import { useEffect, useState, useCallback, useMemo } from 'react';
import { useConfigStore } from '@/stores/useConfigStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Card } from '@/components/ui/Card';
import { toast } from '@/components/ui/Toast';
import { sessionApi } from '@/api/session';
import { classNameToConfigName } from '@/utils/adminMigration';
import { PageHeader, DataTable, FormModal, ActionButtons } from '@/components/common';
import { Plus, Power, PowerOff, Trash2, Settings, Search, FolderPlus, Play, RefreshCw } from 'lucide-react';
import type { Column, FormField } from '@/components/common';
import type { DiscoveredSessionClass, RuntimeSession, SessionClassConfig } from '@/api/types';

interface SessionClassItem {
  name: string;
  config: SessionClassConfig;
}

export function Sessions() {
  const { sessionClasses, llmConfigs, fetchSessionClasses, fetchModels } = useConfigStore();
  const { health } = useBackendStore();
  const [showRegisterModal, setShowRegisterModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedClass, setSelectedClass] = useState<string | null>(null);
  const [scanPaths, setScanPaths] = useState<string[]>([]);
  const [selectedScanPath, setSelectedScanPath] = useState('');
  const [discovered, setDiscovered] = useState<DiscoveredSessionClass[]>([]);
  const [runtimeSessions, setRuntimeSessions] = useState<RuntimeSession[]>([]);
  const [scanning, setScanning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [registerForm, setRegisterForm] = useState({
    name: '', class_path: '', description: '', context_key: '', model_key: 'model_name', model_name: '', params: '{}',
  });
  const [editForm, setEditForm] = useState({
    description: '', context_key: '', model_key: '', model_name: '', params: '{}',
  });
  const [createForm, setCreateForm] = useState({
    session_id: '', adapter_id: '', llm_name: '', params: '{}',
  });

  const fetchRuntimeSessions = useCallback(async () => {
    try {
      const result = await sessionApi.listRuntime();
      setRuntimeSessions(result.sessions);
    } catch {
      setRuntimeSessions([]);
    }
  }, []);

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
    setSaving(true);
    try {
      const params = JSON.parse(editForm.params) as unknown;
      if (typeof params !== 'object' || params === null || Array.isArray(params)) throw new Error('参数必须是 JSON 对象');
      const normalizedParams = params as Record<string, unknown>;
      if (editForm.model_key && editForm.model_name) normalizedParams[editForm.model_key] = editForm.model_name;
      await sessionApi.update(selectedClass, {
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
        class_name: selectedClass,
        session_id: createForm.session_id || undefined,
        adapter_id: createForm.adapter_id || undefined,
        llm_name: createForm.llm_name || undefined,
        params: params as Record<string, unknown>,
      });
      toast('success', `会话已创建: ${result.session.session_id}`);
      setShowCreateModal(false);
      await fetchRuntimeSessions();
    } catch (e) {
      toast('error', '创建失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setSaving(false);
    }
  }, [createForm, fetchRuntimeSessions, selectedClass]);

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

  const runtimeColumns: Column<RuntimeSession>[] = useMemo(() => [
    { key: 'session_id', title: 'Session ID', render: (item) => <span className="font-mono text-sm">{item.session_id}</span> },
    { key: 'session_type', title: '会话类', render: (item) => item.session_type || item.session_type_name || '-' },
    { key: 'active', title: '状态', render: (item) => <Badge variant={item.active ? 'success' : 'default'}>{item.active ? '活跃' : '已持久化'}</Badge> },
    { key: 'message_count', title: '消息数' },
    { key: 'last_used_at', title: '最近使用', render: (item) => new Date(item.last_used_at * 1000).toLocaleString() },
  ], []);

  const modelOptions = useMemo(
    () => [{ value: '', label: '不绑定' }, ...llmNames.map((name) => ({ value: name, label: name }))],
    [llmNames],
  );
  const registerFields: FormField[] = useMemo(() => [
    { key: 'name', label: '名称', required: true },
    { key: 'class_path', label: 'Class Path', placeholder: '如: misskey_session.MisskeySession', required: true },
    { key: 'description', label: '描述（可选）' },
    { key: 'context_key', label: '上下文键（可选）' },
    { key: 'model_key', label: '模型键', placeholder: '如: model_name' },
    { key: 'model_name', label: '绑定 LLM', type: 'select', options: modelOptions },
    { key: 'params', label: '参数模板（JSON）', type: 'textarea', rows: 10 },
  ], [modelOptions]);
  const editFields: FormField[] = useMemo(() => [
    { key: 'description', label: '描述（可选）' },
    { key: 'context_key', label: '上下文键（可选）' },
    { key: 'model_key', label: '模型键（可选）' },
    { key: 'model_name', label: '绑定 LLM', type: 'select', options: modelOptions },
    { key: 'params', label: '参数（JSON）', type: 'textarea', rows: 12 },
  ], [modelOptions]);
  const createFields: FormField[] = useMemo(() => [
    { key: 'session_id', label: 'Session ID（留空自动生成）' },
    {
      key: 'adapter_id', label: '绑定适配器', type: 'select',
      options: [{ value: '', label: '自动（按消息来源）' }, ...adapterIds.map((id) => ({ value: id, label: id }))],
    },
    { key: 'llm_name', label: 'LLM 模型', type: 'select', options: modelOptions },
    { key: 'params', label: '附加参数（JSON）', type: 'textarea', rows: 8 },
  ], [adapterIds, modelOptions]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="会话管理"
        description="发现、注册和配置会话类, 并创建运行时会话"
        actions={<Button variant="primary" onClick={() => openRegister()}><Plus className="h-4 w-4 mr-2" />手动注册</Button>}
      />

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
      <Card>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-text-primary">会话实例</h3>
          <Button variant="ghost" size="sm" onClick={fetchRuntimeSessions}><RefreshCw className="h-4 w-4" /></Button>
        </div>
        <DataTable columns={runtimeColumns} data={runtimeSessions} keyExtractor={(item) => item.session_id} emptyMessage="暂无会话实例" />
      </Card>

      <FormModal open={showRegisterModal} onClose={() => setShowRegisterModal(false)} title="注册新会话类" fields={registerFields} values={registerForm} onChange={(key, value) => setRegisterForm((current) => ({ ...current, [key]: value }))} onSubmit={handleRegister} submitText="注册" loading={saving} size="lg" />
      <FormModal open={showEditModal} onClose={() => setShowEditModal(false)} title={`编辑会话类: ${selectedClass || ''}`} fields={editFields} values={editForm} onChange={(key, value) => setEditForm((current) => ({ ...current, [key]: value }))} onSubmit={handleSaveEdit} submitText="保存" loading={saving} size="lg" />
      <FormModal open={showCreateModal} onClose={() => setShowCreateModal(false)} title={`创建会话: ${selectedClass || ''}`} fields={createFields} values={createForm} onChange={(key, value) => setCreateForm((current) => ({ ...current, [key]: value }))} onSubmit={handleCreateSession} submitText="创建" loading={saving} size="lg" />
    </div>
  );
}
