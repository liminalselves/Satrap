import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  Activity, ArrowUpRight, Bot, Code2, Cpu, FolderPlus, LayoutGrid, List, Plus, Puzzle, RefreshCw,
  Search, Settings, ShieldCheck, Trash2, X,
} from 'lucide-react';
import { Badge } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { Card } from '@/components/kit/Card';
import { Field, Input, Textarea } from '@/components/kit/Field';
import { CountUp, EmptyState, PageHeader, Skeleton } from '@/components/kit/Feedback';
import { Modal } from '@/components/kit/Modal';
import { toast } from '@/components/kit/Toast';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { useConfigWatch } from '@/hooks/useConfigWatch';
import { useConfigStore } from '@/stores/useConfigStore';
import { useBackendStore } from '@/stores/useBackendStore';
import { useShallow } from 'zustand/react/shallow';
import { sessionApi } from '@/api/session';
import { edictumApi } from '@/api/edictum';
import { controlApi } from '@/api/control';
import { createEdictumApplication, type ConfigReloadResult } from '@/api/backend';
import { ApplicationUnconfirmedError } from '@/api/runtimeApplication';
import { useRuntimeApplication } from '@/hooks/useRuntimeApplication';
import { RuntimeApplicationProgress } from '@/components/common/RuntimeApplicationProgress';
import { SessionEnabledToggle } from '@/components/common/SessionEnabledToggle';
import { classNameToConfigName } from '@/utils/adminMigration';
import { cn } from '@/utils/cn';
import type {
  DiscoveredSessionClass, EdictumAvailablePlugin, EdictumSessionConfig, EdictumTypeDefinition, PlatformConfig,
} from '@/api/types';
import { AgentEditor, type AgentSaveInput } from './AgentEditor';
import { InstancesPanel } from './InstancesPanel';
import { PluginManager } from './PluginManager';
import {
  edictumEntry, errorMessage, providerLabel, providerTone, relativeTime, sessionClassEntry, sumKnown,
  type AgentEntry, type Provider,
} from './shared';
import { Select as KitSelect } from '@/components/kit/Select';

type Tab = 'session' | 'edictum';
type View = 'grid' | 'list';

interface EditorState {
  mode: 'create' | 'edit';
  provider: Provider;
  initial: AgentEntry | null;
}

const blankAgent = (provider: Provider, typeName = ''): AgentEntry => ({
  name: '',
  provider,
  enabled: true,
  description: '',
  model_name: '',
  edictum_type: typeName,
  params: {},
  plugins: [],
  activeInstances: 0,
  runningInstances: 0,
  updatedAt: null,
});

const kindLabel = (agent: AgentEntry, types: EdictumTypeDefinition[]): string => {
  if (agent.provider !== 'edictum') return agent.is_async ? '会话类 · async' : '会话类 · sync';
  const type = types.find((item) => item.name === agent.edictum_type);
  return `${agent.edictum_type || 'edictum'}${type ? ` · ${type.is_async ? 'async' : 'sync'}` : ''}`;
};

const enabledPluginCount = (agent: AgentEntry): number => agent.plugins.filter(
  (plugin) => typeof plugin === 'string' || plugin.enabled !== false,
).length;

interface CreateSessionInput {
  session_id: string;
  adapter_id: string;
  llm_name: string;
  params: Record<string, unknown>;
}

export function Sessions({ view = 'agents' }: { view?: 'agents' | 'instances' }) {
  if (view === 'instances') return <InstancesPanel />;
  return <AgentsPage />;
}

