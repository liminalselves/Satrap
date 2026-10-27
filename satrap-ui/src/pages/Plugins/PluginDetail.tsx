import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  AlertTriangle, ArrowLeft, BookOpen, CheckCircle2, Clock, FileText, Gauge, Info,
  Puzzle, RefreshCw, Server, ShieldAlert, SlidersHorizontal, Smile, Wrench,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, Card, CardHeader, EmptyState, Skeleton, Switch, toast } from '@/components/kit';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { errorText } from '@/utils/errorText';
import { sourceStateLabels } from '@/api/contracts';
import { pluginCatalogHealthRead, pluginExecutionApi, type CatalogPlugin, type PluginExecutionSaveResult } from '@/api/pluginsContracts';
import { PluginInteractions } from '@/components/common/PluginCapabilities';
import type { PluginRuntimeResult } from '@/api/types';
import { useDirtyGuard, confirmDiscard } from '@/hooks/useDirtyGuard';
import { GlobalPluginSettings, PluginRuntimeStatus } from './GlobalPluginSettings';
import { PluginUsages } from './PluginUsages';
import { SkillLibrary } from './SkillLibrary';
import { StickerLibrary } from './StickerLibrary';
import {
  CapabilityDeclarations, ExecutionChip, HealthChip, PluginIcon, SourceChip, TintTabs,
} from './PluginBits';
import {
  CAPABILITY_KINDS, capabilityCount, displayName, executionEnabled, externalUrl, healthLabel,
  pluginCapabilities, pluginHealth, sessionText, platformText, timestampText,
} from './pluginModel';

type Tab = 'overview' | 'config' | 'usages' | 'skills' | 'stickers';

function usePluginCatalog() {
  const [plugins, setPlugins] = useState<CatalogPlugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    setLoading(true);
    setError('');
    let force = revision > 0;
    const poll = startVisiblePoll(async (signal) => {
      const forced = force; force = false;
      try {
        const data = await pluginCatalogHealthRead.read({ force: forced, signal });
        if (!signal.aborted) { setPlugins(data); setError(''); }
      } finally { if (!signal.aborted) setLoading(false); }
    }, { interval: 30000, onError: (reason: unknown) => setError(errorText(reason, '读取插件失败')) });
    return () => poll.stop();
  }, [revision]);
  return { plugins, loading, error, refresh };
}

function normalizeTab(value: string | null, pluginName?: string): Tab {
  if (value === 'config' || value === 'usages' || value === 'overview') return value;
  if (value === 'skills' && pluginName === 'skills') return 'skills';
  if (value === 'stickers' && pluginName === 'group_chat') return 'stickers';
  return 'overview';
}