function AgentsPage() {
  useConfigWatch(['sessions', 'edictum', 'llm']);
  const [searchParams] = useSearchParams();
  const linkedName = searchParams.get('edictum');
  const openedLink = useRef<string | null>(null);

  const {
    sessionClasses, edictumConfigs, llmConfigs, fetchSessionClasses, fetchEdictumConfigs, acceptEdictumConfig,
  } = useConfigStore(useShallow((state) => ({
    sessionClasses: state.sessionClasses,
    edictumConfigs: state.edictumConfigs,
    llmConfigs: state.llmConfigs,
    fetchSessionClasses: state.fetchSessionClasses,
    fetchEdictumConfigs: state.fetchEdictumConfigs,
    acceptEdictumConfig: state.acceptEdictumConfig,
  })));
  const { isRunning, health, reloadConfig } = useBackendStore(useShallow((state) => ({
    isRunning: state.isRunning, health: state.health, reloadConfig: state.reloadConfig,
  })));

  const [booting, setBooting] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [tab, setTab] = useState<Tab>(() => (searchParams.get('edictum') ? 'edictum' : 'session'));
  const [view, setView] = useState<View>('grid');
  const [query, setQuery] = useState('');
  const [typesOpen, setTypesOpen] = useState(true);

  const [types, setTypes] = useState<EdictumTypeDefinition[]>([]);
  const [availablePlugins, setAvailablePlugins] = useState<EdictumAvailablePlugin[]>([]);
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);

  const [editor, setEditor] = useState<EditorState | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [pluginTarget, setPluginTarget] = useState<string | null>(null);
  const [pluginSaving, setPluginSaving] = useState(false);
  const [pluginProgress, setPluginProgress] = useState('');
  const [createTarget, setCreateTarget] = useState<AgentEntry | null>(null);
  const [platformChoose, setPlatformChoose] = useState<AgentEntry | null>(null);

  const [scanning, setScanning] = useState(false);
  const [scanPaths, setScanPaths] = useState<string[]>([]);
  const [selectedScanPath, setSelectedScanPath] = useState('');
  const [discovered, setDiscovered] = useState<DiscoveredSessionClass[]>([]);

  const [confirmPrompt, setConfirmPrompt] = useState<{ message: string; resolve: (value: boolean) => void } | null>(null);
  const [runtimeResult, setRuntimeResult] = useState<ConfigReloadResult | null>(null);
  const [runtimeApplyKind, setRuntimeApplyKind] = useState<'plugins' | 'full'>('plugins');
  const [retryingRuntime, setRetryingRuntime] = useState(false);
  const [fullApplyLabel, setFullApplyLabel] = useState<string | null>(null);
  const application = useRuntimeApplication<ConfigReloadResult>();

  const askConfirm = useCallback((message: string) => new Promise<boolean>((resolve) => {
    setConfirmPrompt({ message, resolve });
  }), []);

  const settleConfirm = useCallback((value: boolean) => {
    setConfirmPrompt((current) => {
      current?.resolve(value);
      return null;
    });
  }, []);

  const applyRuntime = useCallback((operation: ReturnType<typeof createEdictumApplication>, kind: 'plugins' | 'full') => {
    setRuntimeResult(null);
    setRuntimeApplyKind(kind);
    return application.apply(operation, kind);
  }, [application]);

  useEffect(() => {
    if (application.view?.result) {
      setRuntimeResult(application.view.result);
      setRuntimeApplyKind(application.view.kind);
    }
  }, [application.view?.result, application.view?.kind]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const [availableTypes, plugins, platformResult] = await Promise.all([
        edictumApi.listTypes(),
        edictumApi.listPlugins(),
        controlApi.listPlatforms(),
      ]);
      setTypes(availableTypes);
      setAvailablePlugins(plugins);
      setPlatforms(platformResult.platforms || []);
      await Promise.all([fetchSessionClasses(), fetchEdictumConfigs()]);
    } catch (error) {
      toast.error('读取 Agent 配置失败', errorMessage(error));
    } finally {
      setRefreshing(false);
      setBooting(false);
    }
  }, [fetchEdictumConfigs, fetchSessionClasses]);

  useEffect(() => { void refresh(); }, [refresh]);

  const agents = useMemo<AgentEntry[]>(() => [
    ...Object.entries(sessionClasses).map(([name, config]) => sessionClassEntry(name, config)),
    ...Object.entries(edictumConfigs).map(([name, config]) => edictumEntry(name, config)),
  ], [sessionClasses, edictumConfigs]);

  const sessionAgents = useMemo(() => agents.filter((agent) => agent.provider === 'session_class'), [agents]);
  const edictumAgents = useMemo(() => agents.filter((agent) => agent.provider === 'edictum'), [agents]);
  const sourceList = tab === 'session' ? sessionAgents : edictumAgents;
  const visible = useMemo(() => {
    const text = query.trim().toLowerCase();
    if (!text) return sourceList;
    return sourceList.filter((agent) => `${agent.name} ${agent.description} ${agent.model_name} ${agent.edictum_type ?? ''} ${agent.class_path ?? ''}`
      .toLowerCase().includes(text));
  }, [sourceList, query]);

  const activeInstances = sumKnown(agents.map((agent) => agent.activeInstances));
  const configuredPlugins = agents.reduce(
    (total, agent) => total + (agent.provider === 'edictum' ? enabledPluginCount(agent) : 0), 0,
  );

  const pluginAgent = pluginTarget ? agents.find((agent) => agent.name === pluginTarget) ?? null : null;
  const editorPlugins = editor
    ? editor.mode === 'edit'
      ? (editor.provider === 'edictum' ? edictumConfigs[editor.initial?.name ?? '']?.plugins ?? [] : [])
      : (editor.initial?.plugins ?? [])
    : [];

  useEffect(() => {
    if (!linkedName || booting || openedLink.current === linkedName) return;
    if (!edictumConfigs[linkedName]) return;
    openedLink.current = linkedName;
    setPluginTarget(linkedName);
  }, [linkedName, booting, edictumConfigs]);

  const openEdit = useCallback((agent: AgentEntry) => {
    setFullApplyLabel(null);
    setEditor({ mode: 'edit', provider: agent.provider, initial: agent });
  }, []);

  const openCreate = useCallback((provider: Provider, typeName = '') => {
    setFullApplyLabel(null);
    if (provider === 'edictum') {
      const target = typeName || types[0]?.name || '';
      const debugPlugin = ['simple', 'async_simple'].includes(target)
        && availablePlugins.some((plugin) => plugin.name === 'debug_logger');
      setEditor({
        mode: 'create',
        provider,
        initial: {
          ...blankAgent('edictum', target),
          plugins: debugPlugin ? [{ name: 'debug_logger', enabled: true, config: {}, capabilities: {} }] : [],
        },
      });
      return;
    }
    setEditor({
      mode: 'create',
      provider,
      initial: {
        ...blankAgent('session_class'),
        model_name: Object.keys(llmConfigs)[0] ?? '',
        model_key: 'model_name',
      },
    });
  }, [availablePlugins, llmConfigs, types]);

  const registerDiscovered = useCallback((item: DiscoveredSessionClass) => {
    setEditor({
      mode: 'create',
      provider: 'session_class',
      initial: {
        ...blankAgent('session_class'),
        name: classNameToConfigName(item.class_name || 'session'),
        class_path: item.class_path,
        is_async: item.is_async,
        params: { ...item.init_params },
        description: `从 ${item.file_path.split('/').pop() ?? item.file_path} 扫描到的会话类`,
        model_key: 'model_name',
        model_name: Object.keys(llmConfigs)[0] ?? '',
      },
    });
  }, [llmConfigs]);

  const runScan = useCallback(async (path?: string) => {
    setScanning(true);
    try {
      const result = await sessionApi.discover(path || undefined);
      setScanPaths(result.paths);
      setSelectedScanPath((current) => current || result.paths[0] || '');
      setDiscovered(result.results);
      toast.success('扫描完成', `${result.results.filter((item) => !item.error).length} 个可注册会话类`);
    } catch (error) {
      toast.error('扫描失败', errorMessage(error));
    } finally {
      setScanning(false);
    }
  }, []);

  const createScanDirectory = useCallback(async () => {
    try {
      const result = await sessionApi.createScanDirectory(selectedScanPath || undefined);
      setScanPaths((current) => (current.includes(result.path) ? current : [...current, result.path]));
      setSelectedScanPath(result.path);
      toast.success('目录已创建', result.path);
      await runScan(result.path);
    } catch (error) {
      toast.error('创建目录失败', errorMessage(error));
    }
  }, [runScan, selectedScanPath]);

  const commitEdictum = useCallback(async (input: AgentSaveInput, editingName: string | null): Promise<boolean> => {
    const payload = {
      name: input.name,
      edictum_type: input.edictum_type ?? '',
      enabled: input.enabled,
      description: input.description,
      model_name: input.model_name,
      params: input.params,
      plugins: input.plugins,
    };
    let saved = false;
    try {
      if (isRunning && editingName) {
        const preview = await edictumApi.previewFullRuntimeChanges(editingName, payload);
        const affected = preview.edictum_sessions.filter((item) => item.action !== 'noop');
        if (affected.length > 0 && !(await askConfirm(`本次配置将立即协调 ${affected.length} 个活跃会话, 是否继续?`))) {
          return false;
        }
      }
      const updated = editingName ? await edictumApi.update(editingName, payload) : await edictumApi.create(payload);
      saved = true;
      const finalName = updated.name || input.name;
      acceptEdictumConfig(finalName, updated.config || await edictumApi.get(finalName), editingName || undefined);
      setFullApplyLabel(finalName);
      const result = isRunning
        ? await applyRuntime(createEdictumApplication('full', { config_name: finalName }), 'full')
        : null;
      if (result) {
        setRuntimeResult(result);
        setRuntimeApplyKind('full');
      }
      const reloaded = !result || result.ok;
      if (reloaded) toast.success(editingName ? 'Edictum 配置已保存' : 'Edictum 配置已创建');
      else toast.warning('Edictum 配置已保存, 但后端热加载失败');
      return true;
    } catch (error) {
      if (error instanceof ApplicationUnconfirmedError && error.code === 'query_stopped') return false;
      if (saved) toast.warning('Edictum 配置已保存, 运行时应用未确认', errorMessage(error));
      else toast.error('保存失败', errorMessage(error));
      return false;
    }
  }, [acceptEdictumConfig, applyRuntime, askConfirm, isRunning]);

  const commitSessionClass = useCallback(async (input: AgentSaveInput, editingName: string | null): Promise<boolean> => {
    try {
      const payload = {
        name: input.name,
        class_path: input.class_path ?? '',
        is_async: input.is_async ?? false,
        description: input.description,
        context_key: input.context_key ?? '',
        model_key: input.model_key ?? '',
        params: input.params,
      };
      if (editingName) await sessionApi.update(editingName, payload);
      else await sessionApi.register(payload);
      await fetchSessionClasses();
      toast.success(editingName ? '会话类配置已保存' : '会话类已注册');
      return true;
    } catch (error) {
      toast.error('保存失败', errorMessage(error));
      return false;
    }
  }, [fetchSessionClasses]);

  const handleSaveAgent = useCallback(async (input: AgentSaveInput) => {
    if (!editor) return;
    setSaving(true);
    try {
      const editingName = editor.mode === 'edit' ? editor.initial?.name ?? null : null;
      const ok = input.provider === 'edictum'
        ? await commitEdictum(input, editingName)
        : await commitSessionClass(input, editingName);
      if (ok) setEditor(null);
    } finally {
      setSaving(false);
    }
  }, [commitEdictum, commitSessionClass, editor]);

  const askDelete = useCallback(async (agent: AgentEntry) => {
    const message = agent.provider === 'edictum'
      ? `确定要删除 Edictum 配置 "${agent.name}" 吗? 已绑定它的平台将无法再创建新会话.`
      : `确定要注销会话类 "${agent.name}" 吗? 已创建的运行时实例会保留, 但无法再创建新实例.`;
    if (!(await askConfirm(message))) return;
    setDeleting(true);
    try {
      if (agent.provider === 'edictum') {
        await edictumApi.remove(agent.name);
        const reloaded = !isRunning || await reloadConfig();
        await fetchEdictumConfigs();
        if (reloaded) toast.success('Edictum 配置已删除');
        else toast.warning('Edictum 配置已删除, 但后端热加载失败');
      } else {
        await sessionApi.unregister(agent.name);
        await fetchSessionClasses();
        toast.success('会话类已注销');
      }
    } catch (error) {
      toast.error('删除失败', errorMessage(error));
    } finally {
      setDeleting(false);
    }
  }, [askConfirm, fetchEdictumConfigs, fetchSessionClasses, isRunning, reloadConfig]);

  const openCreateSession = useCallback((agent: AgentEntry) => {
    if (agent.provider === 'edictum' && !agent.enabled) {
      toast.warning('配置已停用', '请先启用此 Edictum 配置后再创建会话');
      return;
    }
    if (agent.provider === 'edictum') {
      const bound = platforms.filter((platform) => platform.session_provider === 'edictum' && platform.session_type === agent.name);
      if (bound.length > 1) { setPlatformChoose(agent); return; }
    }
    setCreateTarget(agent);
  }, [platforms]);

  const submitCreateSession = useCallback(async (agent: AgentEntry, data: CreateSessionInput, platformId?: string) => {
    try {
      if (agent.provider === 'edictum') {
        const result = await sessionApi.createRuntime({
          session_provider: 'edictum',
          session_type: agent.name,
          platform_id: platformId,
          adapter_id: platformId,
          activate: isRunning,
        }, isRunning);
        const message = isRunning ? 'Edictum 会话已创建并激活' : 'Edictum 会话已冷创建';
        toast.success(message, result.session.session_id);
      } else {
        const result = await sessionApi.createRuntime({
          session_provider: 'session_class',
          session_type: agent.name,
          session_id: data.session_id || undefined,
          adapter_id: data.adapter_id || undefined,
          llm_name: data.llm_name || undefined,
          params: data.params,
        }, isRunning);
        toast.success(isRunning ? '会话已创建' : '持久化会话已冷创建', result.session.session_id);
      }
      setCreateTarget(null);
      await Promise.all([fetchSessionClasses(), fetchEdictumConfigs()]);
    } catch (error) {
      toast.error('创建运行时会话失败', errorMessage(error));
    }
  }, [fetchEdictumConfigs, fetchSessionClasses, isRunning]);

  const handleAgentChanged = useCallback(async () => {
    await Promise.all([fetchSessionClasses(), fetchEdictumConfigs()]);
  }, [fetchEdictumConfigs, fetchSessionClasses]);

  const handlePluginSave = useCallback(async (plugins: EdictumSessionConfig['plugins']) => {
    if (!pluginAgent) return;
    if (application.view?.busy) { toast.warning('正在查询或应用配置, 请等待本次结果'); return; }
    setPluginSaving(true);
    let saved = false;
    try {
      if (isRunning) {
        setPluginProgress('正在核对活跃会话');
        const preview = await edictumApi.previewRuntimeChanges(pluginAgent.name, plugins);
        const affected = preview.edictum_sessions.filter(
          (item) => item.config_name === pluginAgent.name && (item.plugins?.length || 0) > 0,
        );
        const changes = affected.reduce((total, item) => total + (item.plugins?.length || 0), 0);
        if (changes > 0 && !(await askConfirm(`本次插件配置将影响 ${affected.length} 个活跃会话, 共 ${changes} 项运行时变更, 是否继续?`))) return;
      }
      setPluginProgress('正在保存插件配置');
      const updated = await edictumApi.update(pluginAgent.name, { plugins });
      saved = true;
      acceptEdictumConfig(updated.name || pluginAgent.name, updated.config || await edictumApi.get(pluginAgent.name));
      if (isRunning) {
        setPluginProgress('配置已保存, 正在应用到该 Agent 的活跃会话');
        const result = await applyRuntime(createEdictumApplication('plugins', { config_name: pluginAgent.name }), 'plugins');
        setRuntimeResult(result);
        setRuntimeApplyKind('plugins');
        const affected = result.edictum_sessions.filter((item) => item.config_name === pluginAgent.name);
        const failed = affected.filter((item) => !item.ok);
        if (failed.length === 0) toast.success('Edictum 插件配置已保存', `已同步 ${affected.length} 个活跃会话`);
        else toast.warning('插件配置已保存', `${failed.length}/${affected.length} 个活跃会话同步失败`);
      } else {
        toast.success('Edictum 插件配置已保存, 将在会话激活时生效');
      }
      setPluginTarget(null);
    } catch (error) {
      if (error instanceof ApplicationUnconfirmedError && error.code === 'query_stopped') return;
      if (saved) toast.warning('插件配置已保存, 运行时应用未确认', errorMessage(error));
      else toast.error('插件配置保存失败', errorMessage(error));
    } finally {
      setPluginSaving(false);
      setPluginProgress('');
    }
  }, [acceptEdictumConfig, application.view?.busy, applyRuntime, askConfirm, isRunning, pluginAgent]);

  const retryRuntimeChanges = useCallback(async () => {
    const failedSessions = runtimeResult?.edictum_sessions.filter((item) => !item.ok) || [];
    if (!failedSessions.length) return;
    setRetryingRuntime(true);
    try {
      const sessionRefs = failedSessions.map((item) => ({ platform_id: item.platform_id, session_id: item.session_id }));
      const result = await applyRuntime(createEdictumApplication(runtimeApplyKind, { session_refs: sessionRefs }), runtimeApplyKind);
      setRuntimeResult(result);
      const failed = result.edictum_sessions.filter((item) => !item.ok).length;
      if (failed === 0) toast.success(runtimeApplyKind === 'full' ? '配置同步重试完成' : '插件漂移重试完成');
      else toast.warning(`仍有 ${failed} 个会话未同步`);
    } catch (error) {
      if (error instanceof ApplicationUnconfirmedError && error.code === 'query_stopped') return;
      toast.error('重试失败', errorMessage(error));
    } finally {
      setRetryingRuntime(false);
    }
  }, [applyRuntime, runtimeApplyKind, runtimeResult]);

  const counts = { session: sessionAgents.length, edictum: edictumAgents.length };
  const hasFilter = query.trim().length > 0;
  const progressCard = application.view ? (
    <RuntimeApplicationProgress
      id={application.view.application.id}
      label={application.view.application.label}
      snapshot={application.view.snapshot}
      resultOk={application.view.result?.ok}
      busy={application.view.busy}
      error={application.view.error}
      onQuery={() => void application.query()}
    />
  ) : null;
  const failedRuntimeSessions = runtimeResult?.edictum_sessions.filter((item) => !item.ok) ?? [];

  return (
    <div className="space-y-5 pb-6">
      <PageHeader
        title="Agent 配置"
        description="配置 Agent 的会话类模板、Edictum 执行流程、模型与插件"
        actions={<>
          <Link to="/conversations/instances" className="ctl ctl-ghost ctl-sm text-fg-2">查看已创建实例<ArrowUpRight className="h-3.5 w-3.5" /></Link>
          <Button size="sm" icon={<RefreshCw className={cn('h-4 w-4', refreshing && 'animate-spin-slow')} />} onClick={() => void refresh()} disabled={refreshing} title="刷新 Agent 配置">刷新</Button>
          <Button size="sm" variant="tinted" tint="teal" icon={<Plus className="h-4 w-4" />} onClick={() => openCreate(tab === 'session' ? 'session_class' : 'edictum')}>
            {tab === 'session' ? '手动注册' : '新建 Edictum 配置'}
          </Button>
        </>}
      />

      <Card tint="teal" padding="md" className="animate-rise" style={{ animationDelay: '40ms' }}>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
            {[
              { label: '全部配置', value: agents.length },
              { label: '会话类模板', value: counts.session },
              { label: 'Edictum 流程', value: counts.edictum },
              { label: '活跃实例', value: activeInstances },
              { label: '启用插件', value: configuredPlugins },
            ].map((stat) => (
              <div key={stat.label}>
                <div className="text-[11px] text-fg-4">{stat.label}</div>
                <div className="display mt-0.5 text-[20px] font-semibold leading-none text-fg">
                  {stat.value === null ? '—' : <CountUp value={stat.value} />}
                </div>
              </div>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <Badge tone={isRunning ? 'green' : 'orange'} dot live={isRunning}>{isRunning ? '后端热管理' : '冷管理'}</Badge>
            <span className="text-[11px] text-fg-4">{isRunning ? '配置保存后立即协调活跃会话' : '后端已停止, 配置将在启动后应用'}</span>
          </div>
        </div>
      </Card>

      {progressCard}

      {runtimeResult && runtimeResult.edictum_sessions.length > 0 && (
        <Card padding="md" className="animate-rise">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="flex items-center gap-2 text-[14px] font-semibold text-fg"><ShieldCheck className="h-4 w-4 text-green" />插件运行时同步结果</h3>
              <p className="mt-0.5 text-xs text-fg-3">展示每个活跃会话的实际应用状态、回滚结果和残留漂移</p>
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                icon={<RefreshCw className={cn('h-3.5 w-3.5', retryingRuntime && 'animate-spin-slow')} />}
                onClick={() => void retryRuntimeChanges()}
                disabled={retryingRuntime || Boolean(application.view?.busy) || failedRuntimeSessions.length === 0}
              >
                重试未同步会话
              </Button>
              <IconButton label="关闭同步结果" size="sm" onClick={() => setRuntimeResult(null)}><X className="h-4 w-4" /></IconButton>
            </div>
          </div>
          <div className="scroll mt-3 max-h-72 space-y-2 pr-1">
            {runtimeResult.edictum_sessions.map((session) => (
              <div key={`${session.platform_id}:${session.session_id}`} className="rounded-xl border border-line bg-glass px-3 py-2.5 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-fg">{session.platform_id}:{session.session_id}</span>
                  <Badge tone={session.ok ? 'green' : 'orange'} dot>{session.ok ? '已同步' : session.restart_required ? '需重启' : '存在漂移'}</Badge>
                  {(session.drift || 0) > 0 && <Badge tone="orange">漂移 {session.drift}</Badge>}
                </div>
                {session.error && <p className="mt-1.5 break-words text-red">{session.error}</p>}
                {(session.plugins || []).map((plugin) => (
                  <div key={plugin.plugin} className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-fg-3">
                    <span className="font-mono text-fg-2">{plugin.plugin}</span>
                    <span>· {plugin.action || '同步'}</span>
                    <Badge tone={plugin.status === 'error' || plugin.status === 'rollback_error' ? 'red' : 'green'}>{plugin.status}</Badge>
                    {plugin.error && <span className="text-red">{plugin.error}</span>}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-3 animate-rise" style={{ animationDelay: '70ms' }}>
        <Tabs value={tab} onValueChange={(value) => setTab(value as Tab)}>
          <TabsList>
            <TabsTrigger value="session">会话类模板</TabsTrigger>
            <TabsTrigger value="edictum">Edictum 流程</TabsTrigger>
          </TabsList>
        </Tabs>
        <div className="ml-auto flex items-center gap-2">
          <div className="w-56 max-w-[46vw]">
            <Input leading={<Search className="h-4 w-4" />} placeholder="搜索名称、模型或类型…" value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索 Agent 配置" />
          </div>
          <div className="flex rounded-[10px] border border-line bg-glass p-0.5">
            <IconButton label="卡片视图" size="sm" active={view === 'grid'} onClick={() => setView('grid')}><LayoutGrid className="h-4 w-4" /></IconButton>
            <IconButton label="列表视图" size="sm" active={view === 'list'} onClick={() => setView('list')}><List className="h-4 w-4" /></IconButton>
          </div>
        </div>
      </div>

      {tab === 'edictum' && (
        <Card padding="md" className="animate-rise" style={{ animationDelay: '100ms' }}>
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="flex items-start gap-2.5">
              <span className="mt-0.5 grid h-7 w-7 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint"><Bot className="h-3.5 w-3.5" /></span>
              <div>
                <h3 className="text-[13.5px] font-semibold text-fg">可用 Edictum 类型</h3>
                <p className="mt-0.5 text-[11.5px] text-fg-3">类型由后端注册表提供; 选择类型后可直接创建命名配置</p>
              </div>
            </div>
            <div className="flex items-center gap-1">
              <Button size="sm" variant="ghost" icon={<RefreshCw className={cn('h-3.5 w-3.5', refreshing && 'animate-spin-slow')} />} onClick={() => void refresh()} disabled={refreshing}>刷新</Button>
              <Button size="sm" variant="ghost" onClick={() => setTypesOpen((current) => !current)}>{typesOpen ? '收起' : '展开'}</Button>
            </div>
          </div>
          {typesOpen && (
            <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {types.map((type) => (
                <button
                  key={type.name}
                  type="button"
                  onClick={() => openCreate('edictum', type.name)}
                  className="group rounded-xl border border-line bg-glass p-3 text-left transition-colors hover:border-[rgb(var(--rgb-teal)/0.4)] hover:bg-glass-hover"
                >
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-mono text-[12.5px] font-semibold text-fg">{type.name}</span>
                    <Badge tone="teal">{type.is_async ? 'async' : 'sync'}</Badge>
                    <span className="ml-auto text-[10px] font-medium text-tint opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">用此类型新建</span>
                  </div>
                  <p className="mt-1 line-clamp-2 min-h-[34px] text-[11.5px] leading-5 text-fg-3">{type.description}</p>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {type.capabilities.plugins ? <Badge>插件</Badge> : <Badge tone="neutral">无插件</Badge>}
                    {type.capabilities.mcp && <Badge>MCP</Badge>}
                    {type.capabilities.stream && <Badge>流式</Badge>}
                  </div>
                </button>
              ))}
              {!refreshing && types.length === 0 && <p className="text-sm text-fg-3">没有可用的 Edictum 类型</p>}
            </div>
          )}
        </Card>
      )}

      {tab === 'session' && (
        <Card padding="md" className="animate-rise" style={{ animationDelay: '100ms' }}>
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-[220px] flex-1">
              <Field label="Session 扫描目录" hint="扫描 Python 会话类文件, 注册后即可创建平台实例">
                {scanPaths.length > 0 ? (
                  <KitSelect className="field w-full" value={selectedScanPath} onChange={(value) => setSelectedScanPath(value)}>
                    {scanPaths.map((path) => <option key={path} value={path}>{path}</option>)}
                  </KitSelect>
                ) : (
                  <Input value="" readOnly placeholder="点击扫描读取配置目录" />
                )}
              </Field>
            </div>
            <Button icon={<FolderPlus className="h-4 w-4" />} onClick={() => void createScanDirectory()} disabled={scanning}>创建目录</Button>
            <Button variant="tinted" tint="teal" icon={<Search className="h-4 w-4" />} loading={scanning} onClick={() => void runScan(selectedScanPath)}>扫描</Button>
            <Button variant="ghost" icon={<Plus className="h-4 w-4" />} onClick={() => openCreate('session_class')}>手动注册</Button>
          </div>
          {discovered.length > 0 && (
            <div className="mt-3 space-y-2 border-t border-line pt-3">
              <div className="flex items-center justify-between gap-2">
                <p className="text-[11px] font-medium uppercase tracking-wider text-fg-4">扫描结果 · {discovered.length}</p>
                <button type="button" onClick={() => setDiscovered([])} className="text-[11px] text-fg-3 hover:text-fg">收起</button>
              </div>
              {discovered.map((item) => (
                <div key={`${item.file_path}-${item.class_name || item.error}`} className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-glass px-3 py-2.5">
                  <div className="min-w-0">
                    {item.error ? <p className="text-xs text-red">{item.file_path}: {item.error}</p> : (
                      <>
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-mono text-[13px] font-medium text-fg">{item.class_name}</span>
                          <Badge tone="teal">{item.is_async ? 'async' : 'sync'}</Badge>
                        </div>
                        <p className="truncate font-mono text-[11px] text-fg-3">{item.class_path}</p>
                      </>
                    )}
                  </div>
                  {!item.error && <Button size="sm" variant="tinted" tint="teal" onClick={() => registerDiscovered(item)}>注册</Button>}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {booting ? (
        <div className="stagger grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map((index) => (
            <Card key={index} padding="md" style={{ ['--i' as string]: index }}>
              <div className="flex items-start gap-3">
                <Skeleton className="h-10 w-10 rounded-xl" />
                <div className="flex-1 space-y-2"><Skeleton className="w-2/3" /><Skeleton className="h-3 w-1/2" /></div>
              </div>
              <Skeleton className="mt-4 h-3 w-full" />
              <Skeleton className="mt-2 h-3 w-4/5" />
              <div className="mt-4 flex gap-2"><Skeleton className="h-6 w-16 rounded-full" /><Skeleton className="h-6 w-20 rounded-full" /></div>
              <Skeleton className="mt-5 h-8 w-full" />
            </Card>
          ))}
        </div>
      ) : visible.length === 0 ? (
        <Card padding="none" className="animate-rise">
          <EmptyState
            tint="teal"
            icon={hasFilter ? <Search className="h-6 w-6" /> : <Bot className="h-6 w-6" />}
            title={hasFilter ? `没有匹配 “${query}” 的配置` : tab === 'session' ? '还没有会话类模板' : '还没有 Edictum 流程'}
            description={hasFilter ? '换个关键字试试, 或清除筛选条件' : tab === 'session' ? '扫描本地会话类目录, 或手动注册一个 Class Path' : '创建命名配置后即可绑定平台并管理插件'}
            actions={hasFilter
              ? <Button onClick={() => setQuery('')}>清除筛选</Button>
              : <Button variant="tinted" tint="teal" icon={<Plus className="h-4 w-4" />} onClick={() => openCreate(tab === 'session' ? 'session_class' : 'edictum')}>{tab === 'session' ? '手动注册' : '新建配置'}</Button>}
          />
        </Card>
      ) : view === 'grid' ? (
        <div key={`${tab}-${query}`} className="stagger grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {visible.map((agent, index) => (
            <AgentCard
              key={agent.name}
              agent={agent}
              index={index}
              types={types}
              onOpen={() => openEdit(agent)}
              onChanged={handleAgentChanged}
              onEdit={() => openEdit(agent)}
              onCreate={() => openCreateSession(agent)}
              onPlugins={() => setPluginTarget(agent.name)}
              onDelete={() => void askDelete(agent)}
            />
          ))}
          {tab === 'edictum' && (
            <button
              type="button"
              onClick={() => openCreate('edictum')}
              className="group grid min-h-[210px] place-items-center rounded-[var(--radius-lg)] border-2 border-dashed border-[var(--glass-border-strong)] text-fg-3 transition-all duration-300 hover:border-[rgb(var(--rgb-teal)/0.5)] hover:bg-[rgb(var(--rgb-teal)/0.06)] hover:text-teal"
              style={{ ['--i' as string]: visible.length }}
            >
              <span className="flex flex-col items-center gap-2 text-sm font-medium">
                <span className="grid h-10 w-10 place-items-center rounded-xl border border-current/20 transition-transform duration-300 group-hover:rotate-90 group-hover:scale-110"><Plus className="h-5 w-5" /></span>
                新建 Edictum 配置
              </span>
            </button>
          )}
        </div>
      ) : (
        <Card padding="none" className="animate-rise overflow-hidden">
          <div className="scroll overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-[11px] text-fg-3">
                  <th className="px-4 py-2.5 font-medium">Agent</th>
                  <th className="px-3 py-2.5 font-medium">类型</th>
                  <th className="px-3 py-2.5 font-medium">模型</th>
                  <th className="px-3 py-2.5 font-medium">{tab === 'edictum' ? '插件' : '上下文 / 模型键'}</th>
                  <th className="px-3 py-2.5 text-right font-medium">活跃实例</th>
                  <th className="px-3 py-2.5 font-medium">状态</th>
                  <th className="px-4 py-2.5 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((agent) => {
                  const createDisabled = agent.provider === 'edictum' && !agent.enabled;
                  return (
                    <tr
                      key={agent.name}
                      tabIndex={0}
                      onClick={() => openEdit(agent)}
                      onKeyDown={(event) => { if (event.key === 'Enter') openEdit(agent); }}
                      className="cursor-pointer border-b border-line transition-colors last:border-0 hover:bg-glass-hover focus-visible:bg-glass-hover"
                    >
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2.5">
                          <span className="grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--rgb-teal)/0.14)] text-teal">
                            {agent.provider === 'edictum' ? <Bot className="h-4 w-4" /> : <Code2 className="h-4 w-4" />}
                          </span>
                          <div className="min-w-0">
                            <div className="font-mono text-[13px] font-medium text-fg">{agent.name}</div>
                            <div className="max-w-[280px] truncate text-[11px] text-fg-3">{agent.description || '暂无描述'}</div>
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div><Badge tone={providerTone(agent.provider)}>{kindLabel(agent, types)}</Badge></div>
                        <div className="mt-1 max-w-[190px] truncate font-mono text-[10px] text-fg-4">{agent.provider === 'edictum' ? agent.edictum_type : agent.class_path}</div>
                      </td>
                      <td className="px-3 py-3 font-mono text-xs text-fg-2">{agent.model_name || '未绑定'}</td>
                      <td className="px-3 py-3 text-xs text-fg-2">
                        {agent.provider === 'edictum' ? `${enabledPluginCount(agent)}/${agent.plugins.length} 启用` : (
                          <>
                            <div>{agent.context_key || '—'}</div>
                            <div className="text-[10px] text-fg-4">模型键 {agent.model_key || '—'}</div>
                          </>
                        )}
                      </td>
                      <td className="num px-3 py-3 text-right font-mono text-xs text-fg-2">
                        {agent.activeInstances === null ? '—' : agent.activeInstances}
                        {agent.runningInstances !== null && agent.runningInstances > 0 && (
                          <span className="ml-1 text-[10px] text-teal">执行中 {agent.runningInstances}</span>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <span onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
                          <SessionEnabledToggle provider={agent.provider} name={agent.name} enabled={agent.enabled} onChanged={handleAgentChanged} />
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center justify-end gap-0.5" onClick={(event) => event.stopPropagation()}>
                          <IconButton label={createDisabled ? '创建会话 (需先启用配置)' : '创建会话'} title="创建会话" size="sm" disabled={createDisabled} onClick={() => openCreateSession(agent)}><Plus className="h-3.5 w-3.5" /></IconButton>
                          {agent.provider === 'edictum' && <IconButton label="管理插件" title="管理插件" size="sm" onClick={() => setPluginTarget(agent.name)}><Puzzle className="h-3.5 w-3.5" /></IconButton>}
                          <IconButton label="编辑配置" title="编辑配置" size="sm" onClick={() => openEdit(agent)}><Settings className="h-3.5 w-3.5" /></IconButton>
                          <IconButton label="删除" title="删除" size="sm" disabled={deleting} onClick={() => void askDelete(agent)} className="text-red hover:text-red"><Trash2 className="h-3.5 w-3.5" /></IconButton>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {tab === 'edictum' && (
            <div className="border-t border-line px-4 py-2.5">
              <button type="button" onClick={() => openCreate('edictum')} className="flex w-full items-center justify-center gap-2 rounded-lg py-1.5 text-xs text-fg-3 transition hover:bg-glass-hover hover:text-fg">
                <Plus className="h-3.5 w-3.5" />新建 Edictum 配置
              </button>
            </div>
          )}
        </Card>
      )}

      <AgentEditor
        key={`${editor?.mode ?? 'closed'}-${editor?.provider ?? 'none'}-${editor?.initial?.name ?? 'new'}`}
        open={editor !== null}
        mode={editor?.mode ?? 'create'}
        provider={editor?.provider ?? 'edictum'}
        initial={editor?.initial ?? null}
        plugins={editorPlugins}
        types={types}
        llmNames={Object.keys(llmConfigs)}
        llmConfigs={llmConfigs}
        saving={saving}
        statusContent={editor && application.view?.kind === 'full' && application.view.application.label === fullApplyLabel ? progressCard : undefined}
        onClose={() => { if (!saving) setEditor(null); }}
        onSave={(input) => void handleSaveAgent(input)}
        onManagePlugins={(name) => setPluginTarget(name)}
      />

      <PluginManager
        key={pluginTarget ?? 'none'}
        open={pluginTarget !== null && pluginAgent !== null}
        configName={pluginTarget}
        availablePlugins={availablePlugins}
        configuredPlugins={pluginAgent?.plugins}
        saving={pluginSaving}
        progress={pluginProgress}
        statusContent={pluginTarget && application.view?.kind === 'plugins' && application.view.application.label === pluginTarget ? progressCard : undefined}
        onClose={() => { if (!pluginSaving) setPluginTarget(null); }}
        onSave={handlePluginSave}
      />

      <CreateSessionModal
        open={createTarget !== null}
        agent={createTarget}
        backendRunning={isRunning}
        adapterIds={Object.keys(health?.adapters || {})}
        llmNames={Object.keys(llmConfigs)}
        onClose={() => setCreateTarget(null)}
        onCreate={async (data) => {
          if (!createTarget) return;
          const bound = platforms.find((platform) => platform.session_provider === 'edictum' && platform.session_type === createTarget.name);
          await submitCreateSession(createTarget, data, createTarget.provider === 'edictum' ? bound?.id : undefined);
        }}
      />

      <Modal
        open={confirmPrompt !== null}
        onClose={() => settleConfirm(false)}
        title="确认操作"
        size="sm"
        className="tint-orange"
        footer={<>
          <Button variant="ghost" onClick={() => settleConfirm(false)}>取消</Button>
          <Button variant="danger" onClick={() => settleConfirm(true)}>确认</Button>
        </>}
      >
        <p className="whitespace-pre-wrap break-words text-[13px] leading-6 text-fg-2">{confirmPrompt?.message}</p>
      </Modal>

      <PlatformChooseModal
        open={platformChoose !== null}
        agent={platformChoose}
        platforms={platforms}
        onClose={() => setPlatformChoose(null)}
        onConfirm={async (platformId) => {
          if (!platformChoose) return;
          await submitCreateSession(platformChoose, { session_id: '', adapter_id: '', llm_name: '', params: {} }, platformId);
          setPlatformChoose(null);
        }}
      />
    </div>
  );
}

function AgentCard({
  agent, index, types, onOpen, onChanged, onEdit, onCreate, onPlugins, onDelete,
}: {
  agent: AgentEntry;
  index: number;
  types: EdictumTypeDefinition[];
  onOpen: () => void;
  onChanged: () => void | Promise<void>;
  onEdit: () => void;
  onCreate: () => void;
  onPlugins: () => void;
  onDelete: () => void;
}) {
  const isEdictum = agent.provider === 'edictum';
  const typeDef = types.find((item) => item.name === agent.edictum_type);
  const createDisabled = isEdictum && !agent.enabled;
  return (
    <Card
      interactive
      tint="teal"
      padding="md"
      onClick={onOpen}
      aria-label={`打开 ${agent.name} 配置`}
      className={cn('group flex flex-col', !agent.enabled && 'opacity-80')}
      style={{ ['--i' as string]: index }}
    >
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">
          {isEdictum ? <Bot className="h-5 w-5" /> : <Code2 className="h-5 w-5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <h3 className="truncate font-mono text-[13.5px] font-semibold text-fg">{agent.name}</h3>
            <Badge tone="teal">{isEdictum ? (typeDef ? (typeDef.is_async ? 'async' : 'sync') : 'edictum') : agent.is_async ? 'async' : 'sync'}</Badge>
            {!agent.enabled && <Badge tone="orange">已停用</Badge>}
          </div>
          <p className="mt-0.5 text-[11px] text-fg-3">
            {providerLabel(agent.provider)} · {isEdictum ? agent.edictum_type : <span className="font-mono">{agent.class_path || '未设置类路径'}</span>}
          </p>
        </div>
        <span className="flex-none" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
          <SessionEnabledToggle provider={agent.provider} name={agent.name} enabled={agent.enabled} onChanged={onChanged} />
        </span>
      </div>

      <p className="mt-3 line-clamp-2 min-h-[38px] text-[12px] leading-5 text-fg-3">{agent.description || '暂无描述'}</p>

      <div className="mt-3 flex flex-wrap gap-1.5">
        <Badge tone={agent.model_name ? 'accent' : 'neutral'}><Cpu className="h-3 w-3" />{agent.model_name || '未绑定模型'}</Badge>
        {isEdictum ? (
          <Badge tone={enabledPluginCount(agent) > 0 ? 'teal' : 'neutral'}><Puzzle className="h-3 w-3" />{enabledPluginCount(agent)}/{agent.plugins.length} 插件</Badge>
        ) : (
          <>
            <Badge><Code2 className="h-3 w-3" />{agent.context_key || '无上下文键'}</Badge>
            <Badge><Cpu className="h-3 w-3" />模型键 {agent.model_key || '—'}</Badge>
          </>
        )}
        <Badge><Activity className="h-3 w-3" />{agent.activeInstances === null ? '实例未知' : `${agent.activeInstances} 实例`}</Badge>
        {agent.runningInstances !== null && agent.runningInstances > 0 && (
          <Badge tone="teal"><Activity className="h-3 w-3" />执行中 {agent.runningInstances}</Badge>
        )}
      </div>

      <div className="mt-auto flex items-center justify-between gap-2 border-t border-line pt-3">
        <span className="text-[10.5px] text-fg-4">更新于 {agent.updatedAt ? relativeTime(agent.updatedAt) : '—'}</span>
        <div className="flex items-center gap-0.5" onClick={(event) => event.stopPropagation()}>
          <IconButton label={createDisabled ? '创建会话 (需先启用配置)' : '创建会话'} title="创建会话" size="sm" disabled={createDisabled} onClick={onCreate}><Plus className="h-3.5 w-3.5" /></IconButton>
          {isEdictum && <IconButton label="管理插件" title="管理插件" size="sm" onClick={onPlugins}><Puzzle className="h-3.5 w-3.5" /></IconButton>}
          <IconButton label="编辑配置" title="编辑配置" size="sm" onClick={onEdit}><Settings className="h-3.5 w-3.5" /></IconButton>
          <IconButton label="删除" title="删除" size="sm" onClick={onDelete} className="text-red hover:text-red"><Trash2 className="h-3.5 w-3.5" /></IconButton>
        </div>
      </div>
    </Card>
  );
}

function CreateSessionModal({
  open, agent, backendRunning, adapterIds, llmNames, onClose, onCreate,
}: {
  open: boolean;
  agent: AgentEntry | null;
  backendRunning: boolean;
  adapterIds: string[];
  llmNames: string[];
  onClose: () => void;
  onCreate: (data: CreateSessionInput) => Promise<void>;
}) {
  const [form, setForm] = useState({ session_id: '', adapter_id: '', llm_name: '', params: '{}' });
  const [busy, setBusy] = useState(false);
  const [paramsError, setParamsError] = useState('');

  useEffect(() => {
    if (!open || !agent) return;
    setForm({ session_id: '', adapter_id: '', llm_name: agent.model_name || '', params: '{}' });
    setParamsError('');
    setBusy(false);
  }, [open, agent]);

  if (!agent) return null;
  const isEdictum = agent.provider === 'edictum';

  const submit = async () => {
    let parsed: Record<string, unknown>;
    try {
      const value: unknown = JSON.parse(form.params || '{}');
      if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('附加参数必须是 JSON 对象');
      parsed = value as Record<string, unknown>;
    } catch (error) {
      setParamsError(errorMessage(error));
      return;
    }
    setBusy(true);
    try {
      await onCreate({ session_id: form.session_id, adapter_id: form.adapter_id, llm_name: form.llm_name, params: parsed });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => { if (!busy) onClose(); }}
      title={`创建会话: ${agent.name}`}
      description={isEdictum ? '使用当前命名配置创建运行时会话' : '使用当前会话类模板创建平台实例'}
      icon={<Plus className="h-[18px] w-[18px]" />}
      size="lg"
      className="tint-teal"
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={busy}>取消</Button>
        <Button variant="tinted" tint="teal" loading={busy} onClick={() => void submit()}>{isEdictum ? '创建并激活' : '创建'}</Button>
      </>}
    >
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-glass px-3 py-2 text-xs text-fg-3">
          <Badge tone="teal">{providerLabel(agent.provider)}</Badge>
          <span className="font-mono text-fg-2">{isEdictum ? agent.edictum_type : agent.class_path}</span>
          <span className={cn('ml-auto text-[11px]', backendRunning ? 'text-green' : 'text-orange')}>{backendRunning ? '热创建并激活' : '冷创建, 后端启动后激活'}</span>
        </div>
        <Field label="Session ID" hint="留空将由后端自动生成; 同一平台内需唯一">
          <Input value={form.session_id} onChange={(event) => setForm((current) => ({ ...current, session_id: event.target.value }))} placeholder="如: g8823-assistant" />
        </Field>
        {!isEdictum && (
          <Field label="适配器初始化参数" hint="留空表示按消息来源自动选择">
            <KitSelect className="field w-full" value={form.adapter_id} onChange={(value) => setForm((current) => ({ ...current, adapter_id: value }))}>
              <option value="">自动 (按消息来源)</option>
              {adapterIds.map((id) => <option key={id} value={id}>{id}</option>)}
            </KitSelect>
          </Field>
        )}
        <Field label="LLM 模型" hint="留空使用配置绑定的模型">
          <KitSelect className="field w-full" value={form.llm_name} onChange={(value) => setForm((current) => ({ ...current, llm_name: value }))}>
            <option value="">使用配置绑定</option>
            {llmNames.map((name) => <option key={name} value={name}>{name}</option>)}
          </KitSelect>
        </Field>
        <Field label="附加参数 (JSON)" error={paramsError} hint="会与此配置的 params 合并">
          <Textarea rows={5} value={form.params} onChange={(event) => { setForm((current) => ({ ...current, params: event.target.value })); setParamsError(''); }} spellCheck={false} className="font-mono text-[12px]" />
        </Field>
      </div>
    </Modal>
  );
}

function PlatformChooseModal({
  open, agent, platforms, onClose, onConfirm,
}: {
  open: boolean;
  agent: AgentEntry | null;
  platforms: PlatformConfig[];
  onClose: () => void;
  onConfirm: (platformId: string) => Promise<void>;
}) {
  const options = agent
    ? platforms.filter((platform) => platform.session_provider === 'edictum' && platform.session_type === agent.name)
    : [];
  const [platformId, setPlatformId] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open || options.length === 0) return;
    setPlatformId(options[0].id);
    setBusy(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, agent?.name]);

  return (
    <Modal
      open={open}
      onClose={() => { if (!busy) onClose(); }}
      title={`选择运行平台: ${agent?.name ?? ''}`}
      description="此 Edictum 配置同时绑定多个平台, 请选择要创建的实例"
      icon={<Bot className="h-[18px] w-[18px]" />}
      size="sm"
      className="tint-teal"
      footer={<>
        <Button variant="ghost" onClick={onClose} disabled={busy}>取消</Button>
        <Button variant="tinted" tint="teal" loading={busy} disabled={!platformId} onClick={() => { setBusy(true); void onConfirm(platformId).finally(() => setBusy(false)); }}>创建并激活</Button>
      </>}
    >
      <Field label="目标平台" required hint="仅列出绑定了当前配置的平台">
        <KitSelect className="field w-full" value={platformId} onChange={(value) => setPlatformId(value)}>
          {options.map((platform) => <option key={platform.id} value={platform.id}>{`${platform.id} (${platform.type})`}</option>)}
        </KitSelect>
      </Field>
    </Modal>
  );
}