function ExternalField({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  const href = externalUrl(value);
  return (
    <div className="min-w-0">
      <dt className="text-[10.5px] text-fg-4">{label}</dt>
      <dd className="mt-0.5 break-all text-fg-2">
        {href
          ? <a href={href} target="_blank" rel="noopener noreferrer" className="text-teal transition hover:underline">{value}</a>
          : value}
      </dd>
    </div>
  );
}

function Overview({ plugin, onOpenTab }: { plugin: CatalogPlugin; onOpenTab: (tab: Tab) => void }) {
  const caps = pluginCapabilities(plugin);
  const health = pluginHealth(plugin);
  const coverage = plugin.health?.coverage;
  const instances = plugin.health?.instances ?? [];
  const issues = instances.filter((instance) => {
    const value = instance as { plugin?: { error?: string; drift?: boolean; restart_required?: boolean } };
    return Boolean(value.plugin?.error || value.plugin?.drift || value.plugin?.restart_required);
  });
  const healthNote = plugin.health?.state === 'attention'
    ? issues.map((instance) => {
      const value = instance as { platform_id?: string; session_id?: string; plugin?: { error?: string } };
      return `[${value.platform_id ?? '—'} / ${value.session_id ?? '—'}] ${value.plugin?.error || '实例状态异常'}`;
    }).join('；') || '存在运行实例异常或降级，请到使用位置查看实际状态。'
    : null;
  const infoRows: Array<[string, ReactNode]> = [
    ['作者', plugin.author || '未声明'],
    ['版本 / 来源', <span className="flex flex-wrap items-center gap-1.5"><span className="font-mono">{plugin.version || '未声明'}</span><SourceChip source={plugin.source} /></span>],
    ['Satrap 要求', <span className="font-mono">{plugin.compatibility?.satrap || '未声明'}</span>],
    ['适用会话', sessionText(plugin.applicability)],
    ['适用平台', platformText(plugin.applicability)],
    ['配置结构版本', <span className="num font-mono">{plugin.config_version || '未声明'}</span>],
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card padding="lg">
          <CardHeader icon={<Info className="h-4 w-4" />} title="插件信息" description="来自插件元数据 (meta.yaml)" />
          <dl className="grid grid-cols-1 gap-x-6 gap-y-2.5 text-[12.5px] sm:grid-cols-2">
            {infoRows.map(([label, value]) => (
              <div key={label} className="min-w-0">
                <dt className="text-[10.5px] text-fg-4">{label}</dt>
                <dd className="mt-0.5 break-words text-fg-2">{value}</dd>
              </div>
            ))}
            {plugin.license && <div className="min-w-0"><dt className="text-[10.5px] text-fg-4">许可证</dt><dd className="mt-0.5 break-words text-fg-2">{plugin.license}</dd></div>}
            <ExternalField label="主页" value={plugin.homepage} />
            <ExternalField label="仓库" value={plugin.repository} />
          </dl>
          <p className="mt-4 border-t border-line pt-3 text-[12.5px] leading-5 text-fg-2">{plugin.description || '暂无说明'}</p>
          <p className="mt-2 text-[11.5px] text-fg-3">
            <span className="num">{plugin.usage_count ?? 0}</span> 个使用位置 · 能力声明来自插件元数据，实际加载结果由各运行实例决定。
          </p>
        </Card>

        <Card padding="lg">
          <CardHeader
            icon={<Gauge className="h-4 w-4" />}
            title="健康与覆盖"
            description="运行实例状态与观测来源"
            actions={<HealthChip plugin={plugin} />}
          />
          <div className={cn('rounded-xl border border-line bg-glass p-3.5', health === 'attention' ? 'tint-orange' : health === 'healthy' ? 'tint-green' : 'tint-teal')}>
            <div className="flex items-center gap-2 text-[12.5px] font-medium text-tint">
              <span className="dot" data-live={health === 'healthy' ? '1' : undefined} />
              {healthLabel(plugin)}
            </div>
            <p className="mt-1 text-[12px] leading-5 text-fg-2">
              {healthNote
                ?? (health === 'healthy'
                  ? '运行服务提供的观测显示插件实例正常。'
                  : health === 'disabled'
                    ? '执行总闸已关闭，所有使用位置的能力开关都会保留。'
                    : '运行服务未提供可确认的健康观测，未知项不会显示为 0。')}
            </p>
            <div className="mt-2.5 flex flex-wrap gap-3 text-[11px] text-fg-3">
              <span className="num">观测实例 {instances.length}</span>
              <span className={cn('num', issues.length > 0 && 'text-orange')}>需处理 {issues.length}</span>
              <span className="num">观测时间 {timestampText(plugin.health?.as_of) ?? '未知'}</span>
            </div>
          </div>

          <div className="mt-3 rounded-xl border border-line bg-glass p-3.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[12px] font-medium text-fg-2">覆盖来源</span>
              {coverage && !coverage.complete && <Badge tone="orange" dot>覆盖不完整</Badge>}
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {coverage?.sources?.length
                ? coverage.sources.map((source) => (
                  <Badge key={source.service} tone={source.state === 'available' ? 'green' : source.state === 'stale' ? 'orange' : 'red'} dot live={source.state === 'available'}>
                    {source.service} · {sourceStateLabels[source.state] || source.state}
                  </Badge>
                ))
                : <Badge tone="neutral">未提供覆盖来源</Badge>}
            </div>
            <div className="mt-3 flex items-center gap-2 text-[11.5px] text-fg-3">
              <CheckCircle2 className="h-3.5 w-3.5 text-green" />
              控制服务未提供在线更新源，不会自动抓取外部地址。
            </div>
          </div>
        </Card>
      </div>

      <Card padding="lg">
        <CardHeader
          icon={<Wrench className="h-4 w-4" />}
          title="声明的能力"
          description="工具、服务、技能、MCP、前处理与命令"
          actions={<Badge tone="teal">{capabilityCount(caps)} 项</Badge>}
        />
        <CapabilityDeclarations caps={caps} />
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card padding="lg">
          <CardHeader icon={<Puzzle className="h-4 w-4" />} title="插件交互" description="与其他插件的依赖、替代与冲突" />
          <PluginInteractions interop={plugin.interop} />
        </Card>

        <div className="space-y-4">
          <Card padding="lg">
            <CardHeader
              icon={<Server className="h-4 w-4" />}
              title="使用位置"
              description={`${plugin.usage_count ?? 0} 个使用位置`}
              actions={<Button size="sm" variant="ghost" onClick={() => onOpenTab('usages')}>管理</Button>}
            />
            <ul className="space-y-1">
              <li className="flex items-center gap-2.5 rounded-lg px-2 py-1.5">
                <span className={cn('dot', plugin.chat_enabled && 'tint-green')} data-live={plugin.chat_enabled ? '1' : undefined} />
                <span className="min-w-0 flex-1 truncate text-[12.5px] text-fg-2">Chat</span>
                <Badge tone={plugin.chat_enabled ? 'green' : 'neutral'}>{plugin.chat_enabled ? '已启用' : '未启用'}</Badge>
              </li>
              {(plugin.edictum_configs ?? []).map((name) => (
                <li key={name} className="flex items-center gap-2.5 rounded-lg px-2 py-1.5">
                  <span className="dot tint-purple" />
                  <span className="min-w-0 flex-1 truncate text-[12.5px] text-fg-2">{name}</span>
                  <Badge tone="purple">命名配置</Badge>
                </li>
              ))}
              {!plugin.chat_enabled && !(plugin.edictum_configs ?? []).length && (
                <li className="rounded-lg border border-dashed border-line px-3 py-4 text-center text-[12px] text-fg-3">尚未添加使用位置</li>
              )}
            </ul>
          </Card>

          <Card padding="lg">
            <CardHeader
              icon={<SlidersHorizontal className="h-4 w-4" />}
              title="配置摘要"
              description="全局参数供所有使用位置继承"
              actions={<Button size="sm" variant="ghost" onClick={() => onOpenTab('config')}>编辑</Button>}
            />
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-lg border border-line bg-glass px-3 py-2">
                <div className="text-[10.5px] text-fg-4">配置结构版本</div>
                <div className="num mt-0.5 text-[15px] font-semibold text-fg">{plugin.config_version || '未声明'}</div>
              </div>
              <div className="rounded-lg border border-line bg-glass px-3 py-2">
                <div className="text-[10.5px] text-fg-4">全局更新时间</div>
                <div className="mt-0.5 truncate text-[12px] text-fg-2">打开全局参数查看</div>
              </div>
            </div>
            <p className="mt-2.5 text-[12px] text-fg-3">最近更新时间和已覆盖字段会从全局参数接口读取；缺少旧元数据时显示“未知”。</p>
          </Card>
        </div>
      </div>
    </div>
  );
}

export function PluginDetail() {
  const navigate = useNavigate();
  const { name = '' } = useParams();
  const [params] = useSearchParams();
  const { plugins, loading, error, refresh } = usePluginCatalog();
  const plugin = plugins.find((item) => item.name === name);
  const [tab, setTab] = useState<Tab>('overview');
  const [dirty, setDirty] = useState(false);
  const [execution, setExecution] = useState<{ enabled: boolean; revision: number } | null>(null);
  const [executionBusy, setExecutionBusy] = useState(false);
  const [executionError, setExecutionError] = useState('');
  const [executionRuntime, setExecutionRuntime] = useState<PluginRuntimeResult[]>([]);
  useDirtyGuard(dirty);

  const tabParam = params.get('tab');
  useEffect(() => { setDirty(false); }, [name]);
  // URL 中的 tab 变化时同步标签; 目录轮询刷新不会重置当前标签
  useEffect(() => {
    setTab(normalizeTab(tabParam, plugin?.name));
  }, [tabParam, plugin?.name]);

  const pluginName = plugin?.name;
  const pluginExecutionEnabled = plugin?.execution_enabled;
  const pluginHostRevision = plugin?.host_revision;
  useEffect(() => {
    if (pluginName === undefined || pluginExecutionEnabled === undefined) { setExecution(null); return; }
    setExecution({ enabled: pluginExecutionEnabled, revision: pluginHostRevision ?? 0 });
  }, [pluginName, pluginExecutionEnabled, pluginHostRevision]);

  const changeTab = (next: Tab) => {
    if (next === tab) return;
    if (dirty && !confirmDiscard()) return;
    setDirty(false);
    setTab(next);
  };

  const saveExecution = async (next: boolean) => {
    if (!plugin || !execution || executionBusy) return;
    setExecutionBusy(true);
    setExecutionError('');
    setExecutionRuntime([]);
    try {
      const result: PluginExecutionSaveResult = await pluginExecutionApi.save(plugin.name, next, execution.revision);
      setExecution({ enabled: result.execution_enabled, revision: result.revision });
      setExecutionRuntime(result.runtime || []);
      toast.success(result.execution_enabled ? '已打开插件执行总闸' : '已关闭插件执行总闸', '各使用位置的能力开关保持不变');
      refresh();
    } catch (reason) {
      setExecutionError(errorText(reason, '保存插件执行总闸失败'));
      refresh();
    } finally { setExecutionBusy(false); }
  };

  const tabs = useMemo(() => {
    if (!plugin) return [];
    const items: { value: Tab; label: string; icon: ReactNode }[] = [
      { value: 'overview', label: '概览', icon: <Info className="h-3.5 w-3.5" /> },
      { value: 'config', label: '全局参数', icon: <SlidersHorizontal className="h-3.5 w-3.5" /> },
      { value: 'usages', label: '使用位置', icon: <Server className="h-3.5 w-3.5" /> },
    ];
    if (plugin.name === 'skills') items.push({ value: 'skills', label: '技能库', icon: <BookOpen className="h-3.5 w-3.5" /> });
    if (plugin.name === 'group_chat') items.push({ value: 'stickers', label: '表情库', icon: <Smile className="h-3.5 w-3.5" /> });
    return items;
  }, [plugin]);

  if (!plugin && loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-4 w-24" />
        <Card padding="lg" className="space-y-4">
          <div className="flex gap-4"><Skeleton className="h-16 w-16" /><div className="flex-1 space-y-2"><Skeleton className="w-1/3" /><Skeleton className="w-1/4" /><Skeleton className="w-1/2" /></div></div>
          <Skeleton className="h-20 w-full" />
        </Card>
        <Skeleton className="h-10 w-80 max-w-full" />
        <div className="grid gap-4 lg:grid-cols-2"><Skeleton className="h-48 w-full" /><Skeleton className="h-48 w-full" /></div>
      </div>
    );
  }

  if (!plugin) {
    return (
      <div className="space-y-4">
        <Link to="/plugins" className="inline-flex items-center gap-1 text-xs text-fg-3 transition hover:text-fg">
          <ArrowLeft className="h-3 w-3" />← 返回插件列表
        </Link>
        <Card padding="none">
          <div role="alert">
            <EmptyState
              tint="orange"
              icon={<AlertTriangle className="h-6 w-6" />}
              title={error ? '无法读取插件详情' : '插件不存在或元数据无效'}
              description={error || `没有找到名为 “${name}” 的插件。它可能已被卸载，或者 Web 界面与控制服务的插件目录不同步。`}
              actions={<Button onClick={() => navigate('/plugins')}>返回插件列表</Button>}
            />
          </div>
        </Card>
      </div>
    );
  }

  const health = pluginHealth(plugin);
  const caps = pluginCapabilities(plugin);
  const instanceCount = plugin.health?.instances.length ?? 0;
  const executionState = execution?.enabled ?? executionEnabled(plugin);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-2 animate-rise">
        <Link to="/plugins" className="inline-flex items-center gap-1 text-xs text-fg-3 transition hover:text-fg">
          <ArrowLeft className="h-3 w-3" />← 返回插件列表
        </Link>
        <span className="flex items-center gap-1.5 font-mono text-[11px] text-fg-4">
          <CheckCircle2 className="h-3 w-3 text-green" />插件目录已同步
        </span>
      </div>

      {error && <p role="alert" className="text-[12.5px] text-red">{error}</p>}

      <Card tint={health === 'attention' ? 'orange' : health === 'healthy' ? 'green' : 'teal'} padding="lg" className="animate-rise overflow-hidden" style={{ ['--i' as string]: 0 }}>
        <div aria-hidden className="pointer-events-none absolute -right-16 -top-24 h-64 w-64 rounded-full bg-[radial-gradient(circle,rgb(var(--tint)/0.3),transparent_65%)] blur-2xl" />
        <div className="relative flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex min-w-0 items-start gap-4">
            <PluginIcon plugin={plugin} size="lg" showHealth />
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="display text-[24px] font-semibold leading-tight text-fg">{displayName(plugin)}</h1>
                <Badge tone="neutral" className="font-mono">v{plugin.version || '未声明'}</Badge>
                <SourceChip source={plugin.source} />
                <HealthChip plugin={plugin} />
                <ExecutionChip enabled={executionState} />
              </div>
              <p className="mt-1 break-all font-mono text-[11px] text-fg-4">{plugin.name}</p>
              <p className="mt-2 max-w-2xl text-[13px] leading-5 text-fg-2">{plugin.description || '暂无说明'}</p>
              <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11.5px] text-fg-3">
                <span>作者 {plugin.author || '未声明'}</span>
                <span className="flex items-center gap-1"><FileText className="h-3 w-3" />{sessionText(plugin.applicability)}</span>
                <span className="flex items-center gap-1"><Server className="h-3 w-3" />{platformText(plugin.applicability)}</span>
                <span className="flex items-center gap-1"><Clock className="h-3 w-3" />配置结构 v{plugin.config_version || '未声明'}</span>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 lg:flex-col lg:items-end">
            {executionState !== undefined && (
              <div className="flex items-center gap-2 rounded-full border border-line bg-glass px-3 py-1">
                <span className="text-[12px] text-fg-2">执行总闸</span>
                <Switch label="插件执行总闸" checked={Boolean(executionState)} disabled={executionBusy} onChange={(value) => void saveExecution(value)} />
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <Button size="sm" icon={<RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin-slow')} />} disabled={loading} onClick={refresh}>刷新</Button>
            </div>
          </div>
        </div>

        <div className="relative mt-5 grid grid-cols-2 gap-2 border-t border-line pt-4 sm:grid-cols-4">
          {[
            { label: '使用位置', value: plugin.usage_count ?? 0, hint: `${(plugin.edictum_configs ?? []).length} 个命名配置` },
            { label: '声明能力', value: capabilityCount(caps), hint: `${CAPABILITY_KINDS.filter((kind) => Object.keys(caps[kind] ?? {}).length).length} 类` },
            { label: '观测实例', value: instanceCount, hint: plugin.health ? (plugin.health.coverage.complete ? '覆盖完整' : '覆盖不完整') : '未知' },
            { label: '执行状态', value: executionState === undefined ? '未知' : executionState ? '开启' : '停用', hint: executionState === undefined ? '服务未提供总闸' : '宿主总闸' },
          ].map((stat) => (
            <div key={stat.label} className="rounded-xl border border-line bg-glass px-3 py-2">
              <div className="text-[10.5px] text-fg-4">{stat.label}</div>
              <div className="num mt-0.5 text-[18px] font-semibold text-fg">{stat.value}</div>
              <div className="truncate text-[10.5px] text-fg-4">{stat.hint}</div>
            </div>
          ))}
        </div>

        {executionError && (
          <div role="alert" className="relative mt-3 flex items-start gap-2 rounded-xl border border-[rgb(var(--rgb-red)/0.25)] bg-[rgb(var(--rgb-red)/0.08)] px-3.5 py-2.5 text-[12px] text-red">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 flex-none" />{executionError}
          </div>
        )}
      </Card>

      <div className="max-w-full overflow-x-auto animate-rise" style={{ animationDelay: '60ms' }}>
        <TintTabs<Tab> label="插件详情标签" value={tab} onChange={changeTab} items={tabs} />
      </div>

      <div key={tab} className="animate-rise">
        {tab === 'overview' && <Overview plugin={plugin} onOpenTab={changeTab} />}
        {tab === 'config' && <GlobalPluginSettings key={plugin.name} name={plugin.name} onDirty={setDirty} />}
        {tab === 'usages' && <PluginUsages key={plugin.name} plugin={plugin} onDirty={setDirty} onSaved={refresh} />}
        {tab === 'skills' && plugin.name === 'skills' && <SkillLibrary onDirty={setDirty} />}
        {tab === 'stickers' && plugin.name === 'group_chat' && <StickerLibrary onDirty={setDirty} />}
      </div>

      {executionRuntime.length > 0 && <PluginRuntimeStatus results={executionRuntime} onRetry={() => void saveExecution(execution?.enabled ?? true)} busy={executionBusy} />}
    </div>
  );
}
